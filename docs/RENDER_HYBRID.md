# CaseSifter — Render services (analysis + control plane + web app)

Analysis API with **persistent per-matter fact memory** (encrypted facts/findings; **no PDF vault**),
Stripe control plane, and hosted SPA.

## Soft-launch URLs (2026-10)

| Surface | URL | Render service |
|---|---|---|
| Marketing | `https://casesifter.com` / `https://www.casesifter.com` (**live on Render**) / `https://casesifter-marketing.onrender.com` | `casesifter-marketing` |
| Web app (interim) | `https://casesifter-app.onrender.com` | `casesifter-app` |
| Web app (custom, not yet) | `https://app.casesifter.com` — **NXDOMAIN until `app` CNAME** | `casesifter-app` |
| Control plane | `https://casesifter-control.onrender.com` | `casesifter-control` |
| Analysis | `https://casesifter-analysis.onrender.com` | `casesifter-analysis` (**resume before sell**) |

### Interim client path

| Action | URL |
|---|---|
| Sign up | `https://casesifter.com/signup.html` (invite **optional** — gate off) |
| Log in / use product | `https://casesifter-app.onrender.com` → **Account → Sign in** |
| Avoid until DNS | `https://app.casesifter.com` |

**Cross-origin:** marketing-site session token ≠ app session. After signup, users must Sign in on the hosted app.

**Invite + Stripe path:** marketing Sign up (invite optional) → Open web app (`casesifter-app.onrender.com`) → Account → Sign in → Start 14-day trial → Stripe Checkout → return to `CASESIFTER_PUBLIC_URL`.

## Services

### analysis (Docker from `backend/`)

- Health: `/api/health` → 200 with `app_mode: analysis`
- Jobs: `POST /api/v1/jobs` (optional `matter_id` / `filed_date`), `GET /api/v1/jobs/{id}`
- Matters: `GET/POST /api/v1/matters`, findings/export/delete under `/api/v1/matters/{id}`
- Auth on `/api/v1/*`: `X-API-Key` / Bearer matching `ANALYSIS_API_KEYS`, **or** Bearer control-plane session with **active** entitlement (`CLOUD_CONTROL_URL` probe)
- Unauthorized `/api/v1/jobs` → **401**; vault `/api/cases` → **404** in analysis mode
- Env:
  - `APP_MODE=analysis`
  - `EPHEMERAL_JOBS=true` (source PDF still deleted immediately)
  - `SOURCE_FILE_RETENTION=immediate`
  - `PERSIST_FACTS=true` (per-firm `persist_facts` can opt out → classic ephemeral)
  - `DATABASE_URL` — Render Postgres (sync:false). Local/CI falls back to SQLite with the same schema.
  - `FACT_MEMORY_MASTER_KEY` — wraps per-firm Fernet data keys (sync:false)
  - `EPHEMERAL_TTL_HOURS=6` — job report TTL for **non-persist** firms only; persist firms skip sweep via `persist.flag`
  - `CORS_ALLOW_ORIGINS` — must include `https://app.casesifter.com`, `https://www.casesifter.com`, `https://casesifter.com` (plus onrender fallbacks). **Never `*` in analysis.**
  - `ANALYSIS_API_KEYS=` (ops keys; sync:false secret)
  - `CLOUD_CONTROL_URL=https://casesifter-control.onrender.com`
  - `ENTITLEMENT_MODE=cloud`
  - `GEMINI_API_KEY`, `GEMINI_MODEL=gemini-3.6-flash`, `CONTEXTUAL_MULTIMODAL=true`
  - `GOTCHA_VERIFY_PROVIDER=auto`, `ANTHROPIC_API_KEY` (optional), `CLAUDE_MODEL=claude-sonnet-4-5-20250929`
  - `SENTRY_DSN` (optional)

**Resume (John):** Dashboard → `casesifter-analysis` → Resume. Confirm `/api/health` 200 and unauthenticated `POST /api/v1/jobs` → 401. Keep suspended until secrets + CORS + keys are set.

Scratch disk only if needed for in-flight jobs; do not treat it as permanent case storage.

**AI split:** Gemini reads native PDF/images for alteration/historical chrome. Claude (when keyed) only **judges already-proposed claim/evidence pairs** behind hard vetoes and four gates.

### control (Docker from `control_plane/`)

- `POST /v1/signup`, `/v1/login`, `GET /v1/entitlement`
- `POST /v1/checkout` (quantity / seats), `POST /v1/billing-portal`, `POST /v1/webhooks/stripe`
- `POST /v1/trial` (admin), `POST /v1/dms/webhook` (phase-2 stub)
- Env (Blueprint + Dashboard secrets):

