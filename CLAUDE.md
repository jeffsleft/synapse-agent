# Synapse Agent — Project Instructions

## What This Is

A learnings-synthesis pipeline. Accepts any URL — article, podcast page, tweet thread, essay — and produces a structured note in Notion ready to review or roll into a weekly synthesis. Single-function deployment. Notion is the UI; no separate frontend.

## Architecture

```
POST /process-link  →  Modal Python function (synapse.py)
                            │
                            ├──  Jina Reader (scrape) — see AI_RULES §2
                            ├──  Gemini Flash (synthesize) — see AI_RULES §1
                            └──  Notion API (persist) — see AI_RULES §8
```

## File Layout

- `synapse.py` — entire app, single FastAPI endpoint
- `README.md` — public-facing description

## Project-Specific Decisions

1. **Literalist guardrails in the Gemini system prompt.** Explicit rules: punchy title, never invent context, ignore sidebar noise, mark N/A when content is thin. Prevents hallucination on short/noisy input that's common in this app's input stream.
2. **Twitter/X sidebar noise filter.** For `x.com` or `twitter.com` URLs, content is further truncated to 1500 chars (after the §2 general 30k cap) — drops "Who to follow" and trending noise that otherwise dominates short tweet content.
3. **8-section output structure**, hardcoded in the prompt: Executive Summary, Epiphanies, Core Concepts, Action Items, Follow-up Questions, Controversial Opinions, 3 'If True' Scenarios, Personal Reflection Prompts. Every note has the same shape so they aggregate cleanly across the weekly synthesis.

## Secrets (Modal secret `project-synapse`)

`JINA_API_KEY`, `GEMINI_API_KEY`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`

## Notion DB Schema (required)

`Name` (title), `URL` (url), `Date` (date), `Status` (status), `Synthesis` (rich text), `Action Items` (rich text), `Key Scenarios` (rich text).

## Deployment

```bash
modal deploy synapse.py
```

## Gotchas

- If Gemini returns a response without expected `##` section headers, the `action_items_snippet` and `scenarios_snippet` fallbacks kick in ("No specific actions identified."). Don't strip those — they prevent crashes on thin inputs.
