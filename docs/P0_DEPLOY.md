# P0 deploy: Blueprint wiring and order

Live production was on `cursor/cloud-agent-1790049794515-xh7tp` at `f8ba1e6`.
`main` now includes that commit. After merging the P0 PR, switch Render **service auto-deploy** to `main`, then sync the Blueprint (see below).

## Branch base (confirmed)

| Ref | Contains `f8ba1e6`? |
|-----|---------------------|
| `origin/main` | yes (fast-forwarded from live branch) |
| `p0-security-privacy` | yes (P0 commits on top) |

## Blueprint branch + sync (required for `render.yaml` changes)

`render.yaml` is applied by a **Render Blueprint**, not by service auto-deploy alone.

| Question | Answer |
|----------|--------|
| Which branch must the Blueprint track? | **`main`** (same branch you switch service auto-deploy to after merge). Confirm in Render → Blueprint → Settings → **Connected branch**. If it still points at `cursor/cloud-agent-…`, change it to `main` before relying on these env wires. |
| Does merge to `main` apply `render.yaml` automatically? | **Only if Blueprint Auto Sync is On** for that Blueprint. Default for many Blueprints is On (push to the connected branch syncs). If Auto Sync is **Off**, open the Blueprint and click **Manual Sync** after the merge lands on `main`. |
| Service auto-deploy vs Blueprint sync | Auto-deploy rebuilds the Docker/static services from git. Blueprint sync updates env wiring (`fromDatabase`, `envVarGroups`, new keys). You need **both** for P0: merge → Blueprint sync → deploys. |

## What the Blueprint wires (no manual secret copy)

Defined in `render.yaml`:

1. **`envVarGroups.casesifter-auth`**
   - `SESSION_SECRET` with `generateValue: true` (set once; Blueprint re-sync does **not** rotate it).
   - Attached to **both** `casesifter-analysis` and `casesifter-control` via `fromGroup: casesifter-auth`.
   - Analysis fail-closed boot requires this env var — after sync, both services get it with **no Dashboard paste**.

2. **`casesifter-control` `DATABASE_URL`**
   - `fromDatabase: { name: casesifter-fact-memory, property: connectionString }`
   - That is the **internal** Postgres URL (private network; empty IP allow list is fine).

3. **`casesifter-analysis` `DATABASE_URL`**
   - Remains Dashboard `sync: false` (already set to the internal host). Not changed by this PR.

**Note on existing control `SESSION_SECRET`:** Blueprint cannot “adopt” a service-level secret into a new env group. First sync of `casesifter-auth` mints a new `SESSION_SECRET` (`generateValue`). You will be signed out once. Control prefers the **env** value and rewrites `/var/data/session_secret` to match so analysis and control stay aligned.

## Dashboard changes already made (do not overwrite)

- `casesifter-analysis` plan: **Standard (2 GB)** — blueprint does not set `plan:`
- `GEMINI_MODEL` / `GEMINI_API_KEY` / concurrency / memory: **Dashboard `sync: false` only**
- Postgres IP allow list: **empty** (internal host only)

## Deploy order (after merge + Blueprint sync)

1. **Merge PR #3 → `main`.**
2. **Point Blueprint connected branch at `main`** (if not already) and **sync** (auto or Manual Sync).
3. Confirm in Dashboard:
   - Env group **`casesifter-auth`** exists with `SESSION_SECRET` set.
   - `casesifter-analysis` and `casesifter-control` both show `SESSION_SECRET` from that group.
   - `casesifter-control` shows `DATABASE_URL` from `casesifter-fact-memory` (internal).
4. **Deploy order if deploys are staggered:** control first (migrates `accounts.json` → `cp_users`), then analysis (needs `SESSION_SECRET` to boot), then app/marketing.
5. **Smoke:** sign in as john@libertygroupfunding.com (password unchanged; expect one re-login after secret mint); matters visible; `curl …/api/agent-debug` → 404.

## Existing account: john@libertygroupfunding.com

On control boot with `DATABASE_URL` set:

1. Creates `cp_*` tables if missing.
2. Imports **missing** users from `/var/data/accounts.json` (SHA-256 + salt, `email_verified=true`).
3. Leaves `accounts.json` in place (idempotent re-run).
4. Next successful login upgrades password to **argon2id**.
5. Firm/matters stay attached by email.

## Postgres tenancy test (Render Shell on `casesifter-analysis`)

Isolated DB `casesifter_p0_eval` only — the suite refuses any `TEST_DATABASE_URL` that does not contain `p0_eval`.

```bash
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
        print("created")
    else:
        print("exists")
eng.dispose()
u = urlparse(live)
print(urlunparse(u._replace(path="/casesifter_p0_eval")))
PY

export TEST_DATABASE_URL="$(python - <<'PY'
import os
from urllib.parse import urlparse, urlunparse
live = os.environ["DATABASE_URL"]
if live.startswith("postgres://"):
    live = "postgresql://" + live[len("postgres://"):]
print(urlunparse(urlparse(live)._replace(path="/casesifter_p0_eval")))
PY
)"

cd /app
python -m pytest evals/test_postgres_tenancy.py -v --tb=short
```

## Do not do yet

- Reopen the 32 interrupted jobs until **P1** (queue deadlock) ships.
