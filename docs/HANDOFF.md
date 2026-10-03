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
| Marketing signup UX + **Open web app** CTA | **Done** — default CTA `https://casesifter-app.onrender.com` (probe-upgrades to `app.casesifter.com` when DNS live) |
| Marketing apex / www on Render | **Live** — `https://casesifter.com` / `www` already serve marketing |
| Render `casesifter-app` (frontend SPA) | **Deployed** — use onrender URL until `app` CNAME |
| Browser Account (signup/login/checkout → control plane) | **Done** (`VITE_CONTROL_PLANE_URL`) |
| Control invite gate + Stripe env in `render.yaml` | **Runbook ready** — John sets secrets (`docs/STRIPE.md`); live `/health` still `"stripe":false` until keys |
| Render `casesifter-analysis` | **SUSPENDED** — John resumes after secrets/CORS; code/docs ready |
| Phase 0 lockdown | **Done** (`APP_MODE=analysis`, CORS not `*`, API key **or** active session) |
| Phase 1 page anchors / Phase 4 extraction | **Done** |
| Custom domain `app` | **BLOCKED** — `app.casesifter.com` NXDOMAIN until John adds CNAME + Render attach |
| Phase 2 case_id memory | **Next before invoicing** |

## Soft-launch URLs

| Surface | Prefer (today) | Notes |
|---|---|---|
| Marketing | `https://casesifter.com` / `https://www.casesifter.com` | Already on Render |
| Web app | `https://casesifter-app.onrender.com` | Avoid `app.casesifter.com` until DNS |
| Control | `https://casesifter-control.onrender.com` | same |
| Analysis | `https://casesifter-analysis.onrender.com` | resume first |

### Interim client path (use now)

| Action | URL |
|---|---|
| Sign up | `https://casesifter.com/signup.html` (invite optional — gate off) |
| Log in / use product | `https://casesifter-app.onrender.com` → **Account → Sign in** |
| Avoid | `https://app.casesifter.com` (NXDOMAIN until `app` CNAME) |

**Cross-origin note:** a token stored on the marketing origin is **not** an app session. After signup, the client must **Sign in** on the hosted app with the same credentials.

## Invite + Stripe verify path

Full operator checklist: **`docs/STRIPE.md`**.

1. Set on `casesifter-control`: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID`, `CASESIFTER_INVITE_REQUIRED=false` (invite optional), optional `CASESIFTER_INVITE_CODES=…`, `CASESIFTER_PUBLIC_URL=https://casesifter-app.onrender.com` until custom app domain works (Blueprint default matches; override Dashboard if still `app.casesifter.com`).
2. Sign up on marketing with password + terms (invite optional) → success → **Open web app** (`casesifter-app.onrender.com`).
3. In Account: **Sign in** → pick seats → **Start 14-day trial** → Stripe Checkout at **$79/seat** (card collected; charge after trial). After Checkout, **Manage billing** opens the Customer Portal when `has_billing_portal`. Without keys, Account shows **Stripe not configured — trial started without a card (local/dev).** and Subscribe marks Pro-dev locally (no Checkout tab).
4. Webhook: `https://casesifter-control.onrender.com/v1/webhooks/stripe` — enable `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`. Enable Customer Portal + quantity updates in Stripe.
5. Redeploy control → `curl -sS https://casesifter-control.onrender.com/health` → `"stripe":true`.
6. Demo shortcut (no card): admin `POST /v1/trial` with `CONTROL_ADMIN_TOKEN`.

## Unsuspend analysis (John)

1. Confirm `ANALYSIS_API_KEYS` and/or `CLOUD_CONTROL_URL` set; CORS includes app + www + apex (see `render.yaml`).
2. Dashboard → Resume `casesifter-analysis`.
3. Expect: `GET /api/health` → 200; `POST /api/v1/jobs` without auth → **401**; entitled session or API key can create jobs.
4. Until resume: live health is **503**.

## DNS (John)

Apex / `www` are already pointing at Render marketing. **Remaining:**

| Host | Type | Target |
|---|---|---|
| `app` | CNAME | `casesifter-app.onrender.com` |

Then attach `app.casesifter.com` on the Render `casesifter-app` service. Details: `docs/RENDER_HYBRID.md`.

## What John must provide

- Invite gate is **off** (`CASESIFTER_INVITE_REQUIRED=false` in Blueprint). If live Render still has `true`, sync Blueprint or set env + redeploy `casesifter-control`. Optional `CASESIFTER_INVITE_CODES` may remain set.
- Stripe keys + `$79/seat` price `price_1UMIUnD6sOleRtjBjVbHzMdP` + webhook signing secret — see `docs/STRIPE.md` (`control` `/health` shows `"stripe":false` until `STRIPE_SECRET_KEY` **and** `STRIPE_PRICE_ID` are set). Secrets stay Render-only.
- DNS: `app` CNAME → `casesifter-app.onrender.com` + attach domain in Render
- Unsuspend `casesifter-analysis` when ready for scans
- Render dashboard access or `RENDER_API_KEY` if agent help is needed for the above

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
