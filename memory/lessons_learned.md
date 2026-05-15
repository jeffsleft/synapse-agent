# Lessons Learned

Technical failures and their resolutions, per AI_RULES §4. Append new entries at the top.

---

## 2026-05-15 — Hardcoded Gemini model version (AI_RULES §1 violation)

**Problem:** `synapse.py:77` had `model="gemini-3-flash-preview"` — a hardcoded version string. AI_RULES §1 prohibits this exact pattern because specific version strings cause 404s in some regions.

**How it was caught:** Surfaced while writing the project's `CLAUDE.md` and auditing for AI_RULES violations. Wasn't causing user-visible errors in Jeff's region — but would silently break for anyone in a region where that preview model wasn't deployed.

**Resolution:** Switched to the stable alias `model="gemini-flash-latest"`. Redeployed to Modal, smoke-tested (HEAD: 405, bad POST: 422 with proper validation — endpoint healthy).

**Future agents:** Always use `gemini-pro-latest` / `gemini-flash-latest`. Never paste a model version from Google's release notes directly into code, even if it's the "newest" — Google's region rollouts are uneven and aliases handle it.
