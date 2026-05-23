# Synapse Agent — Wins

A running record of what shipped, what worked, and the story arc. For roll-up and career storytelling later.

---

## The Project

A learnings-synthesis pipeline. Drop in any URL (article, podcast, essay, tweet thread) → get a structured 8-section Notion note back, ready to review or roll into a weekly synthesis. Single-function Modal deployment; no frontend.

---

## Wins

### Initial Build
- Built and deployed a single-endpoint Modal function (`POST /process-link`) that handles the full pipeline: scrape via Jina Reader → synthesize via Gemini Flash → persist to Notion
- 8-section output structure hardcoded in the prompt — every note has the same shape, so they aggregate cleanly across a weekly synthesis: Executive Summary, Epiphanies, Core Concepts, Action Items, Follow-up Questions, Controversial Opinions, 3 'If True' Scenarios, Personal Reflection Prompts
- Literalist guardrails prevent hallucination on thin/noisy input — explicit prompt rules: punchy title, never invent context, ignore sidebar noise, mark N/A when content is thin
- Twitter/X sidebar noise filter: content truncated to 1500 chars for social URLs to drop "Who to follow" and trending noise
- Zero frontend — Notion is the UI

---

### Security hardening + feature expansion (2026-05-23)
- SSRF blocked: URL validation enforces http/https only, rejects localhost/metadata IPs before any Jina call
- Error sanitization: except block no longer leaks env var names or stack traces to callers
- Page title sanitized to prevent prompt injection via Jina metadata headers
- Unsupported URL detection: podcast domains and .pdf links return explicit rejection instead of creating garbage Notion entries
- Deduplication: Notion queried before processing — duplicate URLs return the existing page link, no wasted API calls
- Gemini backoff: exponential retry (15, 30, 60, 120, 240s) on RESOURCE_EXHAUSTED — now AI_RULES §1 compliant
- YouTube support: full transcript fetching via `youtube-transcript-api` — talks, interviews, and conference sessions now supported
- Weekly synthesis rollup: new Modal cron (Sundays 8pm UTC) reads all "New" entries from the past 7 days, produces a cross-source digest in Notion, marks entries Synthesized
- Submission UI: `synapse-submit.html` — self-contained form, paste URL + hit Enter, get Notion link back; no server required; endpoint and passcode saved to localStorage

---

## The "So What"

Replaced the "read and forget" pattern with a structured capture-and-synthesize pipeline. Every URL that goes in comes out as a consistent, reviewable note with actionable items and reflection prompts. Built in a single session; deployed and running.
