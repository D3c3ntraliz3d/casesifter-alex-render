# CaseSifter — Stripe operator runbook

Control plane billing lives in `control_plane/main.py`. Hosted service: **`casesifter-control`**.

Pricing: **$79/seat/month** (quantity at Checkout + Customer Portal). Server-only Checkout and Portal — **no `STRIPE_PUBLISHABLE_KEY`**.

## Safe IDs (ok to share) vs secrets (Render only)

| Give Cursor / docs | Keep private (Render env only) |
|---|---|
| Product: `prod_VN2ZsVDlIiPkHD` | `STRIPE_SECRET_KEY=sk_test_…` (then `sk_live_…`) |
| Price: `price_1UMIUnD6sOleRtjBjVbHzMdP` ($79/seat/mo) | `STRIPE_WEBHOOK_SECRET=whsec_…` |
| Webhook URL below | Do not commit keys |

Set on Render: `STRIPE_PRICE_ID=price_1UMIUnD6sOleRtjBjVbHzMdP`.

## What “Stripe not configured” means

`GET /health` → `{"status":"ok","stripe":false}` when **`STRIPE_SECRET_KEY` or `STRIPE_PRICE_ID` is empty**.

`POST /v1/checkout` then returns **dev fallback** (no Stripe Checkout URL):

| Button (Account) | Body | Dev behavior |
|---|---|---|
| **Start free trial** | `{ "trial": true, "quantity": 1 }` | Sets `trial_until` locally; message: `Stripe not configured — trial started without a card (local/dev).` |
| (no “Subscribe now” for entitled users) | `{ "trial": false, "quantity": N }` | Marks `subscription.status=active`, `plan=pro-dev`, `seats=N` for ~30 days (dev only). |

UI shows that message as the red Account banner and **does not open** a Checkout tab (`url` is `null`, `mode` is `"dev"`).

When Stripe **is** configured:

- Checkout returns `{ "mode": "stripe", "url": "https://checkout.stripe.com/…" }` (card collected; trial uses `subscription_data.trial_period_days`).
- **Manage billing** (when `has_billing_portal`) opens the **Stripe Customer Portal** via `POST /v1/billing-portal` — not a new Checkout session.

## Entitlement fields

`GET /v1/entitlement` (and signup/login payloads) include:

| Field | Values / meaning |
|---|---|
| `billing_state` | `none` \| `trialing` \| `active` \| `past_due` \| `canceled` |
| `trial_days_left` | ceil days remaining while `trialing` (local `trial_until` or Stripe `trial_end`) |
| `has_billing_portal` | true when `stripe_customer_id` exists |
| `active` / `plan` / `seats` | unchanged |

App UI: header shows **Trial · N days left** while trialing; Account CTAs follow `billing_state` (Start free trial when `none`; Manage billing + Sign out when trialing/active).

## Domain trial limit

Stored in `accounts.json`:

- `trial_domains`: `domain → { email, started_at, user_id }` for **corporate** domains
- `trial_emails`: `email → { … }` for **free consumer** providers (gmail, outlook, yahoo, icloud, proton, etc.)

Rules:

- Corporate: **one trial per email domain** (blocks `a@firm.com` then `b@firm.com`).
- Free providers: **one trial per email address**.
- Claim is recorded on successful `checkout.session.completed` / first `trialing` (abandoned Checkout does not burn the slot).
- `POST /v1/checkout` with `trial=true` still **403**s if the domain/email is already claimed or another account on that corporate domain is already `trialing`/`active`.

Clear 403 copy: *“A trial was already started for this company email domain…”* (or the per-email variant for free mail).

## New case gate (app)

- Logged out → **New case** sends the browser to `https://casesifter.com/signup.html`.
- Signed in with `billing_state` in `none` / `canceled` / `past_due` → Account (and Checkout trial when `none`).
- `active` / `trialing` → normal create-case flow.

## Pricing (match Account UI)

**$79/seat/month.** Checkout line-item quantity = seats (1–50). Soft-launch trial Checkout defaults to **1 seat**.

---

## John checklist (copy-paste)

### A. Stripe Dashboard — product + price (test mode first)

