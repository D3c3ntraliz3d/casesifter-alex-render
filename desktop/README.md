# CaseSifter desktop (thin client)

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

Unsigned builds may trigger Windows SmartScreen until code signing is configured (`CSC_*` secrets). Trial / Subscribe still happens **in-app** (Account), not at download time.

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
