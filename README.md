# CaseSifter

Decision-support for court filings: browser web app + proprietary analysis on an ephemeral cloud API (process then delete — **no document vault**).

## Hybrid model

| Piece | Where |
| --- | --- |
| Case PDFs, index, UI | **Web app** (`frontend/` → firm browser) |
| Pipeline / gotcha / OCR / prompts | **Analysis API** (`backend/` on Render) |
| Login / trial / Stripe | **Control plane** (`control_plane/`) |

Privacy pitch: we do not keep a document library. Files used for analysis are processed and removed.

See [docs/RENDER_HYBRID.md](docs/RENDER_HYBRID.md), [marketing/index.html](marketing/index.html), [marketing/signup.html](marketing/signup.html), and [docs/DMS_PHASE2.md](docs/DMS_PHASE2.md).

## End-user signup (web)

Marketing CTAs point to **Sign up** (`marketing/signup.html` → control-plane `POST /v1/signup`). The desktop installer funnel is retired (`download.html` redirects to signup).

Live marketing (current): `https://casesifter-marketing.onrender.com`  
Custom domain `casesifter.com` / `www.casesifter.com` may still be on **SiteGround** until DNS is moved to this Render static service — see custom-domain steps in [docs/RENDER_HYBRID.md](docs/RENDER_HYBRID.md).

## Quick start (local web / ops)

**Backend** (Python 3.10+):

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Optional: install [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) for scan text-of-record.

**Frontend**:

```powershell
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. Vite proxies `/api` to the backend.

**Desktop thin client** (local store + remote analyze):

```powershell
cd desktop
npm install
npm start
```

**Control plane** (trial / Stripe):

```powershell
cd control_plane
pip install -r requirements.txt
$env:CONTROL_ADMIN_TOKEN="dev-admin"
uvicorn main:app --host 127.0.0.1 --port 8010
```

### Contextual verification (opt-in)

Tier 2 needs `GEMINI_API_KEY`. Tier 3 also needs `CONTEXTUAL_MULTIMODAL=true` and sends the file — do not enable for sealed or privileged evidence.

## Ephemeral analysis API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/jobs` | Upload bytes; start ephemeral job |
| GET | `/api/v1/jobs/{id}` | Progress (+ report when complete) |
| DELETE | `/api/v1/jobs/{id}` | Purge job directory |
| POST | `/api/v1/sweep` | TTL sweeper |

Legacy case endpoints remain for local ops; customer product storage is desktop-local.

## Docker / Render

```powershell
docker compose up --build
```

Blueprint: [render.yaml](render.yaml) — analysis API, control plane, and static marketing site (`casesifter-marketing` → `./marketing`).

## Handling evidence

Store scans with the case file. High-risk findings should be reviewed by staff; the system does not auto-escalate.
