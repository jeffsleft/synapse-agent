# Lessons Learned

Per `AI_RULES.md` §4 — technical failures and their resolutions are logged here so future agents (Claude, Gemini CLI) don't re-introduce the same issues.

---

## 2026-10-01 — Gemini 503 overload surfaced as a generic error

### Symptom
Every submission returned `{"status":"error","message":"Processing failed. Check Modal logs for details."}`. Modal logs showed the fetch and Twitter filter succeeding, then `503 UNAVAILABLE ... This model is currently experiencing high demand`.

### Root cause
`_gemini_generate()` only caught `genai_errors.ClientError` (4xx). A 503 is a `ServerError`, so it skipped the handler and hit the catch-all in `process_link`, which returns the generic message. The outage itself was on Google's side and temporary.

### Fix
`_gemini_generate()` now catches `ServerError`: retry once on `MODEL` after 2s, then fall back to `FALLBACK_MODEL` (`gemini-flash-lite-latest`, overridable via `SYNAPSE_FALLBACK_MODEL`), then raise a `ValueError` with "Gemini is temporarily overloaded. Try again in a minute." 429 billing/rate-limit handling is unchanged and still fails fast.

### Rules
- **Handle `ServerError` separately from `ClientError` on Gemini calls.** 4xx means fix the request or billing; 5xx means transient, so retry briefly or fall back.
- **Overload is often per-model.** A fallback to a lighter model is cheaper and faster than long backoff, and keeps the call inside the iOS Shortcut's ~60s timeout (see 2026-05-29: no long backoff on this interactive endpoint).
- Check the logs for `Used fallback model` to see how often the fallback fires.

---

## 2026-05-29 — Gemini billing exhaustion caused iOS Shortcut timeouts

### Symptom
iOS Shortcut showed "The request timed out." on every submission. The endpoint never returned an error — it just hung until the Shortcut's HTTP timeout fired.

### Root cause
1. **Gemini prepaid credits depleted.** Every call returned `429 RESOURCE_EXHAUSTED` with message: `"Your prepayment credits are depleted."` This is a billing state, not a transient rate limit — no amount of waiting resolves it.
2. **Backoff loop made it worse.** `_gemini_generate()` had a `(15, 30, 60, 120, 240)` second backoff on all `RESOURCE_EXHAUSTED` errors without distinguishing billing exhaustion from rate limiting. The Modal function sat alive for 2–4+ minutes retrying a call that would never succeed, until Modal cancelled the input or the Shortcut timed out.
3. **No fast-fail path.** Neither billing exhaustion nor rate limiting returned a clean error to the caller — both silently burned wait time.

### Fix

**`synapse.py` — `_gemini_generate()`:**

Removed the backoff loop entirely. Replaced with a single try/except that fails fast on all `RESOURCE_EXHAUSTED` errors, with distinct messages for billing vs rate limiting:

```python
def _gemini_generate(client, contents: list) -> str:
    from google.genai import errors as genai_errors
    try:
        response = client.models.generate_content(model=MODEL, contents=contents)
        return response.text
    except genai_errors.ClientError as e:
        error_str = str(e)
        if "RESOURCE_EXHAUSTED" in error_str:
            if any(kw in error_str.lower() for kw in ("credits", "prepayment", "billing")):
                raise ValueError("Gemini credits depleted. Top up at https://aistudio.google.com/app/plan")
            raise ValueError("Gemini is rate limited. Wait a few seconds and try again.")
        raise
```

Also removed the now-unused `_BACKOFF_DELAYS` constant.

**Modal secret `project-synapse`:** `GEMINI_API_KEY` updated to a new free-tier key via Modal dashboard (not `--force`). Old paid key deactivated.

### Why the backoff was wrong for this tool
Synapse is an interactive personal tool triggered by an iOS Shortcut with a ~60s HTTP timeout. Exponential backoff (up to 4 minutes total) is appropriate for unattended batch pipelines where eventual success matters more than response time. For an interactive tool, the right behavior is: fail fast, return a clean error, let the user retry. The Shortcut can handle a clean error message; it cannot handle a 90-second hang.

### Verification
- `modal deploy synapse.py` — deployed clean
- Submitted a URL via Shortcut — processed successfully with new free-tier key
- Rate-limit and billing-depletion errors now return immediately with actionable messages

