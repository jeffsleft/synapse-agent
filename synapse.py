import modal
import os
import time
import re
from pydantic import BaseModel
from urllib.parse import urlparse
from typing import Annotated

# --- Top-of-file constants (override via Modal secret env vars) ---
CONTENT_CAP = int(os.environ.get("SYNAPSE_CONTENT_CAP", "30000"))
TWITTER_CAP = int(os.environ.get("SYNAPSE_TWITTER_CAP", "1500"))
NOTION_BLOCK_LIMIT = 2000
MODEL = os.environ.get("SYNAPSE_MODEL", "gemini-flash-latest")

PODCAST_DOMAINS = {
    "open.spotify.com", "podcasts.apple.com", "podcasts.google.com",
    "overcast.fm", "pocketcasts.com", "anchor.fm", "buzzsprout.com",
    "simplecast.com", "podbean.com", "transistor.fm", "player.fm",
}

YOUTUBE_DOMAINS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}

# Private/metadata IPs to block (SSRF protection)
_BLOCKED_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "::1")

# Gemini backoff delays in seconds (AI_RULES §1)
_BACKOFF_DELAYS = (15, 30, 60, 120, 240)

image = modal.Image.debian_slim().pip_install(
    "google-genai",
    "notion-client",
    "requests",
    "fastapi[standard]",
    "youtube-transcript-api",
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


def _is_youtube(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in YOUTUBE_DOMAINS


def _fetch_youtube_transcript(url: str) -> tuple[str, str]:
    """Returns (transcript_text, video_title). Lazy-imports youtube-transcript-api."""
    from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

    video_id = None
    for pattern in (r"[?&]v=([a-zA-Z0-9_-]{11})", r"youtu\.be/([a-zA-Z0-9_-]{11})"):
        m = re.search(pattern, url)
        if m:
            video_id = m.group(1)
            break

    if not video_id:
        raise ValueError("Could not extract YouTube video ID from URL.")

    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
    except (NoTranscriptFound, TranscriptsDisabled):
        raise ValueError("No transcript available for this YouTube video. Try a video with auto-captions or CC enabled.")

    text = " ".join(t["text"] for t in transcript_list)
    return text[:CONTENT_CAP], f"YouTube video ({video_id})"


def _gemini_generate(client, contents: list) -> str:
    """Calls Gemini with exponential backoff on RESOURCE_EXHAUSTED (AI_RULES §1)."""
    from google.genai import errors as genai_errors

    last_exc = None
    for i, delay in enumerate(_BACKOFF_DELAYS):
        try:
            response = client.models.generate_content(model=MODEL, contents=contents)
            return response.text
        except genai_errors.ClientError as e:
            if "RESOURCE_EXHAUSTED" in str(e):
                last_exc = e
                if i < len(_BACKOFF_DELAYS) - 1:
                    print(f"⏳ Gemini rate limited, retrying in {delay}s... (attempt {i+1})")
                    time.sleep(delay)
            else:
                raise
    raise last_exc


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


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@app.function(image=image, secrets=[modal.Secret.from_name("project-synapse")])
@modal.fastapi_endpoint(method="POST")
def process_link(query: Query, x_passcode: Annotated[str | None, "Header"] = None):
    from google import genai
    from notion_client import Client
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

        # 1. Fetch content — YouTube transcript or Jina scrape
        if _is_youtube(url):
            raw_content, page_title = _fetch_youtube_transcript(url)
            print(f"🎬 YouTube transcript fetched ({len(raw_content)} chars)")
        else:
            reader_url = f"https://r.jina.ai/{url}"
            headers = {"Authorization": f"Bearer {os.environ['JINA_API_KEY']}"}
            content_res = requests.get(reader_url, headers=headers)

            page_title = content_res.headers.get("x-respond-title", "Untitled Source")
            page_title = page_title[:200].replace("\n", " ").replace("```", "")
            raw_content = content_res.text[:CONTENT_CAP]

            if "x.com" in url or "twitter.com" in url:
                raw_content = raw_content[:TWITTER_CAP]
                print("🐦 Twitter filter applied.")

        # 2. Deduplication check
        notion = Client(auth=os.environ["NOTION_TOKEN"])
        existing = notion.databases.query(
            database_id=os.environ["NOTION_DATABASE_ID"],
            filter={"property": "URL", "url": {"equals": url}}
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
        new_page = notion.pages.create(
            parent={"database_id": os.environ["NOTION_DATABASE_ID"]},
            properties={
                "Name": {"title": [{"text": {"content": generated_title}}]},
                "URL": {"url": url},
                "Date": {"date": {"start": process_date}},
                "Status": {"status": {"name": "New"}},
                "Synthesis": {"rich_text": [{"type": "text", "text": {"content": table_synthesis}}]},
                "Action Items": {"rich_text": [{"type": "text", "text": {"content": action_items_snippet}}]},
                "Key Scenarios": {"rich_text": [{"type": "text", "text": {"content": scenarios_snippet}}]}
            },
            children=_build_text_blocks(synthesis)
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
    from notion_client import Client
    from datetime import datetime, timedelta

    notion = Client(auth=os.environ["NOTION_TOKEN"])
    db_id = os.environ["NOTION_DATABASE_ID"]

    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"📅 Weekly rollup: {seven_days_ago} → {today}")

    results = notion.databases.query(
        database_id=db_id,
        filter={
            "and": [
                {"property": "Status", "status": {"equals": "New"}},
                {"property": "Date", "date": {"on_or_after": seven_days_ago}},
            ]
        }
    ).get("results", [])

    if not results:
        print("No new entries this week — skipping rollup.")
        return

    print(f"📚 Synthesizing {len(results)} entries...")

    # Build combined input from each entry's Synthesis property
    combined = []
    page_ids = []
    for page in results:
        title = ""
        if page["properties"].get("Name", {}).get("title"):
            title = page["properties"]["Name"]["title"][0]["text"]["content"]
        synthesis_blocks = page["properties"].get("Synthesis", {}).get("rich_text", [])
        synthesis_text = synthesis_blocks[0]["text"]["content"] if synthesis_blocks else ""
        if synthesis_text:
            combined.append(f"### {title}\n{synthesis_text}")
            page_ids.append(page["id"])

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
    notion.pages.create(
        parent={"database_id": db_id},
        properties={
            "Name": {"title": [{"text": {"content": digest_title}}]},
            "Date": {"date": {"start": today}},
            "Status": {"status": {"name": "New"}},
            "Synthesis": {"rich_text": [{"type": "text", "text": {"content": digest[:1800]}}]},
        },
        children=_build_text_blocks(digest)
    )

    # Mark source entries as Synthesized (requires "Synthesized" status in your Notion DB)
    for page_id in page_ids:
        try:
            notion.pages.update(
                page_id=page_id,
                properties={"Status": {"status": {"name": "Synthesized"}}}
            )
        except Exception as e:
            print(f"⚠️ Could not update status for {page_id}: {e}")

    print(f"✅ Weekly digest written. {len(page_ids)} entries marked Synthesized.")
