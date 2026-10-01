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
| Phase 1 page anchors | **Done** — evidence below |
| Phase 4 | Ready after this gate checklist |
| `desktop/` | **Frozen — delete before launch** |

## Pre–Phase 4 checklist (2026-10-01)

### 1. Local vault (`APP_MODE=vault`)

- `docker-compose.yml` overrides Dockerfile bake-in: `APP_MODE: ${APP_MODE:-vault}`
- Local run: `APP_MODE=vault APP_ENV=development PYTHONPATH=. uvicorn app.main:app --host 127.0.0.1 --port 8000`
- **GET `/api/cases` → 200** (`{"cases":[]}`, `app_mode: vault`)

### 2. CORS in analysis mode (env-driven, not `*`)

```
CORS_ALLOW_ORIGINS=https://casesifter.com,https://www.casesifter.com
resolved_allow_origins=["https://casesifter.com","https://www.casesifter.com"]
contains_star=false
```

Also baked in `backend/Dockerfile` / `render.yaml` (casesifter.com + www + marketing). Local compose keeps `CORS_ALLOW_ORIGINS=${CORS_ALLOW_ORIGINS:-*}`.

### 3. Phase 1 evidence

**`run_gotcha_eval.py`:** every section `all_pass: True` (PIPELINE 1.0/1.0; LIE DETECTOR 1.0/1.0 misses=[]; CASE IMPACT all_pass including `national_grid_historical_fixture` + `ai_print_scan_fixtures`).

**Cross-doc findings with `evidence.refs` pages on BOTH docs** (11467 PDFs not in this cloud workspace — local multi-page proof):

1. Employment 2019 vs 2022 — Loan app **p.2** ↔ VOE **p.2**
2. Began-employment vs Employed-since 2022 — Updated VOE **p.3** ↔ VOE **p.2**
3. DOB conflict — Passport **p.2** ↔ Borrower application **p.2**

**Three forged-exhibit lanes still caught** (CASE IMPACT fixtures): National Grid Brooklyn Apr 2007 historical; wrong-era logo/chrome; too-crisp AI print-scan.

### 4. Suspended Render disk (`data/cases/`, `data/jobs/`)

- Live probe: **503 Service Suspended** — no shell/API access to list files.
- `render.yaml` has **no persistent disk**; `docs/RENDER_HYBRID.md` says scratch only for in-flight jobs.
- **File list:** unavailable while suspended. Expect **none persisted** (ephemeral FS). John: if you unsuspend briefly to wipe, check `/app/data/cases/` and `/app/data/jobs/` — names only — then re-suspend.

### 5. Next for launch (still open from Phase 0)

- Control-plane **password hashing** (move off SHA-256+salt → argon2/bcrypt; rehash on next login)
- Control-plane **session expiry** (e.g. 14 days) + logout invalidation
- **`GEMINI_TIER=paid|vertex|free`** — refuse `free` when `APP_ENV=production`

## Render lockdown status codes (reference)

| Request | Before (vault exposed) | After (`analysis`) |
|---|---|---|
| GET `/api/health` | 200 | 200 (`analysis`) |
| GET `/api/cases` | **200** | **404** |
| POST `/api/v1/jobs` (no key) | 422 | **401** |

Live now: **503** (suspended).

## Next

Phase 4 (if not already merged from this branch) → keep Render suspended → launch build (Phase 2/3 + parity + control-plane / GEMINI_TIER items above) when ready to sell.
