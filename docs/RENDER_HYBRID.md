# CaseSifter — Render services (analysis + control plane + web app)

Ephemeral analysis API, Stripe control plane, and hosted SPA. **No customer document vault.**

## Soft-launch URLs (2026-10)

| Surface | URL | Render service |
|---|---|---|
| Marketing | `https://www.casesifter.com` (DNS cutover) / `https://casesifter-marketing.onrender.com` | `casesifter-marketing` |
| Web app | `https://app.casesifter.com` (DNS) / `https://casesifter-app.onrender.com` | `casesifter-app` |
| Control plane | `https://casesifter-control.onrender.com` | `casesifter-control` |
| Analysis | `https://casesifter-analysis.onrender.com` | `casesifter-analysis` (**resume before sell**) |

**Invite + Stripe path:** marketing Sign up (invite code) → Open web app → Account → Start 14-day trial → Stripe Checkout → return to `CASESIFTER_PUBLIC_URL`.

## Services

### analysis (Docker from `backend/`)

- Health: `/api/health` → 200 with `app_mode: analysis`
- Ephemeral jobs: `POST /api/v1/jobs`, `GET /api/v1/jobs/{id}`, `DELETE /api/v1/jobs/{id}`
- Auth on `/api/v1/*`: `X-API-Key` / Bearer matching `ANALYSIS_API_KEYS`, **or** Bearer control-plane session with **active** entitlement (`CLOUD_CONTROL_URL` probe)
- Unauthorized `/api/v1/jobs` → **401**; vault `/api/cases` → **404**
- Env:
  - `APP_MODE=analysis`
  - `EPHEMERAL_JOBS=true`
  - `EPHEMERAL_TTL_HOURS=6`
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
- `POST /v1/checkout`, `POST /v1/webhooks/stripe`
- `POST /v1/trial` (admin), `POST /v1/dms/webhook` (phase-2 stub)
- Env (Blueprint + Dashboard secrets):

| Key | Soft-launch |
|---|---|
| `STRIPE_SECRET_KEY` | sync:false — John |
| `STRIPE_WEBHOOK_SECRET` | sync:false — John |
| `STRIPE_PRICE_ID` | sync:false — John |
| `CONTROL_ADMIN_TOKEN` | sync:false — John |
| `CASESIFTER_PUBLIC_URL` | `https://app.casesifter.com` |
| `CASESIFTER_TRIAL_DAYS` | `14` |
| `CASESIFTER_INVITE_REQUIRED` | `true` |
| `CASESIFTER_INVITE_CODES` | sync:false comma-list — John |

**Verify:** signup with invite → Account → Start trial → Stripe (or admin `POST /v1/trial` for demos). Webhook endpoint: `https://casesifter-control.onrender.com/v1/webhooks/stripe`.

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
- Public CTA: `/signup.html` → control-plane `POST /v1/signup`; success CTA **Open web app** → `https://app.casesifter.com`
- `/download.html` redirects to signup — desktop installer is retired.

**Why John may still see Download:** `casesifter.com` / `www` may still be SiteGround. `https://casesifter-marketing.onrender.com` already serves Sign up.

## DNS cutover (John — agent cannot without credentials)

| Host | Type | Target |
|---|---|---|
| `@` (apex) | **A** | Render load balancer IP from Dashboard (often `216.24.57.1`) |
| `www` | **CNAME** | `casesifter-marketing.onrender.com` |
| `app` | **CNAME** | `casesifter-app.onrender.com` |

Remove old SiteGround A `34.174.53.153` for `@` / `www`, and any **AAAA**. Keep SiteGround MX/SPF if mail still uses SiteGround antispam.

**Secrets for agent to finish remotely:** `RENDER_API_KEY` and/or SiteGround DNS/FTP — see historical Domain fix notes below.

### Fix custom domain (pick one)

**Agent blocker:** this Cloud Agent has **no** `RENDER_API_KEY`, SiteGround Site Tools login, or FTP/SFTP credentials. Cannot attach domains or upload to SiteGround without those.

**A — Preferred: attach domain to Render `casesifter-marketing` (+ `casesifter-app`)**
1. Render → **casesifter-marketing** → Custom Domains → `www.casesifter.com` (and apex as shown).
2. Render → **casesifter-app** → Custom Domains → `app.casesifter.com`.
3. SiteGround DNS Zone Editor: apply the DNS table above.
4. Verify TLS; hard-refresh — nav must say **Sign up**, not Download.

**B — Fast interim: overwrite SiteGround files** with this branch’s `marketing/` (no `.exe`).

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
