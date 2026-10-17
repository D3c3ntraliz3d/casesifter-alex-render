# Transport and HTTP security

There is **no TLS 2.0**. Modern browsers and platforms use **TLS 1.2** (minimum) and **TLS 1.3** (preferred). CaseSifter targets that bar at every edge that terminates HTTPS.

## What we require

| Layer | Policy |
| --- | --- |
| Public HTTPS | TLS **1.2+**, prefer **1.3**; disable TLS 1.0 / 1.1 |
| HSTS | `max-age=63072000; includeSubDomains; preload` on HTTPS responses |
| Session cookies | `HttpOnly`; `Secure` in production; `SameSite=None` (cross-site app↔API) or `Lax` locally |
| API responses | `nosniff`, `DENY` framing, COOP/CORP, tight CSP (`default-src 'none'`), `no-store` |
| Static app / marketing | CSP + HSTS + COOP on Render static headers and nginx |
| SMTP | STARTTLS / TLS when mail is configured (`SMTP_USE_TLS=true`) |

TLS is **terminated at the host edge** (Render custom domains / load balancer). App containers may listen on HTTP internally; clients only see HTTPS.

## Operator checklist (Render)

1. Custom domains on `app.casesifter.com`, control, analysis, and marketing use Render-managed certificates.
2. Confirm the dashboard shows **HTTPS** and that HTTP redirects to HTTPS (Render default for custom domains).
3. After DNS is stable, optionally submit domains for [HSTS preload](https://hstspreload.org/) (header already includes `preload`).
4. Spot-check with SSL Labs / `curl -vI https://app.casesifter.com` — expect HSTS and no TLS 1.0/1.1.
5. Keep `APP_ENV=production` so APIs emit HSTS when `X-Forwarded-Proto: https`.
6. Set `COOKIE_SECURE=true` (or rely on production `APP_ENV`) so session cookies never go cleartext.

## Local development

- HSTS is **not** sent on cleartext `http://localhost` (avoids locking browsers onto HTTPS for local ports).
- Set `FORCE_HSTS=true` only behind a local HTTPS reverse proxy when testing preload behavior.

## Related code

- `backend/app/edge_security.py` / `control_plane/edge_security.py` — API headers + HSTS
- `backend/app/session_cookie.py` / `control_plane/session_cookie.py` — Secure / HttpOnly cookies
- `frontend/nginx.conf` — container static headers
- `render.yaml` — static site HSTS / CSP for app + marketing
- `.github/workflows/security.yml` — automated security checks
