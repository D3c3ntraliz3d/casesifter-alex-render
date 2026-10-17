# Admin panel, SSO (OIDC), and 2FA (TOTP)

Operator guide for CaseSifter control plane + analysis API. Extends the existing
session/cookie auth stack (`cp_users` / `cp_sessions` / `cs_session`); it does not
replace it.

## What shipped

| Capability | Where | Notes |
|---|---|---|
| Admin panel | App **Admin** tab | Account seats, sessions, auth audit, firm settings, scan/job health |
| OIDC SSO | Control plane `/v1/sso/oidc/*` | Google / Microsoft / Okta-style discovery + PKCE |
| TOTP 2FA | Control plane `/v1/2fa/totp/*` | Password users only; recovery codes; no admin bypass |
| Hardening | Existing + new audit actions | HttpOnly cookies, CSRF origin check, auth rate limits, session revoke |

**SAML** is a follow-up. Use OIDC for Google Workspace, Microsoft Entra ID, Okta, Auth0, etc.

## Roles (hybrid access)

| Role | Cases / scans | Account (2FA, personal sign-in log) | Firm matter access log | Admin tab |
|---|---|---|---|---|
| **Owner** | Yes | Yes | Yes | Yes |
| **Admin** | Yes | Yes | Yes | Yes |
| **Member** | Yes | Yes (own account only) | No | No |
| Sales `CONTROL_ADMIN_TOKEN` | No firm files | Trials only | No | Separate API |

Signup creates an **owner**. Multi-user seat invites are still a follow-up; seats exist for billing.

**Access log (Account):** control-plane events for this user always; firm matter events only when the analysis role is owner/admin. Filter by Source: Account vs Firm.

## How to turn on 2FA (TOTP)

No special env flag is required beyond a running control plane with `pyotp` installed.

1. Sign in with **password** (not only SSO if you want local TOTP).
2. Open **Account**.
3. Under **Two-factor authentication**, click **Enable authenticator**.
4. Add the secret / `otpauth://` URL in Google Authenticator, 1Password, Authy, etc.
5. Enter the 6-digit code → **Confirm and enable**.
6. Save the recovery codes offline (shown once).
7. Next password login asks for MFA before the session cookie is set.

Optional env:

```bash
# Optional; defaults to SESSION_SECRET-derived Fernet key
TOTP_ENC_KEY=
TOTP_ISSUER=CaseSifter
```

Disabling 2FA requires password **and** a current TOTP or recovery code. There is no
admin override path.

## Enable locally

### Shared (already required)

```bash
SESSION_SECRET=dev-session-secret-change-me
# Same value on casesifter-control and casesifter-analysis
```

Cookies: `cs_session` is HttpOnly; `COOKIE_SECURE` / `COOKIE_SAMESITE` follow `APP_ENV`
(production → Secure + SameSite=None for cross-site app↔control).

### SSO (OIDC)

```bash
OIDC_ISSUER=https://accounts.google.com
# or https://login.microsoftonline.com/<tenant>/v2.0
# or https://<okta-domain>/oauth2/default
OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=
OIDC_REDIRECT_URI=http://127.0.0.1:8001/v1/sso/oidc/callback
OIDC_SCOPES=openid email profile
# Optional: only allow these email domains
OIDC_ALLOWED_DOMAINS=yourfirm.com
# When true (default), SSO login skips local TOTP — IdP should enforce MFA
OIDC_TRUSTS_IDP_MFA=true
CASESIFTER_PUBLIC_URL=http://127.0.0.1:5173
```

Register the redirect URI on the IdP. Start SSO from Account (**Sign in with SSO**) or
`GET {CONTROL}/v1/sso/oidc/start`. Callback sets the session cookie and redirects to the app.

Password login remains available unless you choose to hide it in a future UI pass.
`sso_preferred` on the account is a soft preference stored for the admin panel.

### Admin panel

- Control plane: `/v1/admin/overview`, `/v1/admin/audit`, session revoke, settings.
- Analysis API (firm owner/admin role): `/api/v1/matters/admin`, `/api/v1/matters/admin/settings`.
- UI: **Admin** nav when signed in (account role owner/admin; default owner).

Sales-only token admin (`CONTROL_ADMIN_TOKEN` + `POST /v1/trial`) is unchanged and separate.

## Audit events

New `cp_auth_audit` actions (actor-scoped; no passwords/tokens/files):

- `login.mfa_required`, `login.mfa_failure`, `login.sso_success`, `login.sso_failure`
- `2fa.enroll_start`, `2fa.enroll_confirm`, `2fa.disable`, `2fa.recovery_used`
- `session.revoke`, `session.revoke_all`
- `admin.settings_view`, `admin.settings_update`

Firm matter audit (analysis DB) also records `admin.overview` and `firm.settings_update`.

## Gaps / follow-ups

- **SAML 2.0** — enterprise IdP matrix (follow-up; OIDC + JWKS is the soft-launch path)
- Per-firm OIDC client credentials (today: one control-plane OIDC app + optional domain allow-list)
- Desktop Electron MFA/SSO parity — **deferred**; `desktop/` is frozen / cut before broad launch

## Shipped in productize finish

- Multi-user seat invites + `owner` / `admin` / `member` role assignment (Admin → Invite seat)
- OIDC ID-token **JWKS signature verification** (`control_plane/oidc_auth.py`)
- SSO app redirect uses `?cs_code=` (one-time exchange), not `cs_token`
