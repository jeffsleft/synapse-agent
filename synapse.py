import modal
import os
import time
from pydantic import BaseModel
from urllib.parse import urlparse
from typing import Annotated

# --- Top-of-file constants (override via Modal secret env vars) ---
CONTENT_CAP = int(os.environ.get("SYNAPSE_CONTENT_CAP", "30000"))
TWITTER_CAP = int(os.environ.get("SYNAPSE_TWITTER_CAP", "1500"))
NOTION_BLOCK_LIMIT = 2000
MODEL = os.environ.get("SYNAPSE_MODEL", "gemini-flash-latest")
FALLBACK_MODEL = os.environ.get("SYNAPSE_FALLBACK_MODEL", "gemini-flash-lite-latest")

PODCAST_DOMAINS = {
    "open.spotify.com", "podcasts.apple.com", "podcasts.google.com",
    "overcast.fm", "pocketcasts.com", "anchor.fm", "buzzsprout.com",
    "simplecast.com", "podbean.com", "transistor.fm", "player.fm",
}

# Private/metadata IPs to block (SSRF protection)
_BLOCKED_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "::1")


image = modal.Image.debian_slim().pip_install(
    "google-genai",
    "requests",
    "firecrawl-py",
    "fastapi[standard]",
)
app = modal.App("synapse-agent")


class Query(BaseModel):
    url: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_url(url: str) -> str | None:
    """Returns an error string if the URL is invalid or disallowed, else None."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL format."
    if parsed.scheme not in ("http", "https"):
        return "Only http and https URLs are supported."
    host = (parsed.hostname or "").lower()
    if any(host == b or host.endswith(f".{b}") for b in _BLOCKED_HOSTS):
        return "URL target is not allowed."
    return None


def _detect_unsupported(url: str) -> str | None:
    """Returns a user-facing message if the URL type is unsupported, else None."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    hostname = (parsed.hostname or "").lower()
    root_domain = ".".join(hostname.split(".")[-2:]) if "." in hostname else hostname

    if path.endswith(".pdf"):
        return "Direct PDF links are not supported. Submit a page that embeds or links the PDF."

    if {hostname, root_domain} & PODCAST_DOMAINS:
        return "Podcast URLs without embedded transcripts are not supported. Submit a transcript page instead."

    return None



def _gemini_generate(client, contents: list) -> str:
    """Calls Gemini. Fails fast on rate limit and billing exhaustion — no retries.
    Interactive tool: return a clear error immediately so the caller can retry."""
    from google.genai import errors as genai_errors

    # Overload (503/500) is often per-model: retry once on the primary, then fall back.
    for attempt, model in enumerate((MODEL, MODEL, FALLBACK_MODEL)):
        try:
            response = client.models.generate_content(model=model, contents=contents)
            if model != MODEL:
                print(f"⚠️ Used fallback model {model}")
            return response.text
        except genai_errors.ServerError as e:
            print(f"⚠️ Gemini server error on {model} (attempt {attempt + 1}): {e}")
            if attempt == 0:
                time.sleep(2)
            continue
        except genai_errors.ClientError as e:
            error_str = str(e)
            if "RESOURCE_EXHAUSTED" in error_str:
                if any(kw in error_str.lower() for kw in ("credits", "prepayment", "billing")):
                    raise ValueError("Gemini credits depleted. Top up at https://aistudio.google.com/app/plan")
                raise ValueError("Gemini is rate limited. Wait a few seconds and try again.")
            raise
    raise ValueError("Gemini is temporarily overloaded. Try again in a minute.")


def _build_text_blocks(synthesis: str) -> list:
    """Splits synthesis into Notion block children, respecting 2000-char limit."""
    text_blocks = []
    for p in synthesis.split('\n\n'):
        if not p.strip():
            continue
        block_type = "paragraph"
        content = p.strip()

        if content.startswith('##'):
            block_type = "heading_2"
            content = content.replace('##', '').strip()

        if len(content) > NOTION_BLOCK_LIMIT:
            for chunk in [content[i:i+NOTION_BLOCK_LIMIT] for i in range(0, len(content), NOTION_BLOCK_LIMIT)]:
                text_blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}
                })
        else:
            text_blocks.append({
                "object": "block",
                "type": block_type,
                block_type: {"rich_text": [{"type": "text", "text": {"content": content}}]}
            })
    return text_blocks