### Postscript
**Billing exhaustion vs rate limit detection:** Gemini returns `429 RESOURCE_EXHAUSTED` for both billing exhaustion and free-tier rate limiting. The distinguishing signal is the message body: billing exhaustion contains `"credits"`, `"prepayment"`, or `"billing"`. Rate limiting does not. Check `str(e).lower()` for these keywords before deciding whether to retry or fail fast.

**Free tier is sufficient for personal use.** `gemini-flash-latest` on free tier provides ~1,500 requests/day at 5–15 RPM. The rate limit was only hit because 5 URLs were submitted in under 2 minutes — unusual usage. Normal single-URL submissions never hit the limit.

**Dual-key failover (free → paid) was considered and rejected.** For a personal tool at this usage volume, it adds code complexity without meaningful benefit. Keep a small prepaid credit balance as a manual safety net for batch sessions instead.

---

## Highlights, lowlights, epiphanies (session of 2026-05-29)

### Highlights
- Root cause diagnosed entirely from Modal logs in under 60 seconds — no guessing
- Fix was clean: deleted the backoff loop rather than patching it, resulting in simpler code
- Correct distinction drawn between "retry makes sense" (transient rate limit) and "retry is pointless" (billing exhaustion) — both now handled correctly

### Lowlights
- Backoff logic from the 2026-05-23 session was designed for batch/unattended workloads but applied to an interactive endpoint without questioning the fit. Should have been flagged at the time.

### Epiphanies
- **Backoff is for unattended pipelines, not interactive endpoints.** When a human is waiting for a response, fail fast and let them retry. Backoff buries the real error under wait time and turns a quick fix into a timeout.
- **`RESOURCE_EXHAUSTED` is not one error — it's two.** Rate limiting resolves itself with time; billing exhaustion does not. Any retry logic on `RESOURCE_EXHAUSTED` must check the message body to distinguish them.

---

## 2026-05-25 — Firecrawl added as third-tier fetch fallback

### What changed
Added `firecrawl-py` as a fallback scraper after Jina for JS-rendered pages. No failures this session — clean design and deploy on first try.

### Decision
AI_RULES §2 prescribes the standard chain: plain → Jina → Firecrawl. Synapse accepts "any URL" so JS-rendered targets (Substack, some podcast episode pages, newsletter archives) will occasionally return thin Jina output. Firecrawl closes that gap. Free tier is 500 scrapes/month — adequate for a personal tool.

### Implementation

**`synapse.py`** — two changes:

1. **`firecrawl-py` added to `pip_install`** in the Modal image definition
2. **Fetch chain restructured** — Jina failure no longer immediately raises `ValueError`; instead, falls through to a Firecrawl block:

```python
# Fires when raw_content < 300 chars after Jina (or Jina returned non-200)
from firecrawl import FirecrawlApp
fc = FirecrawlApp(api_key=os.environ["FIRECRAWL_API_KEY"])
fc_result = fc.scrape_url(url, formats=["markdown"])
if fc_result and fc_result.markdown:
    raw_content = fc_result.markdown[:CONTENT_CAP]
    # also extracts page title from fc_result.metadata["title"] if present
```

Threshold: `< 300 chars` after Jina (matches AI_RULES §2). If Firecrawl also fails, raises `ValueError` with user-facing message.

**Modal secret `project-synapse`** — `FIRECRAWL_API_KEY` added via dashboard (not `--force`), preserving all existing keys per AI_RULES §3.

### Pattern: adding a key to an existing Modal secret
Use the Modal dashboard (modal.com → Secrets → [name] → Edit) to add a single key. Never `modal secret create --force` — that wipes every key not explicitly passed in the same command.

### Verification
- `modal deploy synapse.py` — clean first deploy, `firecrawl-py-4.28.0` installed in image
- Endpoint live: `https://jeffsleft--synapse-agent-process-link.modal.run`
- Firecrawl not yet triggered in production (fires only on thin-content URLs)

---

## Highlights, lowlights, epiphanies (session of 2026-05-25)

### Highlights
- Clean session: architectural decision → code change → deploy with no errors
- Firecrawl integration is minimal (~15 lines) and zero-cost on most requests

### Lowlights
- None

### Epiphanies
- **Firecrawl is a scrape budget, not a drop-in replacement.** 500 free scrapes/month means it must stay a fallback, not a first-pass fetcher. The current chain (plain → Jina → Firecrawl) preserves that discipline structurally — Firecrawl can only fire after both prior tiers fail.

---

