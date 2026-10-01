# CaseSifter HANDOFF — live checkpoint

**Plan:** HANDOFF v2 (`docs/HANDOFF-web-architecture.md`) + John’s reorder (2026-10-01).

## Product decisions (John)

- **`desktop/` — frozen, delete before launch.** No further Electron feature work.
- **Local vault stays until launch.** Test with `APP_MODE=vault`. Do not break `/api/cases`.
- **Phase order:** Phase 1 → Phase 4 → launch (Phase 0 + 2 + 3).
- **Phase 0** only if Render analysis is live (min: `APP_MODE` + CORS + `/api/v1` auth).
- **Parity eval before launch:** vault vs `/v1/jobs`+`case_id` material findings must match.

## Status

| Item | Status |
|---|---|
| Render `casesifter-analysis` | **LIVE** — `https://casesifter-analysis.onrender.com` |
| Phase 0 min lockdown | Shipping with this branch |
| Phase 1 page anchors | Shipping with this branch |
| Phase 4 | After Phase 1 gates |
| `desktop/` | **Frozen — delete before launch** |

## Render before → after (Phase 0)

**Before (unauthenticated):**

| Request | Code |
|---|---|
| GET `/api/health` | 200 |
| GET `/api/cases` | **200** (vault exposed) |
| POST `/api/upload` | 422 |
| POST `/api/v1/jobs` | 422 (open entitlement) |

**After deploy (`APP_MODE=analysis`):** vault routes 404; `/api/v1/*` without `X-API-Key` → 401. Re-probe and update this table.

## Next

Phase 1 gates (gotcha all_pass + 11467 with pages) → Phase 4.
