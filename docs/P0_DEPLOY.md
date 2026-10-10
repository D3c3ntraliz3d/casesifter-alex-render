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

## Env vars (before deploy)

### On `casesifter-analysis` (required — currently missing)

| Key | Value | Notes |
|-----|--------|--------|
| `SESSION_SECRET` | **Identical** to control's `SESSION_SECRET` | **Fail closed:** production analysis **refuses to boot** if empty; browser sessions are always rejected without it (no default secret, no probe-only identity). |

### On `casesifter-control`

| Key | Value | Notes |
|-----|--------|--------|
| `DATABASE_URL` | **Internal** Postgres URL for `casesifter-fact-memory` (same DB analysis uses) | Must be the *internal* hostname (Render private network). Do not use the external URL; IP allow list is empty. |
| `SESSION_SECRET` | Already set — keep; copy **this value** onto analysis | Disk pin at `/var/data/session_secret` also applies. |
| `APP_ENV` | `production` | Blueprint sets this; confirm. |
| `CONTROL_DATA_DIR` | `/var/data` | Keep disk mounted — needed for `accounts.json` → `cp_users` migration and `session_secret` pin. |

Optional (only if you want email delivery later): `RESEND_API_KEY`. Without it, production will not email verify/reset links (existing accounts are grandfathered verified).

## Existing account: john@libertygroupfunding.com

On control boot with `DATABASE_URL` set:

1. Creates `cp_*` tables in the shared Postgres if missing.
2. Imports **missing** users from `/var/data/accounts.json` into `cp_users` (SHA-256 + salt preserved, `email_verified=true`).
3. **Leaves `accounts.json` in place** (never deletes/renames) so a mid-deploy failure can re-run safely; already-imported emails are skipped (idempotent).
4. Next successful login upgrades the password to **argon2id**.
5. Analysis firm binding still finds your firm/matters by **email** (`ensure_firm_for_email`), so matters stay attached.

## Deploy order (sign-in must not break mid-deploy)

1. **Dashboard first (no deploy yet)**  
   - On `casesifter-control`: set `DATABASE_URL` (internal).  
   - On `casesifter-analysis`: **add** `SESSION_SECRET` = exact same value as control. Do not change analysis `DATABASE_URL`.

2. **Deploy `casesifter-control` first**  
   - Boot migrates `accounts.json` → Postgres `cp_users` (idempotent).  
   - Sign-in still works against control; analysis may still use the old entitlement probe until step 3.

3. **Deploy `casesifter-analysis` (P0 build)**  
   - Boots only if `SESSION_SECRET` is set (production fail-closed).  
   - Local session HMAC + entitlement split go live.

4. **Deploy `casesifter-app` / marketing** (frontend: `cs_code` handoff, logout revoke).

5. **Smoke**  
   - Sign in as john@libertygroupfunding.com (password unchanged).  
   - Confirm matters list still shows your cases.  
   - Sign out → old bearer gets 401 on `/v1/entitlement`.  
   - `curl https://casesifter-analysis.onrender.com/api/agent-debug` → 404.

## Postgres tenancy test (Render Shell on `casesifter-analysis`)

Uses an isolated DB named `casesifter_p0_eval` — **not** live data. The test refuses any `TEST_DATABASE_URL` that does not contain `p0_eval`.

```bash
# 1) Create isolated DB once (safe to re-run)
python - <<'PY'
import os
from sqlalchemy import create_engine, text
from urllib.parse import urlparse, urlunparse

live = os.environ["DATABASE_URL"]
if live.startswith("postgres://"):
    live = "postgresql://" + live[len("postgres://"):]
eng = create_engine(live, isolation_level="AUTOCOMMIT", future=True)
with eng.connect() as c:
    exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname='casesifter_p0_eval'")).scalar()
    if not exists:
        c.execute(text("CREATE DATABASE casesifter_p0_eval"))
        print("created casesifter_p0_eval")
    else:
        print("casesifter_p0_eval already exists")
eng.dispose()
u = urlparse(live)
print("TEST_DATABASE_URL=" + urlunparse(u._replace(path="/casesifter_p0_eval")))
PY

# 2) Export the printed URL (or rebuild it), then run tests
export TEST_DATABASE_URL="$(python - <<'PY'
import os
from urllib.parse import urlparse, urlunparse
live = os.environ["DATABASE_URL"]
if live.startswith("postgres://"):
    live = "postgresql://" + live[len("postgres://"):]
u = urlparse(live)
print(urlunparse(u._replace(path="/casesifter_p0_eval")))
PY
)"

# 3) Run only the Postgres tenancy suite (working dir = backend image root)
cd /app   # Render Docker WORKDIR for casesifter-analysis
python -m pytest evals/test_postgres_tenancy.py -v --tb=short
```

Optional cleanup after you are done: `DROP DATABASE casesifter_p0_eval;` via the same Python/`AUTOCOMMIT` pattern.

## Do not do yet

- Reopen the 32 interrupted jobs matter until **P1** (queue deadlock) ships.
- Point Render auto-deploy at `main` until this PR is merged and the checklist below passes.
