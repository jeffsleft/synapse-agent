# Synapse Agent — Project Instructions

## What This Is

A learnings-synthesis pipeline. Accepts any URL — article, podcast page, tweet thread,
essay — and produces a structured note in Notion ready to review or roll into a
weekly synthesis. Single-function deployment. Notion is the UI; no separate frontend.

## Architecture

```
POST /process-link  →  Modal Python function (synapse.py)
                            │
                            ├──  Jina Reader (scrape)
                            ├──  Gemini Flash (synthesize)
                            └──  Notion API (persist)
```

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Compute | Modal serverless | Per AI_RULES §3 |
| Scraping | Jina Reader (`r.jina.ai`) | Clean readable content, no Chrome rendering needed |
| LLM | Gemini Flash via `google-genai` | Fast, cheap, sufficient for structured synthesis |
| Storage | Notion DB | Humans read here — no separate UI needed |

## File Layout

- `synapse.py` — entire app, single FastAPI endpoint
- `README.md` — public-facing description

## Design Decisions

1. **Jina Reader, not BeautifulSoup or Playwright.** No Chrome rendering, no anti-bot
   tripwires. Trade-off: depends on Jina uptime and rate limits.
2. **Literalist guardrails in the Gemini system prompt.** Explicit rules: punchy
   title, never invent context, ignore sidebar noise, mark N/A when content is thin.
   Prevents hallucination on short/noisy input.
3. **Twitter/X sidebar noise filter.** For x.com or twitter.com URLs, content is
   truncated to 1500 chars before reaching Gemini — drops "Who to follow" and
   trending noise.
4. **30k char content cap.** Sources larger get truncated. Prevents token bloat.
5. **8-section output structure**, hardcoded in the prompt. Every note has the
   same shape so they aggregate cleanly across the weekly synthesis.
6. **Notion writes use both properties and block children.** Properties populate
   the table view; block children populate the deep-dive page. Long blocks split
   at the 2000-char Notion limit.

## Secrets (Modal secret `project-synapse`)

`JINA_API_KEY`, `GEMINI_API_KEY`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`

## Notion DB Schema (required)

`Name` (title), `URL` (url), `Date` (date), `Status` (status), `Synthesis` (rich
text), `Action Items` (rich text), `Key Scenarios` (rich text).

## Deployment

```bash
modal deploy synapse.py
```

## Gotchas

- If Gemini returns a response without expected `##` section headers, the
  action_items_snippet and scenarios_snippet fallbacks kick in ("No specific
  actions identified."). Don't strip those — they prevent crashes on thin inputs.
