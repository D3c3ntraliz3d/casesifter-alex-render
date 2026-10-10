# P0 deploy: env vars and order

Live production was on `cursor/cloud-agent-1790049794515-xh7tp` at `f8ba1e6`.
`main` now includes that commit. After merging the P0 PR, switch Render auto-deploy to `main`.

## Branch base (confirmed)

| Ref | Contains `f8ba1e6`? |
|-----|---------------------|
| `origin/main` | yes (fast-forwarded from live branch) |
| `p0-security-privacy` | yes (7 P0 commits on top) |

## Dashboard changes already made (do not overwrite)

- `casesifter-analysis` plan: **Standard (2 GB)** — blueprint no longer sets `plan:`
- `GEMINI_MODEL` / `GEMINI_API_KEY` / concurrency / memory: **Dashboard `sync: false` only**
- Postgres IP allow list: **empty** (internal host only)

## Env vars to add on `casesifter-control` (before deploy)

Copy from `casesifter-analysis` Dashboard where noted.

| Key | Value | Notes |
|-----|--------|--------|
| `DATABASE_URL` | **Internal** Postgres URL for `casesifter-fact-memory` (same DB analysis uses) | Must be the *internal* hostname (Render private network). Do not use the external URL; IP allow list is empty. |
| `SESSION_SECRET` | **Identical** to `casesifter-analysis` `SESSION_SECRET` | Already on control disk pin; confirm Dashboard values match analysis. |
| `APP_ENV` | `production` | Blueprint sets this; confirm. |
| `CONTROL_DATA_DIR` | `/var/data` | Keep disk mounted — needed for one-time `accounts.json` → `cp_users` migration and `session_secret` pin. |

Optional (only if you want email delivery later): `RESEND_API_KEY`. Without it, production will not email verify/reset links (existing accounts are grandfathered verified).

## Existing account: john@libertygroupfunding.com

On control boot with `DATABASE_URL` set:

1. Creates `cp_*` tables in the shared Postgres if missing.
2. Imports missing users from `/var/data/accounts.json` into `cp_users` (SHA-256 + salt preserved, `email_verified=true`).
3. Renames the JSON to `accounts.json.migrated` after a successful import.
4. Next successful login upgrades the password to **argon2id**.
5. Analysis firm binding still finds your firm/matters by **email** (`ensure_firm_for_email`), so matters stay attached.

## Deploy order (sign-in must not break mid-deploy)

1. **Dashboard first (no deploy yet)**  
   - On `casesifter-control`: set `DATABASE_URL` (internal) + confirm `SESSION_SECRET` matches analysis.  
   - On `casesifter-analysis`: confirm `SESSION_SECRET` is set (same value). Do not change `DATABASE_URL`.

2. **Deploy `casesifter-control` first**  
   - Boot migrates `accounts.json` → Postgres `cp_users`.  
   - Sign-in still works against control; analysis may still use the old entitlement probe until step 3.

3. **Deploy `casesifter-analysis` (P0 build)**  
   - Local session HMAC + entitlement split go live.  
   - If control is healthy, entitlement probe succeeds; if control blips, reads stay authenticated (503 only on write when entitlement cannot be refreshed).

4. **Deploy `casesifter-app` / marketing** (frontend: `cs_code` handoff, logout revoke).

5. **Smoke**  
   - Sign in as john@libertygroupfunding.com (password unchanged).  
   - Confirm matters list still shows your cases.  
   - Sign out → old bearer gets 401 on `/v1/entitlement`.  
   - `curl https://casesifter-analysis.onrender.com/api/agent-debug` → 404.

## Do not do yet

- Reopen the 32 interrupted jobs matter until **P1** (queue deadlock) ships.
- Point Render auto-deploy at `main` until this PR is merged and the checklist below passes.
