# CaseSifter HANDOFF — live checkpoint

**Plan:** HANDOFF v2 (`docs/HANDOFF-web-architecture.md`) + John’s reorder (2026-10-01).

## Product decisions (John)

- **`desktop/` — frozen, delete before launch.** No further Electron feature work.
- **Local vault stays until launch.** Test with `APP_MODE=vault`. Do not break `/api/cases`.
- **Phase order:** Phase 1 → Phase 4 → launch (Phase 0 + 2 + 3).
- **Phase 0** lockdown for launch; **keep `casesifter-analysis` suspended until ready to sell.**
- **Parity eval before launch:** vault vs `/v1/jobs`+`case_id` material findings must match.

## Status

| Item | Status |
|---|---|
| Render `casesifter-analysis` | **SUSPENDED** (leave suspended until sale) |
| Phase 0 min lockdown | **Done** — Dockerfile + `resolve_app_mode()`; vault unmounted in analysis |
| Phase 1 page anchors | **Done** on this branch |
| Phase 4 | **Done** on this branch |
| `desktop/` | **Frozen — delete before launch** |

## Render / lockdown status codes

**Live service (now):** suspended by owner → all routes **503**.

**Pre-suspend (historical before lockdown):**

| Request | Code |
|---|---|
| GET `/api/health` | 200 |
| GET `/api/cases` | **200** (vault exposed) |
| POST `/api/v1/jobs` (no key) | 422 |

**After lockdown (local proof — `prove_lockdown_status_codes.py`):**

| Request | Before (`APP_MODE=vault`) | After (`APP_MODE=analysis` / `RENDER=true`) |
|---|---|---|
| GET `/api/health` | 200 (`vault`) | 200 (`analysis`) |
| GET `/api/cases` | **200** | **404** |
| POST `/api/v1/jobs` (no key) | 422 | **401** |
| POST `/api/v1/jobs` (valid key, no file) | — | **422** |

Image bakes `APP_MODE=analysis`. Empty `ANALYSIS_API_KEYS` no longer crashes boot; `/api/v1` stays 401 until keys are set for sale.

## Next

Keep Render suspended. Phase 1 already shipped — continue launch prep (Phase 2/3 + parity) when John is ready to sell.