def _notion_request(method: str, path: str, token: str, **kwargs) -> dict:
    import requests
    resp = requests.request(
        method,
        f"https://api.notion.com/v1{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        **kwargs,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@app.function(image=image, secrets=[modal.Secret.from_name("project-synapse")])
@modal.fastapi_endpoint(method="POST")
def process_link(query: Query, x_passcode: Annotated[str | None, "Header"] = None):
    from google import genai
    import requests
    from datetime import datetime

    # --- Optional passcode auth (activate by adding SYNAPSE_PASSCODE to Modal secret) ---
    required_passcode = os.environ.get("SYNAPSE_PASSCODE")
    if required_passcode and x_passcode != required_passcode:
        return {"status": "error", "message": "Unauthorized"}

    url = query.url

    validation_error = _validate_url(url)
    if validation_error:
        return {"status": "error", "message": validation_error}

    unsupported = _detect_unsupported(url)
    if unsupported:
        return {"status": "unsupported", "message": unsupported}

    try:
        print(f"🚀 Processing: {url}")

        process_date = datetime.now().strftime("%Y-%m-%d")

        # 1. Fetch content — plain requests first, Jina fallback for thin/failed responses
        _BROWSER_UA = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        page_title = "Untitled Source"
        raw_content = ""

        try:
            plain_res = requests.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=15)
            if plain_res.ok and len(plain_res.text) >= 500:
                raw_content = plain_res.text[:CONTENT_CAP]
                # Best-effort title from <title> tag
                title_match = __import__("re").search(r"<title[^>]*>([^<]+)</title>", plain_res.text, __import__("re").IGNORECASE)
                if title_match:
                    page_title = title_match.group(1).strip()[:200].replace("\n", " ").replace("```", "")
                print(f"📄 Plain fetch succeeded ({len(raw_content)} chars)")
        except Exception as e:
            print(f"⚠️ Plain fetch failed: {e}")

        if len(raw_content) < 500:
            print("⚠️ Thin or failed plain fetch — falling back to Jina.")
            reader_url = f"https://r.jina.ai/{url}"
            jina_headers = {"Authorization": f"Bearer {os.environ['JINA_API_KEY']}"}
            jina_res = requests.get(reader_url, headers=jina_headers, timeout=30)
            if jina_res.ok:
                page_title = jina_res.headers.get("x-respond-title", page_title)
                page_title = page_title[:200].replace("\n", " ").replace("```", "")
                raw_content = jina_res.text[:CONTENT_CAP]
                print(f"🔁 Jina fallback succeeded ({len(raw_content)} chars)")
            else:
                print(f"⚠️ Jina failed (HTTP {jina_res.status_code}) — falling back to Firecrawl.")

        if len(raw_content) < 300:
            print("⚠️ Thin content after Jina — falling back to Firecrawl.")
            try:
                from firecrawl import FirecrawlApp
                fc = FirecrawlApp(api_key=os.environ["FIRECRAWL_API_KEY"])
                fc_result = fc.scrape_url(url, formats=["markdown"])
                if fc_result and fc_result.markdown:
                    raw_content = fc_result.markdown[:CONTENT_CAP]
                    if fc_result.metadata and fc_result.metadata.get("title"):
                        page_title = fc_result.metadata["title"][:200].replace("\n", " ").replace("```", "")
                    print(f"🔥 Firecrawl fallback succeeded ({len(raw_content)} chars)")
                else:
                    raise ValueError("Failed to fetch content from all sources. Check the URL and try again.")
            except ImportError:
                raise ValueError("Firecrawl not available. Check Modal image configuration.")
            except Exception as e:
                if isinstance(e, ValueError):
                    raise
                raise ValueError(f"Firecrawl failed: {e}. Check the URL and try again.")

        if "x.com" in url or "twitter.com" in url:
            raw_content = raw_content[:TWITTER_CAP]
            print("🐦 Twitter filter applied.")

        # 2. Deduplication check
        notion_token = os.environ["NOTION_TOKEN"]
        db_id = os.environ["NOTION_DATABASE_ID"]
        existing = _notion_request(
            "POST", f"/databases/{db_id}/query", notion_token,
            json={"filter": {"property": "URL", "url": {"equals": url}}}
        )
        if existing.get("results"):
            existing_page_url = existing["results"][0].get("url")
            return {"status": "duplicate", "notion_url": existing_page_url, "message": "URL already processed."}

        # 3. Synthesize with Gemini (with backoff)
        genai_client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options={'api_version': 'v1beta'}
        )

        system_prompt = f"""
        You are a literalist Strategic Analyst.

        CRITICAL RULES:
        1. Start immediately with '# [Punchy Title]'. No intro text or "This analysis is based on...".
        2. DO NOT invent philosophy or external context. Only analyze the provided text.
        3. If you see sidebar noise (like "Who to follow" or "Trending"), IGNORE IT.
        4. Focus 100% on the PRIMARY CONTENT of the source.
        5. If a section is not applicable (e.g., a very short tweet), write "N/A for this content."

        Produce these exact headers:
        ## Executive Summary (SCR)
        ## Epiphanies / Learnings
        ## Core Concepts
        ## Action Items
        ## Follow-up Questions
        ## Controversial Opinions
        ## 3 'If True' Scenarios
        ## Personal Reflection Prompts

        Source: {page_title} | Date: {process_date} | URL: {url}
        """

        synthesis = _gemini_generate(genai_client, [system_prompt, f"Source Content: {raw_content}"])

        # 4. Parse synthesis
        lines = [l.strip() for l in synthesis.split('\n') if l.strip()]
        raw_title = lines[0].replace('#', '').strip() if lines else "New Analysis"
        generated_title = ' '.join(raw_title.split()[:12])

        table_synthesis = synthesis[:1800].strip() + "..."

        action_items_snippet = "No specific actions identified."
        scenarios_snippet = "No scenarios identified."

        if "## Action Items" in synthesis:
            action_items_snippet = synthesis.split("## Action Items")[1].split("##")[0].strip()[:1800]
        if "## 3 'If True' Scenarios" in synthesis:
            scenarios_snippet = synthesis.split("## 3 'If True' Scenarios")[1].split("##")[0].strip()[:1800]

        # 5. Write to Notion
        new_page = _notion_request(
            "POST", "/pages", notion_token,
            json={
                "parent": {"database_id": db_id},
                "properties": {
                    "Name": {"title": [{"text": {"content": generated_title}}]},
                    "URL": {"url": url},
                    "Date": {"date": {"start": process_date}},
                    "Status": {"status": {"name": "New"}},
                    "Synthesis": {"rich_text": [{"type": "text", "text": {"content": table_synthesis}}]},
                    "Action Items": {"rich_text": [{"type": "text", "text": {"content": action_items_snippet}}]},
                    "Key Scenarios": {"rich_text": [{"type": "text", "text": {"content": scenarios_snippet}}]},
                },
                "children": _build_text_blocks(synthesis),
            }
        )

        return {"status": "success", "notion_url": new_page.get("url")}

    except ValueError as e:
        # Known user-facing errors (e.g. no YouTube transcript)
        return {"status": "error", "message": str(e)}
    except Exception as e:
        print(f"❌ Error processing {url}: {str(e)}")
        return {"status": "error", "message": "Processing failed. Check Modal logs for details."}