1. Open [Stripe Dashboard](https://dashboard.stripe.com/) → toggle **Test mode ON**.
2. Confirm product `prod_VN2ZsVDlIiPkHD` and price **`price_1UMIUnD6sOleRtjBjVbHzMdP`** = **$79.00 USD / month** per seat (recurring).
3. Use that Price ID as `STRIPE_PRICE_ID` on Render (do not invent a second soft-launch price).

### B. API keys (test first, then live)

1. **Developers → API keys**
2. Copy **Secret key** (`sk_test_…` for soft-launch verify).
3. After Checkout + webhook + portal work in test mode, repeat in **Live mode** (`sk_live_…`, live `price_…` for $79/seat, live webhook) before charging real cards.

Do **not** commit keys. Paste only into Render env. Publishable key is **not** required for this stack.

### C. Webhook endpoint

1. **Developers → Webhooks → Add endpoint**
2. **Endpoint URL:**
   ```
   https://casesifter-control.onrender.com/v1/webhooks/stripe
   ```
3. **Events to send** (handled in `control_plane/main.py`):
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
4. Add endpoint → reveal **Signing secret** (`whsec_…`) → that is `STRIPE_WEBHOOK_SECRET`.

Use the same event list for the live-mode endpoint later. Do **not** add a second Next.js webhook.

### D. Customer Portal

1. Stripe Dashboard → **Settings → Billing → Customer portal**
2. **Enable** the portal.
3. Turn on **update quantities** (so firms can change seat count; webhooks sync `items[0].quantity` → `subscription.seats`).
4. Allow cancel / payment method update as you prefer for soft-launch.

### E. Render env on `casesifter-control`

Dashboard → **casesifter-control** → **Environment** → set:

| Key | Value |
|---|---|
| `STRIPE_SECRET_KEY` | `sk_test_…` (then `sk_live_…` for production) |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` from the webhook above |
| `STRIPE_PRICE_ID` | `price_1UMIUnD6sOleRtjBjVbHzMdP` ($79/seat/mo) |
| `CASESIFTER_PUBLIC_URL` | `https://casesifter-app.onrender.com` until `app.casesifter.com` DNS exists |

Blueprint (`render.yaml`) already defaults `CASESIFTER_PUBLIC_URL` to the onrender app URL. If the live service still has `https://app.casesifter.com`, **overwrite it** in the Dashboard (Checkout success/cancel and Portal return URL would 404/NXDOMAIN otherwise).

Optional (already set in Blueprint): `CASESIFTER_TRIAL_DAYS=14`, `CASESIFTER_INVITE_REQUIRED=false`.

### F. Redeploy + health

1. After saving env vars: **Manual Deploy** (or wait for auto-redeploy) on `casesifter-control` **and** `casesifter-app` (trial UX / New case gate). Redeploy marketing if you want the **Continue to checkout** CTA.
2. Verify:
   ```bash
   curl -sS https://casesifter-control.onrender.com/health
   ```
   Expect: `{"status":"ok","stripe":true}`
3. If still `"stripe":false`, one of `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID` is missing or the deploy did not pick up env — re-check Dashboard values and redeploy.

### G. End-to-end Account path

1. Sign up on `https://casesifter.com/signup.html` → **Continue to checkout** → app with `?checkout=trial`.
2. Or open `https://casesifter-app.onrender.com` → **Account** → Sign in → **Start free trial** → Stripe Checkout (1 seat, card + 14-day trial).
3. Complete Checkout with a [test card](https://docs.stripe.com/testing#cards) (`4242…`) in test mode.
4. Success redirect: `https://casesifter-app.onrender.com/?billing=success`
5. Header badge: **Trial · N days left**; Account shows **Manage billing** + Sign out only (no dual trial/subscribe).
6. **Manage billing** → Customer Portal.
7. Second corporate signup on the same domain → trial Checkout **403**.
8. Confirm Stripe Dashboard → Webhooks → recent deliveries **200**.

### H. Go live (after soft-launch verify)

1. Stripe **Live mode**: $79/seat product/price, live secret key, live webhook (same URL + events including `invoice.payment_failed`), live `whsec_…`, portal enabled with quantity updates.
2. Update the four Render vars to **live** values.
3. Redeploy control → `/health` still `stripe:true`.
4. Smoke one real Checkout with a real card only when ready to invoice (Phase 2 case memory is still the hard gate before first paid invoice — see `docs/HANDOFF.md`).

---

## API surface

| Endpoint | Role |
|---|---|
| `POST /v1/checkout` | Hosted Checkout; body `{ trial, quantity }` (quantity 1–50); reuses `stripe_customer_id` when present; trial respects domain/email limit |
| `POST /v1/billing-portal` | Customer Portal session; **400** if no `stripe_customer_id` yet |
| `POST /v1/webhooks/stripe` | Entitlement sync + trial domain/email claim on first trialing |
| `GET /v1/entitlement` | Includes `billing_state`, `trial_days_left`, `seats`, `has_billing_portal` |

Persisted on the user record (JSON store): `stripe_customer_id`, `subscription.stripe_subscription_id` (`sub_…`, never Checkout `cs_…`), `subscription.seats`, status, period end, optional `trial_end`. Store-level: `trial_domains`, `trial_emails`.

Success/cancel URLs:

`{CASESIFTER_PUBLIC_URL}/?billing=success|cancel`

Portal return URL: `{CASESIFTER_PUBLIC_URL}`

## Code reference (do not invent secrets)

| Concern | Location |
|---|---|
| Env keys | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID`, `CASESIFTER_PUBLIC_URL` |
| Health `stripe` flag | `bool(STRIPE_SECRET and STRIPE_PRICE_ID)` |
| Checkout + seats + trial | `POST /v1/checkout` |
| Domain trial limit | `trial_domains` / `trial_emails` in `accounts.json` |
| Customer Portal | `POST /v1/billing-portal` |
| Webhook events | `POST /v1/webhooks/stripe` |
| Blueprint secrets | `render.yaml` → `casesifter-control` |
