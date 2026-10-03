# CaseSifter HANDOFF — soft-launch checkpoint

**Plan:** Sell soft-launch MVP + HANDOFF v2 (`docs/HANDOFF-web-architecture.md`).

## Product decisions (John)

- **`desktop/` — frozen, delete before broad launch.** No further Electron feature work.
- **Local vault stays for John’s testing** (`APP_MODE=vault`). Do not break `/api/cases` locally.
- **Soft-launch now** (invite beta): marketing signup → hosted web app → Stripe trial → unsuspend analysis with Phase 0 lockdown.
- **Hard gate before first paid invoice:** Phase 2 `case_id` memory so cross-doc gotchas are durable across browser sessions.
- Out of scope for soft-launch: full Phase 2 Postgres, Phase 3 folder picker, deleting `desktop/`, control-plane argon2.

## Soft-launch status

| Item | Status |
|---|---|
| Marketing signup UX + **Open web app** CTA | **Done** (this branch) → `https://app.casesifter.com` |
| Render `casesifter-app` (frontend SPA) | **Blueprint ready** — John: sync Blueprint / deploy + attach `app.casesifter.com` |
| Browser Account (signup/login/checkout → control plane) | **Done** (`VITE_CONTROL_PLANE_URL`) |
| Control invite gate + Stripe env in `render.yaml` | **Documented** — John sets secrets |
| Render `casesifter-analysis` | **SUSPENDED** — John resumes after secrets/CORS; code/docs ready |
| Phase 0 lockdown | **Done** (`APP_MODE=analysis`, CORS not `*`, API key **or** active session) |
| Phase 1 page anchors / Phase 4 extraction | **Done** |
| Custom domain `www` / `app` | **BLOCKED** — needs John’s DNS / Render access |
| Phase 2 case_id memory | **Next before invoicing** |

## Soft-launch URLs

| Surface | Prefer | Fallback |
|---|---|---|
| Marketing | `https://www.casesifter.com` | `https://casesifter-marketing.onrender.com` |
| Web app | `https://app.casesifter.com` | `https://casesifter-app.onrender.com` |
| Control | `https://casesifter-control.onrender.com` | same |
| Analysis | `https://casesifter-analysis.onrender.com` | same (resume first) |

## Invite + Stripe verify path

1. Set on `casesifter-control`: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID`, `CASESIFTER_INVITE_REQUIRED=true`, `CASESIFTER_INVITE_CODES=…`, `CASESIFTER_PUBLIC_URL=https://app.casesifter.com`.
2. Sign up on marketing with password + terms + invite → success → **Open web app**.
3. In Account: sign in if needed → **Start 14-day trial** → Stripe Checkout.
4. Webhook: `https://casesifter-control.onrender.com/v1/webhooks/stripe`.
5. Demo shortcut: admin `POST /v1/trial` with `CONTROL_ADMIN_TOKEN`.

## Unsuspend analysis (John)

1. Confirm `ANALYSIS_API_KEYS` and/or `CLOUD_CONTROL_URL` set; CORS includes app + www + apex (see `render.yaml`).
2. Dashboard → Resume `casesifter-analysis`.
3. Expect: `GET /api/health` → 200; `POST /api/v1/jobs` without auth → **401**; entitled session or API key can create jobs.
4. Until resume: live health is **503**.

## DNS (John)

| Host | Type | Target |
|---|---|---|
| `www` | CNAME | `casesifter-marketing.onrender.com` |
| `app` | CNAME | `casesifter-app.onrender.com` |
| `@` | A | Render LB IP from Dashboard |

Details: `docs/RENDER_HYBRID.md`.

## What John must provide

- Render dashboard access or `RENDER_API_KEY` (deploy `casesifter-app`, custom domains, unsuspend analysis, set secrets)
- Stripe keys + price id
- Invite codes for pilot firms (`CASESIFTER_INVITE_CODES`)
- SiteGround DNS (or registrar) for `www` + `app`

## Next before invoicing

**Phase 2 `case_id` memory** — durable firm case folders so cross-doc gotchas work on `/v1/jobs` with `case_id` across sessions. Soft-launch can demo single-doc forensics + Account/Stripe; foreclosure buyers will expect multi-doc history.

## Phase 4 (conditions met)

- Regex/graph extraction stays the baseline; Gemini facts are **additive**, `confidence` capped (72), labeled **`ai_generated=True`**.
- Fixtures in `planted_bs_cases.json`; `run_gotcha_eval.py` must stay `all_pass`.
- Token use logged per document (`ai_fact_extract[…]`) and on report `comparison.ai_fact_extract_usage`.

### John’s local 11467-2007 re-run checklist

After pulling this branch (`APP_MODE=vault`), rescan and confirm forged-exhibit catches, page refs on both docs for cross-doc cards, AI facts marked `ai_generated`, and no silent drop of prior material findings.

## Render lockdown status codes (reference)

| Request | Before (vault exposed) | After (`analysis`) |
|---|---|---|
| GET `/api/health` | 200 | 200 (`analysis`) |
| GET `/api/cases` | **200** | **404** |
| POST `/api/v1/jobs` (no key/session) | 422 | **401** |

Live while suspended: **503**.