# ---------------------------------------------------------------------------
# Weekly synthesis rollup (runs every Sunday at 8pm UTC)
# ---------------------------------------------------------------------------

@app.function(
    image=image,
    secrets=[modal.Secret.from_name("project-synapse")],
    schedule=modal.Cron("0 20 * * 0"),
)
def weekly_rollup():
    from google import genai
    from datetime import datetime, timedelta

    notion_token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_DATABASE_ID"]

    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"📅 Weekly rollup: {seven_days_ago} → {today}")

    results = _notion_request(
        "POST", f"/databases/{db_id}/query", notion_token,
        json={
            "filter": {
                "and": [
                    {"property": "Status", "status": {"equals": "New"}},
                    {"property": "Date", "date": {"on_or_after": seven_days_ago}},
                ]
            }
        }
    ).get("results", [])

    if not results:
        print("No new entries this week — skipping rollup.")
        return

    print(f"📚 Synthesizing {len(results)} entries...")

    # Build combined input from each entry's Synthesis property
    combined = []
    page_ids = []
    sources = []  # (title, url) tuples for LinkedIn post
    for page in results:
        title = ""
        if page["properties"].get("Name", {}).get("title"):
            title = page["properties"]["Name"]["title"][0]["text"]["content"]
        url_prop = page["properties"].get("URL", {}).get("url", "")
        synthesis_blocks = page["properties"].get("Synthesis", {}).get("rich_text", [])
        synthesis_text = synthesis_blocks[0]["text"]["content"] if synthesis_blocks else ""
        if synthesis_text:
            combined.append(f"### {title}\n{synthesis_text}")
            page_ids.append(page["id"])
            sources.append((title, url_prop))

    if not combined:
        print("Entries found but no synthesis content — skipping rollup.")
        return

    rollup_prompt = f"""
    You are a Strategic Synthesis engine. Below are {len(combined)} processed learnings from the past week.

    Synthesize these into a single weekly learning digest. Be direct and specific — no filler.

    Produce these exact headers:
    ## Weekly Themes
    ## Top Epiphanies This Week
    ## Cross-Source Patterns
    ## Highest-Priority Action Items
    ## Open Questions Worth Pursuing
    ## This Week's Best 'If True' Scenario

    For each section, extract the highest-signal content across all sources. Ignore N/A sections.

    Week: {seven_days_ago} to {today}
    """

    source_block = "\n\n---\n\n".join(combined)

    genai_client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options={'api_version': 'v1beta'}
    )

    digest = _gemini_generate(genai_client, [rollup_prompt, f"Source Syntheses:\n\n{source_block}"])

    digest_title = f"Weekly Digest — {seven_days_ago} to {today}"

    # Write digest to Notion
    _notion_request(
        "POST", "/pages", notion_token,
        json={
            "parent": {"database_id": db_id},
            "properties": {
                "Name": {"title": [{"text": {"content": digest_title}}]},
                "Date": {"date": {"start": today}},
                "Status": {"status": {"name": "New"}},
                "Synthesis": {"rich_text": [{"type": "text", "text": {"content": digest[:1800]}}]},
            },
            "children": _build_text_blocks(digest),
        }
    )

    # Generate LinkedIn reading roundup draft
    # VOICE NOTE: This prompt uses a structural placeholder voice.
    # Replace the voice instruction block below with output from ~/projects/voice-engine
    # when that tool is ready.
    sources_block = "\n".join(
        f"- {title}: {url}" for title, url in sources if url
    )
    linkedin_prompt = f"""
    You are drafting a weekly LinkedIn post called "What I've been reading."

    VOICE: Direct, specific, no corporate filler. Written for GTM/CS ops leaders and
    curious professionals. No "I'm excited to share" or "game-changer" language.
    Conversational but substantive. First person. Show the thinking, not just the conclusion.

    FORMAT (modeled on Farnam Street Brain Food):
    - Opening line: one sentence that sets the week's theme or a provocative observation.
    - 4-5 "tiny thoughts": each is 2-3 sentences. Lead with the insight, end with why it matters.
      Include the source link naturally in the text (e.g. "This piece from [Title] made me think...").
    - One standout quote: brief attribution.
    - One "If True" scenario: 2-3 sentences on the most interesting speculative thread from the week.
    - Closing line: one sentence. A question or an invitation to react. No "let me know your thoughts."

    RULES:
    - Do not use headers or bullet points in the final post — LinkedIn prose only.
    - Separate each section with a blank line.
    - Total length: 200-280 words.
    - Only use insights from the provided source material. Do not invent.

    Week: {seven_days_ago} to {today}

    Sources read this week:
    {sources_block}
    """

    time.sleep(5)  # AI_RULES §1 pacing between sequential LLM calls
    linkedin_draft = _gemini_generate(genai_client, [linkedin_prompt, f"Weekly digest material:\n\n{source_block}"])
    linkedin_title = f"LinkedIn Draft — {seven_days_ago} to {today}"

    _notion_request(
        "POST", "/pages", notion_token,
        json={
            "parent": {"database_id": db_id},
            "properties": {
                "Name": {"title": [{"text": {"content": linkedin_title}}]},
                "Date": {"date": {"start": today}},
                "Status": {"status": {"name": "Draft"}},
                "Synthesis": {"rich_text": [{"type": "text", "text": {"content": linkedin_draft[:1800]}}]},
            },
            "children": _build_text_blocks(linkedin_draft),
        }
    )
    print(f"📝 LinkedIn draft written: {linkedin_title}")

    # Mark source entries as Synthesized (requires "Synthesized" status in your Notion DB)
    for page_id in page_ids:
        try:
            _notion_request(
                "PATCH", f"/pages/{page_id}", notion_token,
                json={"properties": {"Status": {"status": {"name": "Synthesized"}}}}
            )
        except Exception as e:
            print(f"⚠️ Could not update status for {page_id}: {e}")

    print(f"✅ Weekly digest written. {len(page_ids)} entries marked Synthesized.")
