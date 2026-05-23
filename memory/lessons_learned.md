# Lessons Learned

Per `AI_RULES.md` §4 — technical failures and their resolutions are logged here so future agents (Claude, Gemini CLI) don't re-introduce the same issues.

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
- Test YouTube transcript fetching against a real URL in production
- Confirm "Synthesized" status added to Notion DB before Sunday rollup fires
- Consider adding `SYNAPSE_PASSCODE` to Modal secret to lock the endpoint (low urgency, personal tool)
- Dependency pinning still not done (Informational from security review) — low priority but worth doing before any public sharing

---
