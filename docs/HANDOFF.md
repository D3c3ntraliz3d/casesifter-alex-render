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

| Request | Before | Now (`90c5501` live) | Target (`APP_MODE=analysis`) |
|---|---|---|---|
| GET `/api/health` | 200 | 200 (`app_mode: vault`) | 200 (`app_mode: analysis`) |
| GET `/api/cases` | **200** | **200** | **404** |
| POST `/api/v1/jobs` (no key) | 422 | 422 | **401** |

Code for lockdown is on the tip; live env still has `app_mode: vault`. Sync `APP_MODE=analysis` from `render.yaml` / dashboard to finish.

## Gates (this branch)

| Gate | Result |
|---|---|
| `run_gotcha_eval.py` before | `all_pass` |
| `run_gotcha_eval.py` after Phase 1 | `all_pass` |
| Cross-doc refs have pages both sides | Pass (Summers local) |
| 11467 PDFs in workspace | **Missing** — CASE IMPACT forged-exhibit fixtures still pass |
| Phase 4 Summers cross-doc + rephrasings | Shipping |

## Next

Phase 4 complete on this branch → launch build (Phase 0 remainder if needed + 2 + 3) after John confirms Render after-codes.