## 2026-05-23 — Security hardening, feature expansion, and first submission UI

### Symptom
gemini-researcher and gemini-security-reviewer flagged:
- Two CRITICAL security issues: unauthenticated endpoint accepted arbitrary URLs (SSRF risk), error handler returned `str(e)` leaking env var names and stack traces to callers
- Silent failures on unsupported URL types (podcasts, PDFs created garbage Notion entries with no signal)
- No deduplication — same URL submitted twice created two Notion notes
- Model and content limits hardcoded (AI_RULES §1 violation)
- No Gemini backoff — RESOURCE_EXHAUSTED 429 crashed the function immediately (AI_RULES §1 violation)

### Root cause
- `process_link` passed the raw user-supplied URL directly to Jina with no protocol or host validation
- `except Exception as e: return {"status": "error", "message": str(e)}` exposed internals on any crash
- No URL type detection — Spotify, Apple Podcasts, `.pdf` links all reached Jina, returned thin/empty content, still wrote a Notion entry
- Notion query for existing URL was never performed before create
- `model="gemini-flash-latest"` hardcoded on line 77; content caps were magic numbers inline

### Fix

**`synapse.py`** — all changes in one pass:

1. **SSRF protection** — `_validate_url()` enforces `http/https` scheme, rejects `localhost`, `127.0.0.1`, `0.0.0.0`, `169.254.169.254`, `::1` before any Jina call
2. **Unsupported URL detection** — `_detect_unsupported()` returns user-facing message for podcast domains and `.pdf` paths; caller returns `{"status": "unsupported", ...}` immediately
3. **Error sanitization** — `except Exception` now logs full error to Modal stdout, returns generic `"Processing failed. Check Modal logs for details."` to caller; `ValueError` (known user-facing errors like "no YouTube transcript") surfaces its message directly
4. **Page title sanitization** — Jina `x-respond-title` header truncated to 200 chars, newlines and backticks stripped before entering system prompt
5. **Deduplication** — Notion `databases.query` with `filter: URL equals {url}` runs before any Gemini call; returns `{"status": "duplicate", "notion_url": ...}` if found
6. **Constants externalized** — `CONTENT_CAP`, `TWITTER_CAP`, `NOTION_BLOCK_LIMIT`, `MODEL` moved to top-of-file with `os.environ.get()` overrides
7. **Gemini backoff** — `_gemini_generate()` wraps all Gemini calls, catches `google.genai.errors.ClientError` with `RESOURCE_EXHAUSTED`, retries with delays `(15, 30, 60, 120, 240)` before re-raising
8. **YouTube transcript support** — `_is_youtube()` detects YouTube domains; `_fetch_youtube_transcript()` extracts video ID via regex, calls `YouTubeTranscriptApi.get_transcript()`, raises `ValueError` with clean message if captions disabled
9. **Weekly rollup cron** — `weekly_rollup()` Modal function on `Cron("0 20 * * 0")`: queries Notion for "New" entries in past 7 days, synthesizes via Gemini into a cross-source digest, writes "Weekly Digest" entry, updates source entries to "Synthesized" status
10. **Optional passcode auth** — wired in via `SYNAPSE_PASSCODE` env var; inactive until secret is set, no breaking change

**New file: `synapse-submit.html`** — self-contained form (no server), endpoint URL + passcode saved to localStorage, handles all response status codes (`success`, `duplicate`, `unsupported`, `error`) with color-coded feedback.

### Why all steps were needed
- SSRF fix alone doesn't prevent silent failures on unsupported types — those need their own early-return path
- Deduplication requires the Notion client, so it must run after auth but before the expensive Gemini call
- Backoff only helps if errors are caught by type — the generic `except Exception` pattern swallowed 429s without retry

### Verification
- `python3 -c "import synapse; print('OK')"` — passes
- `modal deploy synapse.py` — deployed clean, both `process_link` and `weekly_rollup` created
- Endpoint live: `https://jeffsleft--synapse-agent-process-link.modal.run`

### Postscript
- **"Synthesized" Notion status must be created manually** — Notion status options are defined per-database; if "Synthesized" doesn't exist, the rollup's `pages.update()` call fails with 400 per entry (caught and logged, not fatal). Add it via Notion DB settings before the first Sunday rollup fires.
- `youtube-transcript-api` has no auth requirement for public videos but returns `TranscriptsDisabled` or `NoTranscriptFound` for videos without captions — both are caught and surfaced as user-facing `ValueError`.