| Key | Soft-launch |
|---|---|
| `STRIPE_SECRET_KEY` | sync:false — John (`sk_test_…` first) |
| `STRIPE_WEBHOOK_SECRET` | sync:false — John (`whsec_…`) |
| `STRIPE_PRICE_ID` | sync:false — John (**$79/seat**/mo `price_1UMIUnD6sOleRtjBjVbHzMdP`) |
| `CONTROL_ADMIN_TOKEN` | sync:false — John |
| `CASESIFTER_PUBLIC_URL` | Blueprint: `https://casesifter-app.onrender.com` until `app` DNS; then `https://app.casesifter.com` |
| `CASESIFTER_TRIAL_DAYS` | `14` |
| `CASESIFTER_INVITE_REQUIRED` | `false` (invite optional) |
| `CASESIFTER_INVITE_CODES` | sync:false comma-list — optional while gate is off |

No `STRIPE_PUBLISHABLE_KEY` — Checkout and Customer Portal are server-created.

**Stripe runbook:** `docs/STRIPE.md` (product/price, portal, keys, webhook events, Render env, verify).

**Webhook:** `https://casesifter-control.onrender.com/v1/webhooks/stripe`  
Events: `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`.

**Verify:** `GET /health` → `"stripe":true` → Account → Start trial (Checkout with seat qty) → Manage billing → Customer Portal (or admin `POST /v1/trial` for demos without a card). Without Stripe env, checkout is a local/dev entitlement grant and Account shows the red “Stripe not configured” banner.

### app (static from `frontend/`)

- Service name: `casesifter-app`
- Build: `npm ci && npm run build` (rootDir `frontend`)
- Publish: `frontend/dist`
- SPA rewrite: `/*` → `/index.html`
- Custom domain: `app.casesifter.com` (attach in Dashboard)
- Build-time env: `VITE_CONTROL_PLANE_URL`, `VITE_ANALYSIS_API_URL` (+ optional `VITE_SENTRY_DSN`)

Browser Account uses control-plane signup/login/checkout; analysis calls send session Bearer (or ops key).

### marketing (static from `marketing/`)

- Publish path: `./marketing`
- Public CTA: `/signup.html` → control-plane `POST /v1/signup` (invite **optional** while gate is off); success CTA **Open web app** defaults to `https://casesifter-app.onrender.com` (JS probes and upgrades to `https://app.casesifter.com` when that host resolves)
- Nav/footer **Sign in** → `https://casesifter-app.onrender.com` (Account is in-app; no marketing `/login.html`)
- `/download.html` redirects to signup — desktop installer is retired.

**Apex / www status:** already on Render marketing (not SiteGround). Clients use `https://casesifter.com/signup.html` today.

## DNS remaining (John — agent cannot without credentials)

Apex and `www` are done. **Only custom app domain remains:**

| Host | Type | Target |
|---|---|---|
| `app` | **CNAME** | `casesifter-app.onrender.com` |

Then: Render → **casesifter-app** → Custom Domains → `app.casesifter.com` → verify TLS.

Keep SiteGround MX/SPF if mail still uses SiteGround antispam.

**Secrets for agent to finish remotely:** `RENDER_API_KEY` and/or registrar DNS access.

## Deploy branch

Auto-deploy must track a ref that contains `backend/`, `control_plane/`, `frontend/`, `marketing/`, and `render.yaml`. Prefer merging the product branch, then pointing Render at `main`.

Docker images listen on `0.0.0.0:$PORT` via `sh -c` CMD (Render-injected). Do not hardcode a port that skips Render's injected PORT.

## Local compose hint

```yaml
services:
  analysis:
    build: ./backend
    ports: ["8000:8000"]
    environment:
      EPHEMERAL_JOBS: "true"
      APP_MODE: analysis
      CORS_ALLOW_ORIGINS: "http://127.0.0.1:5173"
      CLOUD_CONTROL_URL: "http://127.0.0.1:8010"
  control:
    build: ./control_plane
    ports: ["8010:8010"]
    command: uvicorn main:app --host 0.0.0.0 --port 8010
```

## Ops: unsuspend analysis + attach Postgres

Cannot be done from this agent without Render credentials.

1. Render Dashboard → `casesifter-analysis` → **Resume** (if Suspended).
2. Create managed Postgres; copy Internal Database URL into analysis env `DATABASE_URL` (Blueprint key is `sync: false`).
3. Generate and set `FACT_MEMORY_MASTER_KEY` (never commit). Rotate = re-encrypt firm keys (plan a migration before rotating in prod).
4. Deploy; verify `/api/health` and `POST /api/v1/matters` with a session Bearer.
5. Source PDFs still delete immediately; facts persist when `PERSIST_FACTS=true` and firm `persist_facts` is true.
