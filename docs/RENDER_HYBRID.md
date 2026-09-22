# CaseSifter — Render services (analysis + control plane)

Ephemeral analysis API and Stripe control plane. **No customer document vault.**

## Services

### analysis (Docker from `backend/`)

- Health: `/api/health`
- Ephemeral jobs: `POST /api/v1/jobs`, `GET /api/v1/jobs/{id}`, `DELETE /api/v1/jobs/{id}`
- Env:
  - `EPHEMERAL_JOBS=true`
  - `EPHEMERAL_TTL_HOURS=6`
  - `ANALYSIS_API_KEYS=` (optional comma-separated)
  - `GEMINI_API_KEY`, `GEMINI_MODEL`, `CONTEXTUAL_MULTIMODAL`
  - `ENTITLEMENT_MODE=open` for internal ops, or require API keys in production

Scratch disk only if needed for in-flight jobs; do not treat it as permanent case storage.

### control (Python from `control_plane/`)

- `POST /v1/signup`, `/v1/login`, `GET /v1/entitlement`
- `POST /v1/checkout`, `POST /v1/webhooks/stripe`
- `POST /v1/trial` (admin), `POST /v1/dms/webhook` (phase-2 stub)
- Env: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID`, `CONTROL_ADMIN_TOKEN`, `CASESIFTER_PUBLIC_URL`, `CASESIFTER_TRIAL_DAYS`

### marketing (static from `marketing/`)

- Publish path: `./marketing`
- Public download page: `/download.html` (installer URL from `assets/downloads.json` → GitHub Releases)

## Local compose hint

```yaml
services:
  analysis:
    build: ./backend
    ports: ["8000:8000"]
    environment:
      EPHEMERAL_JOBS: "true"
  control:
    build: ./control_plane
    ports: ["8010:8010"]
    command: uvicorn main:app --host 0.0.0.0 --port 8010
```