---

## Highlights, lowlights, epiphanies (session of 2026-05-23)

### Highlights
- gemini-researcher + gemini-security-reviewer ran in parallel and returned actionable findings within 45 seconds each — zero manual archaeology
- All 6 quality/security fixes + 3 new features implemented in a single `synapse.py` rewrite + one new HTML file; deploy succeeded first try
- Weekly rollup cron is genuinely high-leverage: closes the "read and forget" loop automatically without any user action

### Lowlights
- Todoist task was marked "done" prematurely — podcasts and books were never actually supported; the task should have been scoped more narrowly at the start (articles/web pages only)
- No test against a real YouTube URL before deploy — transcript fetching is untested in production; first real YouTube URL submission is the functional test

### Epiphanies
- **Run gemini-researcher + gemini-security-reviewer before shipping any new endpoint.** The parallel pattern took <2 minutes and surfaced 2 CRITICAL issues that would have shipped otherwise. Make this the default pre-ship step, not a one-off.
- **Silent failures are worse than loud crashes.** Podcast URLs that produce empty Notion entries with no error signal are more damaging than a 500 — the user has no idea the pipeline failed. Always fail fast with a typed status response.

### Open follow-ups
- ~~Test YouTube transcript fetching~~ — resolved 2026-05-24: youtube-transcript-api removed, Jina handles YouTube
- ~~Confirm "Synthesized" status~~ — resolved 2026-05-24: user confirmed added
- Consider adding `SYNAPSE_PASSCODE` to Modal secret to lock the endpoint (low urgency, personal tool)
- ~~Dependency pinning~~ — resolved 2026-05-24: notion-client removed, youtube-transcript-api removed, no remaining unpinned risky deps

---

## 2026-05-24 — youtube-transcript-api removed entirely; Jina handles all URL types

### Symptom
YouTube URLs failed across multiple patch attempts: `get_transcript` attribute error (v1.x), then `xml.etree.ElementTree.ParseError: no element found` (v0.x, empty response from YouTube), then exception escaping all try/except blocks. Six deploys, no stable fix.

### Root cause
`youtube-transcript-api` is fundamentally unreliable:
- v1.x dropped the `get_transcript` class method entirely
- v0.x raises `ParseError` (not `NoTranscriptFound`) when YouTube returns empty XML — this escaped specific exception handlers
- The library's internal error handling is brittle and unpredictable across versions
- YouTube transcript extraction was never a stated project requirement — it was added reactively

### Fix
Removed `youtube-transcript-api` from `pip_install` entirely. Deleted `_is_youtube()`, `_fetch_youtube_transcript()`, and all YouTube-specific branching. YouTube URLs now route through Jina exactly like every other URL — no special path. Jina extracts title, description, and visible page text, which is sufficient for synthesis.

### Rule
**Do not re-add `youtube-transcript-api` or any YouTube transcript library to this project.** Jina handles YouTube URLs. Full transcript extraction is out of scope — the project synthesizes written content from URLs; page metadata is sufficient.

### Meta-lesson
**Stop patching a flaky third-party library. Ask whether the library is needed at all.** Three rounds of exception-handling patches failed before the right question was asked: "is this library necessary?" It wasn't. Deleting it took 10 minutes and solved the problem permanently. When a dependency causes repeated failures, removal is usually faster than fixing.

### Agent usage note
Spawning subagents via the `Agent` tool uses Claude tokens, not Gemini. To use actual Gemini (agy CLI), invoke via Bash per AI_RULES §12. Clarify which is being used before delegating expensive analysis work.

---

## 2026-05-24 — notion-client SDK churn broke Notion writes

### Symptom
`{"status":"error","message":"Processing failed. Check Modal logs for details."}` on every submission. Modal logs: `'DatabasesEndpoint' object has no attribute 'query'`.

### Root cause
`notion-client` was unpinned so Modal pulled the latest version on every image rebuild. Both v3.1.0 and v2.7.0 had removed `databases.query()` — the method used for dedup checks and page creation.

### Fix
Dropped `notion-client` from `pip_install` entirely. Replaced all SDK calls with direct Notion REST API calls via a `_notion_request(method, path, token, **kwargs)` helper using `requests` (already in deps). Immune to SDK version churn.

### Rule
**Never use the `notion-client` SDK in this project.** Use `_notion_request()` for all Notion API calls. The Notion REST API is stable; the Python SDK is not.

---
