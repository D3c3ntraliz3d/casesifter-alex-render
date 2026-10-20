# Soft-launch ops checklist

Operator steps from [`HANDOFF.md`](HANDOFF.md). This agent cannot unsuspend Render or set DNS.

## Analysis (`casesifter-analysis`)

- [ ] Resume service in Render Dashboard
- [ ] Attach Postgres; set `DATABASE_URL`
- [ ] Set `FACT_MEMORY_MASTER_KEY` (long random; wraps per-firm keys)
- [ ] Confirm `ANALYSIS_API_KEYS`, `GEMINI_API_KEY`, `SESSION_SECRET`, CORS origins
- [ ] Confirm `PERSIST_FACTS=true`, `SOURCE_FILE_RETENTION=immediate`
- [ ] `GET https://casesifter-analysis.onrender.com/api/health` → 200
- [ ] `POST /api/v1/jobs` without auth → **401**

## Stripe / control (`casesifter-control`)

- [ ] Live `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`, `STRIPE_WEBHOOK_SECRET`
- [ ] Webhook endpoint + Customer Portal seat quantity updates
- [ ] `curl -sS https://casesifter-control.onrender.com/health` → `"stripe":true`
- [ ] Trial/checkout smoke from Account

## App DNS / CORS

- [ ] `app` CNAME → `casesifter-app.onrender.com`; attach custom domain on Render
- [ ] Prefer `https://app.casesifter.com` in `CASESIFTER_PUBLIC_URL` once live
- [ ] Confirm session cookie / CORS across marketing ↔ app ↔ control ↔ analysis
- [ ] Marketing signup → app `?cs_code=` handoff (or Sign in once)

## Post-ops smoke

- [ ] Sign up / verify / trial checkout
- [ ] Create matter, upload two exhibits, open authenticity report
- [ ] Export HTML / PDF / JSON downloads
- [ ] Admin → Invite seat (owner/admin)
