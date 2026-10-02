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
  - `GEMINI_API_KEY`, `GEMINI_MODEL=gemini-3.6-flash`, `CONTEXTUAL_MULTIMODAL=true` — multimodal exhibit forensics
  - `GOTCHA_VERIFY_PROVIDER=auto` — Claude verifies gated gotcha candidates when key set; else Gemini
  - `ANTHROPIC_API_KEY` (optional sync secret) — set this to enable Claude as the gotcha verifier
  - `CLAUDE_MODEL=claude-sonnet-4-5-20250929`
  - `ENTITLEMENT_MODE=open` for internal ops, or require API keys in production
  - `SENTRY_DSN` (optional)

Scratch disk only if needed for in-flight jobs; do not treat it as permanent case storage.

**AI split (do not confuse with consumer “lie detectors”):** Gemini reads native PDF/images for alteration/historical chrome. Claude (when keyed) only **judges already-proposed claim/evidence pairs** behind hard vetoes and four gates — it does not open a free-form deception pass.

### control (Docker from `control_plane/`)

- Runtime matches the existing Oregon Docker service (Blueprint cannot flip Docker → native Python on an already-created service).
- `POST /v1/signup`, `/v1/login`, `GET /v1/entitlement`
- `POST /v1/checkout`, `POST /v1/webhooks/stripe`
- `POST /v1/trial` (admin), `POST /v1/dms/webhook` (phase-2 stub)
- Env: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID`, `CONTROL_ADMIN_TOKEN`, `CASESIFTER_PUBLIC_URL`, `CASESIFTER_TRIAL_DAYS`

### marketing (static from `marketing/`)

- Publish path: `./marketing`
- Public CTA: `/signup.html` (control-plane `POST /v1/signup`). `/download.html` redirects to signup — desktop installer is retired.
- **Why John still sees Download:** `casesifter.com` / `www.casesifter.com` are **not** the Render static site. DNS NS = `ns1.siteground.net` / `ns2.siteground.net` (SiteGuard). Apex/www A = `34.174.53.153` (SiteGround). `https://casesifter-marketing.onrender.com` already serves Sign up. Private browsing cannot fix a different host.

### Fix custom domain (pick one)

**Agent blocker (2026-10-02):** this Cloud Agent has **no** `RENDER_API_KEY`, SiteGround Site Tools login, or FTP/SFTP credentials. Cannot attach domains or upload to `34.174.53.153`. Provide one of the secrets below to finish remotely.

**A — Preferred: attach domain to Render `casesifter-marketing`**
1. Render Dashboard → **casesifter-marketing** → **Settings** → **Custom Domains** → add `www.casesifter.com` (Render also adds apex and redirects) **or** add both.
2. In SiteGround **Site Tools → Domain → DNS Zone Editor** (NS are still `ns1/ns2.siteground.net`), replace SiteGround hosting records:

| Host | Type | Value |
|---|---|---|
| `@` (apex `casesifter.com`) | **A** | `216.24.57.1` (Render load balancer; confirm in Dashboard if shown) |
| `www` | **CNAME** | `casesifter-marketing.onrender.com` |

3. Remove old A `34.174.53.153` for `@` / `www`, and remove any **AAAA** records (Render is IPv4-only).
4. Dashboard → **Verify**; wait for TLS. Hard-refresh `https://www.casesifter.com` — nav must say **Sign up**, not Download.
5. Keep SiteGround MX/SPF as-is if mail still uses SiteGround antispam (`mx*.antispam.mailspamprotection.com`).

**Secret for agent to do A:** `RENDER_API_KEY` (Account Settings → API Keys) plus DNS edit access — either SiteGround DNS Zone Editor login **or** registrar access if NS move off SiteGround.

**B — Fast interim: overwrite SiteGround files**
1. SiteGround → **Site Tools** → **File Manager** → site root for `casesifter.com` (often `public_html`).
2. Upload/replace from this branch’s `marketing/`: `index.html`, `signup.html`, `download.html`, `product.html`, `pricing.html`, `security.html`, `contact.html`, and `assets/` (`app.js`, `style.css`). Do **not** upload any real `.exe`.
3. Delete SiteGround `downloads/` (installer) and any `assets/downloads.json`.
4. Hard-refresh `https://www.casesifter.com`.

**Secret for agent to do B:** SiteGround FTP/SFTP host + username + password (Site Tools → Site → FTP Accounts), **or** Site Tools login so File Manager can be used.

## Deploy branch

Auto-deploy must track a ref that contains `backend/`, `control_plane/`, `marketing/`, and `render.yaml`. Bare `main` historically lacked `control_plane/` and `marketing/` — blueprint sync or deploys from that tip fail all three services while older instances can still answer health checks. Prefer merging the product branch, then pointing Render at `main`.

Docker images listen on `0.0.0.0:$PORT` via `sh -c` CMD (Render-injected; defaults 8000 analysis / 8010 control). Do not replace that with a hardcoded port or a `dockerCommand` that skips `/bin/sh -c` — health checks hit Render's injected PORT, not 8000. OpenCV is lazy-imported at forensic time so a bad wheel cannot block `/api/health`.

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
