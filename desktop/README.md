# CaseSifter desktop (thin client)

> **Frozen — cut before broad launch.** Soft-launch is the hosted web app
> (`docs/HANDOFF.md`). Do not add Electron features; delete this folder before
> general availability. Local vault (`APP_MODE=vault`) remains for John’s testing.

Local case files + remote ephemeral analysis. **Does not ship** `gotcha.py` / pipeline engines.

## Run (dev)

1. Analysis API (keeps engines server-side):

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
$env:EPHEMERAL_JOBS="true"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

2. Control plane (billing / trial):

```powershell
cd control_plane
pip install -r requirements.txt
$env:CONTROL_ADMIN_TOKEN="dev-admin"
uvicorn main:app --host 127.0.0.1 --port 8010
```

3. UI:

```powershell
cd frontend
npm run dev
```

4. Desktop shell:

```powershell
cd desktop
npm install
npm start
```

The Electron window loads `http://127.0.0.1:5173`. Cases are stored under Electron `userData/CaseSifterData`.

## Ship Windows installer (trial / paid)

Production builds embed the Vite UI and bake analysis + control plane URLs into [`packaged-defaults.json`](packaged-defaults.json).

```powershell
cd frontend
npm ci

cd ..\desktop
npm ci

# Optional overrides (otherwise prepare-ui writes Render placeholders):
# $env:CASESIFTER_ANALYSIS_URL="https://your-analysis.onrender.com"
# $env:CASESIFTER_CONTROL_URL="https://your-control.onrender.com"
# Real Sentry DSN at pack time (UI + Electron main). Do not commit the value.
# $env:VITE_SENTRY_DSN="https://…@o….ingest.sentry.io/…"
# $env:CASESIFTER_SENTRY_DSN=$env:VITE_SENTRY_DSN
# $env:VITE_SENTRY_ENVIRONMENT="production"

npm run dist:win
```

Output: `desktop/dist/CaseSifter-Setup-<version>.exe` (NSIS).

### Sentry (pack + analysis API)

- **Desktop UI**: set `VITE_SENTRY_DSN` when running `prepare-ui` / `dist:win` (GitHub Actions: `secrets.VITE_SENTRY_DSN`). Events tag `service=casesifter-ui`.
- **Electron main**: same DSN via `CASESIFTER_SENTRY_DSN` / `packaged-defaults.json` `sentryDsn`. Tag `service=casesifter-desktop`.
- **Analysis API**: set `SENTRY_DSN` on the Render service (`render.yaml` lists it as a sync secret). Tag `service=analysis-api`.

No smoke-test throw remains in the tree — after end-to-end verification, production builds only report real errors.

### Release to the website

1. Update version in [`package.json`](package.json) if needed.
2. Push a tag: `git tag desktop-v1.0.0 && git push origin desktop-v1.0.0`
3. GitHub Actions (`.github/workflows/desktop-release.yml`) builds the NSIS and attaches it to a GitHub Release.
4. Point marketing at the asset in [`marketing/assets/downloads.json`](../marketing/assets/downloads.json) (update `url` / `version` / `filename` to match the release).
5. Confirm [`marketing/download.html`](../marketing/download.html) shows the button (loads that JSON).

### Windows SmartScreen — required before you sell downloads

You **cannot** sell a normal Windows download without Authenticode signing. Browsers and SmartScreen will keep blocking unsigned `.exe` files; “Keep anyway” is only for internal testers.

Pick **one** path, then ship a new `desktop-v*` tag:

#### Option A (recommended): Azure Trusted Signing

Cloud signing, no USB token. Good fit for GitHub Actions.

1. In Azure: create a Trusted Signing account + identity validation + certificate profile ([docs](https://learn.microsoft.com/en-us/azure/trusted-signing/)).
2. Create an App registration; grant it **Trusted Signing Certificate Profile Signer** on that account; create a client secret.
3. GitHub → Settings → Secrets and variables → Actions — add:
   - `AZURE_TRUSTED_SIGNING_ENDPOINT` — region URI (e.g. `https://eus.codesigning.azure.net`)
   - `AZURE_TRUSTED_SIGNING_ACCOUNT` — account name
   - `AZURE_TRUSTED_SIGNING_PROFILE` — certificate profile name
   - `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`
4. Optional repo **variable** `REQUIRE_SIGNED_DESKTOP=true` so CI fails if a release is still unsigned.
5. Bump version, `git tag desktop-vX.Y.Z && git push origin desktop-vX.Y.Z`.
6. Point [`marketing/assets/downloads.json`](../marketing/assets/downloads.json) at the new signed `CaseSifter-Setup-X.Y.Z.exe`.

#### Option B: OV code signing `.pfx` (DigiCert / Sectigo / SSL.com)

1. Buy an **organization-validated** code signing cert in your legal entity name; export `.pfx`.
2. Secrets: `CSC_LINK` = base64 of the PFX (`[Convert]::ToBase64String([IO.File]::ReadAllBytes('cert.pfx'))` in PowerShell), `CSC_KEY_PASSWORD` = PFX password.
3. Same tag → release → update `downloads.json` as above.

New publisher reputation still settles over the first wave of downloads after signing; unsigned builds never become “normal.”

**Internal tester override only (do not tell customers this):** Edge/Chrome → Keep anyway; or Properties → Unblock; or SmartScreen → More info → Run anyway.

Trial / Subscribe still happens **in-app** (Account), not at download time.

## Config (userData `casesifter-config.json`)

| Key | Purpose |
| --- | --- |
| `analysisApiUrl` | Analysis API base (dev: `http://127.0.0.1:8000`; packaged: from `packaged-defaults.json`) |
| `controlPlaneUrl` | Control plane (dev: `http://127.0.0.1:8010`) |
| `analysisApiKey` | Optional `X-API-Key` if `ANALYSIS_API_KEYS` is set on the API |
| `sessionToken` | Set by Sign in / Start trial |
| `uiUrl` | Dev only — packaged builds load `renderer/app/index.html` |

Override UI at runtime with `CASESIFTER_UI_URL` even in packaged builds.

## Privacy

- Documents remain on the firm PC.
- Scan uploads are ephemeral; source bytes are removed after the job finishes; job dirs are swept by TTL.

## Packaged defaults

[`packaged-defaults.json`](packaged-defaults.json) is rewritten by `npm run prepare-ui` from `CASESIFTER_ANALYSIS_URL` / `CASESIFTER_CONTROL_URL` (with onrender.com placeholders if unset). Commit updates when production service URLs change so local `dist:win` matches the website build.
