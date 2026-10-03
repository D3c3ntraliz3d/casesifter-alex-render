# CaseSifter — Stripe operator runbook

Control plane billing lives in `control_plane/main.py`. Hosted service: **`casesifter-control`**.

## What “Stripe not configured” means

`GET /health` → `{"status":"ok","stripe":false}` when **`STRIPE_SECRET_KEY` or `STRIPE_PRICE_ID` is empty**.

`POST /v1/checkout` then returns **dev fallback** (no Stripe Checkout URL):

| Button (Account) | Body | Dev behavior |
|---|---|---|
| **Start 14-day trial** | `{ "trial": true }` | Sets `trial_until` locally; message: `Stripe not configured — trial started without a card (local/dev).` |
| **Manage billing** / **Subscribe now** | `{ "trial": false }` | Marks `subscription.status=active`, `plan=pro-dev` for ~30 days; message: `Stripe not configured — marked Pro for local/dev.` |

UI shows that message as the red Account banner and **does not open** a Checkout tab (`url` is `null`, `mode` is `"dev"`).

When Stripe **is** configured, checkout returns `{ "mode": "stripe", "url": "https://checkout.stripe.com/…" }` and the browser opens Checkout. Trials still collect a card up front (`subscription_data.trial_period_days`).

> **Note:** “Manage billing” today opens **Stripe Checkout** (new subscription), not the Stripe Customer Portal.

## Pricing (match Account UI)

Account copy: **Solo $149/attorney/mo** · Firm $119/attorney/mo (2+ seats).

Checkout uses a **single** env price id (`STRIPE_PRICE_ID`). Soft-launch: create the **Solo $149/mo** recurring price and put that `price_…` in Render. Firm multi-seat is UI/marketing only until a second price/path exists.

---

## John checklist (copy-paste)

### A. Stripe Dashboard — product + price (test mode first)

1. Open [Stripe Dashboard](https://dashboard.stripe.com/) → toggle **Test mode ON**.
2. **Product catalog** → **Add product**
   - Name: `CaseSifter Solo` (or `CaseSifter`)
   - Pricing: **Recurring**, **$149.00 USD**, billing period **Monthly**
3. Save → copy the Price ID: `price_…` (not the product `prod_…`).
4. Keep this `price_…` for `STRIPE_PRICE_ID`.

### B. API keys (test first, then live)

1. **Developers → API keys**
2. Copy **Secret key** (`sk_test_…` for soft-launch verify).
3. After Checkout + webhook work in test mode, repeat A–B in **Live mode** (`sk_live_…`, new live `price_…`, new live webhook) before charging real cards.

Do **not** commit keys. Paste only into Render env.

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
4. Add endpoint → reveal **Signing secret** (`whsec_…`) → that is `STRIPE_WEBHOOK_SECRET`.

Use the same event list for the live-mode endpoint later.

### D. Render env on `casesifter-control`

Dashboard → **casesifter-control** → **Environment** → set:

| Key | Value |
|---|---|
| `STRIPE_SECRET_KEY` | `sk_test_…` (then `sk_live_…` for production) |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` from the webhook above |
| `STRIPE_PRICE_ID` | `price_…` for Solo $149/mo |
| `CASESIFTER_PUBLIC_URL` | `https://casesifter-app.onrender.com` until `app.casesifter.com` DNS exists |

Blueprint (`render.yaml`) already defaults `CASESIFTER_PUBLIC_URL` to the onrender app URL. If the live service still has `https://app.casesifter.com`, **overwrite it** in the Dashboard (Checkout success/cancel would 404/NXDOMAIN otherwise).

Optional (already set in Blueprint): `CASESIFTER_TRIAL_DAYS=14`, `CASESIFTER_INVITE_REQUIRED=false`.

### E. Redeploy + health

1. After saving env vars: **Manual Deploy** (or wait for auto-redeploy) on `casesifter-control`.
2. Verify:
   ```bash
   curl -sS https://casesifter-control.onrender.com/health
   ```
   Expect: `{"status":"ok","stripe":true}`
3. If still `"stripe":false`, one of `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID` is missing or the deploy did not pick up env — re-check Dashboard values and redeploy.

### F. End-to-end Account path

1. Open `https://casesifter-app.onrender.com` → **Account** → Sign in.
2. If scans locked: **Start 14-day trial** → new tab should open **Stripe Checkout** (not the red “Stripe not configured” banner).
3. If already on a local/dev trial: **Manage billing** → should also open Checkout (subscribe, no trial days).
4. Complete Checkout with a [test card](https://docs.stripe.com/testing#cards) (`4242…`) in test mode.
5. Success redirect: `https://casesifter-app.onrender.com/?billing=success`
6. Cancel path: `…/?billing=cancel`
7. Confirm Stripe Dashboard → Webhooks → recent deliveries **200**, and Account entitlement shows trial/subscription active after webhook.

### G. Go live (after soft-launch verify)

1. Stripe **Live mode**: create Solo $149 product/price, live secret key, live webhook (same URL + events), live `whsec_…`.
2. Update the four Render vars to **live** values.
3. Redeploy control → `/health` still `stripe:true`.
4. Smoke one real Checkout with a real card only when ready to invoice (Phase 2 case memory is still the hard gate before first paid invoice — see `docs/HANDOFF.md`).

---

## Code reference (do not invent secrets)

| Concern | Location |
|---|---|
| Env keys | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID`, `CASESIFTER_PUBLIC_URL` |
| Health `stripe` flag | `bool(STRIPE_SECRET and STRIPE_PRICE_ID)` |
| Checkout + trial days | `POST /v1/checkout` |
| Webhook events | `POST /v1/webhooks/stripe` |
| Blueprint secrets | `render.yaml` → `casesifter-control` |

Success/cancel URLs are built as:

`{CASESIFTER_PUBLIC_URL}/?billing=success|cancel`
