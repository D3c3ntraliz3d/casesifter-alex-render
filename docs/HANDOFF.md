# CaseSifter HANDOFF — live checkpoint

**Plan:** HANDOFF v2 (`docs/HANDOFF-web-architecture.md`) + John’s reorder (2026-10-01).

## Product decisions (John)

- **`desktop/` — frozen, delete before launch.** No further Electron feature work.
- **Local vault stays until launch.** Test with `APP_MODE=vault`. Do not break `/api/cases`.
- **Phase order:** Phase 1 → Phase 4 → launch (Phase 0 + 2 + 3).
- **Keep `casesifter-analysis` suspended until ready to sell.**
- **Parity eval before launch:** vault vs `/v1/jobs`+`case_id` material findings must match.

## Status

| Item | Status |
|---|---|
| Render `casesifter-analysis` | **SUSPENDED** (leave suspended until sale) |
| Phase 0 min lockdown | **Done** |
| Phase 1 page anchors | **Done** |
| Phase 4 extraction | **Done** (regex baseline + additive AI; fixtures; token log) |
| `desktop/` | **Frozen — delete before launch** |
| Marketing CTAs (Download → Sign up) | **Done on Render** `casesifter-marketing.onrender.com` |
| Custom domain `www.casesifter.com` | **STALE — SiteGround** (A `34.174.53.153`, NS `ns1/ns2.siteground.net`, SiteGuard). John’s private-browser screenshot is this host, not Render. Fix: attach domain to Render **or** re-upload `marketing/` via SiteGround File Manager — see `docs/RENDER_HYBRID.md`. Until then open `https://casesifter-marketing.onrender.com`. |

## Phase 4 (conditions met)

- Regex/graph extraction stays the baseline; Gemini facts are **additive**, `confidence` capped (72), labeled **`ai_generated=True`**.
- Fixtures in `planted_bs_cases.json`: `summers_employment_start_cross_doc` (2019/2022 two-doc), three rephrasing TPs, clean twin — `run_gotcha_eval.py` must stay `all_pass`.
- Gemini token use for the new step is logged per document (`ai_fact_extract[name] tokens …`) and stored on the report at `comparison.ai_fact_extract_usage` / `comparison.gemini_usage.ai_fact_extract`.

### John’s local 11467-2007 re-run checklist

After pulling this branch (`APP_MODE=vault`), rescan the case and confirm:

1. **Still red / still catch all 3 forged exhibits** (National Grid historical, wrong-era/chrome, statement math or too-crisp / edited-exhibit cards — same material interrupts as before).
2. **Cross-doc / gotcha cards show `evidence.refs`** with a **page number on both documents** when two docs are cited (open any employment / occupancy / DOB-style conflict).
3. **AI facts (if `GEMINI_API_KEY` set):** any new fact from the LLM path is marked `ai_generated` and does not replace regex facts; report `comparison.ai_fact_extract_usage` shows `prompt_tokens` / `total_tokens` / `facts_added` for that document (also in server logs as `ai_fact_extract[…] tokens …`).
4. **No silent drop:** list any prior material finding that disappeared or changed severity; paste titles if something moved.
5. **Cost glance:** sum `ai_fact_extract_usage.total_tokens` across rescanned exhibits — that is the Phase 4 extraction add-on cost only (separate from multimodal/chrome).

## Pre–Phase 4 checklist (completed)

1. Local vault: **GET `/api/cases` → 200** with `APP_MODE=vault`; compose defaults vault.
2. Analysis CORS env-driven, not `*`.
3. Gotcha `all_pass`; page refs both sides; forged lanes still caught.
4. Suspended Render: no disk listing possible; expect no persisted case files.
5. Launch next (still open): control-plane argon2/bcrypt + session expiry; `GEMINI_TIER` refuse `free` in production.

## Render lockdown status codes (reference)

| Request | Before (vault exposed) | After (`analysis`) |
|---|---|---|
| GET `/api/health` | 200 | 200 (`analysis`) |
| GET `/api/cases` | **200** | **404** |
| POST `/api/v1/jobs` (no key) | 422 | **401** |

Live now: **503** (suspended).

## Next

John re-runs 11467 locally using the checklist above → keep Render suspended → launch build (Phase 2/3 + parity + control-plane / `GEMINI_TIER`) when ready to sell.
