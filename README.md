# Synapse Agent

A learnings-synthesis pipeline. Drop in any article, podcast page, tweet thread, or essay URL — Synapse scrapes the content, runs it through Gemini for strategic analysis, and writes a structured note to Notion that's ready to review, share, or roll into a weekly synthesis.

## What it does

- Accepts a URL via HTTP POST
- Scrapes clean readable content via [Jina Reader](https://jina.ai/reader/) (no Chrome rendering required)
- Filters out Twitter/X sidebar noise so analyses stay on the primary content
- Synthesizes the source with Gemini 3 Flash using literalist guardrails (no invented context, no hallucinated philosophy)
- Produces a structured note with 8 sections: Executive Summary, Epiphanies/Learnings, Core Concepts, Action Items, Follow-up Questions, Controversial Opinions, 3 'If True' Scenarios, Personal Reflection Prompts
- Writes the result to a Notion database with title, URL, date, status, table-view snippets, and full deep-dive page content

## Architecture

```
POST /process-link  →  Modal Python function
                            │
                            ├──  Jina Reader (scrape)
                            ├──  Gemini 3 Flash (synthesize)
                            └──  Notion API (persist)
```

Single-function deployment. No database, no frontend — Notion is the UI.

## Stack

- **Compute:** [Modal](https://modal.com) (serverless Python, FastAPI endpoint)
- **Scraping:** [Jina Reader](https://jina.ai/reader/) (`r.jina.ai` proxy)
- **LLM:** Google Gemini 3 Flash via `google-genai`
- **Storage:** Notion API via `notion-client`

## Files

| File | Purpose |
|---|---|
| `synapse.py` | Modal app — single FastAPI endpoint that runs the full pipeline |

## Deployment

```bash
pip install modal
modal login
modal deploy synapse.py
```

Requires a Modal secret named `project-synapse` containing:

- `JINA_API_KEY`
- `GEMINI_API_KEY`
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`

The target Notion database must have these properties: `Name` (title), `URL` (url), `Date` (date), `Status` (status), `Synthesis` (rich text), `Action Items` (rich text), `Key Scenarios` (rich text).

## Calling it

```bash
curl -X POST https://<your-modal-endpoint>.modal.run \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/some-article"}'
```

Response: `{"status": "success", "notion_url": "https://www.notion.so/..."}`

## Why this exists

Reading volume scales; synthesis doesn't. Synapse closes the gap between consuming a piece and having a structured note you can actually act on or aggregate. Built to feed a weekly learnings review without requiring a manual write-up step per source.

---

*Built with [Claude Code](https://claude.ai/code).*
