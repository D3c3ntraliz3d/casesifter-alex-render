# CaseSifter HANDOFF — soft-launch checkpoint

**Plan:** Sell soft-launch MVP + HANDOFF v2 (`docs/HANDOFF-web-architecture.md`).

## Product decisions (John)

- **`desktop/` — frozen, delete before broad launch.** No further Electron feature work.
- **Local vault stays for John’s testing** (`APP_MODE=vault`). Do not break `/api/cases` locally.
- **Soft-launch now** (invite beta): marketing signup → hosted web app → Stripe trial → unsuspend analysis with Phase 0 lockdown.
- **Hard gate before first paid invoice:** Persistent per-matter fact memory so cross-doc gotchas survive across sessions (**shipped in code** — needs Render Postgres + unsuspend analysis).
- Out of scope for soft-launch: Phase 3 folder picker, deleting `desktop/`, control-plane argon2.

## Soft-launch status

| Item | Status |
|---|---|
| Marketing signup UX + **Continue to checkout** CTA | **Done** — success → app `/?checkout=trial` (onrender default; probe-upgrades to `app.casesifter.com` when DNS live) |
| Marketing apex / www on Render | **Live** — `https://casesifter.com` / `www` already serve marketing |
| Render `casesifter-app` (frontend SPA) | **Deployed** — use onrender URL until `app` CNAME |
| Browser Account (signup/login/checkout → control plane) | **Done** (`VITE_CONTROL_PLANE_URL`) |
| Control invite gate + Stripe env in `render.yaml` | **Runbook ready** — John sets secrets (`docs/STRIPE.md`); live `/health` still `"stripe":false` until keys |
| Render `casesifter-analysis` | **SUSPENDED** — John must unsuspend in Render Dashboard (this agent cannot). Attach Postgres + set `DATABASE_URL` / `FACT_MEMORY_MASTER_KEY` before sell. |
| Phase 0 lockdown | **Done** (`APP_MODE=analysis`, CORS not `*`, API key **or** active session) |
| Phase 1 page anchors / Phase 4 extraction | **Done** |
| Custom domain `app` | **BLOCKED** — `app.casesifter.com` NXDOMAIN until John adds CNAME + Render attach |
| Persistent fact memory (matters / Postgres) | **Code shipped** — gating test + firm-scoped store + `/v1/jobs?matter_id` + hosted Cases UI. **Ops-blocked:** provision Postgres, set secrets, unsuspend analysis. |

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

**Cross-origin note:** marketing signup stores a token on the marketing origin; the app may still need **Sign in** if that token is not shared. Soft-launch path: signup → **Continue to checkout** (`?checkout=trial`) → Stripe Checkout when the app session is present and `billing_state` is `none`.

## Invite + Stripe verify path

Full operator checklist: **`docs/STRIPE.md`**.

1. Set on `casesifter-control`: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID`, `CASESIFTER_INVITE_REQUIRED=false` (invite optional), optional `CASESIFTER_INVITE_CODES=…`, `CASESIFTER_PUBLIC_URL=https://casesifter-app.onrender.com` until custom app domain works (Blueprint default matches; override Dashboard if still `app.casesifter.com`).
2. Sign up on marketing → success → **Continue to checkout** (`casesifter-app.onrender.com/?checkout=trial`).
3. App Account: **Sign in** or **Start a 14-day free trial** (signup + Stripe Checkout at **$79/seat**, card + 14-day trial; auto-bills after). Entitled users see **Manage billing** + Sign out only. Header shows **Trial · N days left** while trialing. **New case** is gated: logged out → Account; signed in without plan → Account/Checkout. Marketing `signup.html` handoff still works.
4. **Domain trial limit:** one trial per corporate email domain (`trial_domains` in `accounts.json`); free mail (gmail/outlook/yahoo/etc.) is one trial per address. Abandoned Checkout does not burn the claim; second concurrent/corporate trial → clear **403**.
5. Webhook: `https://casesifter-control.onrender.com/v1/webhooks/stripe` — enable `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`. Enable Customer Portal + quantity updates in Stripe.
6. Redeploy control + app (+ marketing for CTA) → `curl -sS https://casesifter-control.onrender.com/health` → `"stripe":true`.
7. Demo shortcut (no card): admin `POST /v1/trial` with `CONTROL_ADMIN_TOKEN`.

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

## Persistent fact memory — ops (John)

Agent **cannot** unsuspend Render services or attach databases without dashboard access.

1. **Unsuspend** `casesifter-analysis` in Render Dashboard → Service → Resume.
2. **Create** a Render Postgres instance (or Blueprint database) and link it to analysis.
3. Set secrets on analysis (Blueprint already declares `sync: false`):
   - `DATABASE_URL` — from the Postgres service
   - `FACT_MEMORY_MASTER_KEY` — long random secret (wraps per-firm Fernet keys)
   - Existing: `ANALYSIS_API_KEYS`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `SENTRY_DSN`
4. Confirm `PERSIST_FACTS=true`, `SOURCE_FILE_RETENTION=immediate`.
5. Redeploy analysis; hit `/api/health` then create a matter from the hosted app and upload two contradicting exhibits.
6. Legal: ToS/Privacy draft wording is in the app Account modal — **flagged for John review; not claimed approved.**

Schema SQL reference: `backend/app/fact_memory/schema.sql`.
