# Court Exhibit Scanner

Automated indicators of document alteration for court exhibit review. Phase 1 covers metadata, image forensics, OCR/text, PDF structure, and page-level anomalies. The software is a **decision-support** tool — it does not determine authenticity, fraud, or admissibility.

## What it does

Upload a PDF, Word, Excel, or image exhibit. The scanner:

1. Extracts timestamps, authors, and editing software
2. Checks PDF layers, JavaScript, attachments, and unapplied redactions
3. Compares page sizes, rotation, and scan resolution
4. Runs JPEG error-level analysis and copy-move (clone) detection, with heatmaps
5. Extracts text, fonts, and hidden/invisible text
6. Extracts entities (organizations, dates, locations) and checks timeline logic
7. Produces a Green / Yellow / Red report with confidence scores, a timeline, and HTML / PDF / JSON export

Informational notes (for example, missing EXIF on a phone screenshot) do **not** by themselves raise the risk level.

## Quick start (local)

**Backend** (Python 3.10+):

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Optional: install [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) for image text extraction. PDFs with embedded text still analyze without it.

### Contextual verification (opt-in, off by default)

The contextual engine always extracts entities and checks timeline logic locally. It can
additionally ask Google Gemini whether a company/place/date combination is contradicted by
public record. That step is **disabled unless you set an API key**, because it sends data to
a third party:

```powershell
$env:GEMINI_API_KEY = "your-key"
```

Only the generated question is transmitted (for example, *Was "Acme Freight, LLC" in
operation, and present at Dallas, TX, on 2019-03-01?*). Document text, page images, and the
file itself never leave the server. Model-sourced findings are capped at medium severity,
carry `ai_generated: true`, and cannot by themselves drive a report to red.

**Frontend**:

```powershell
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The Vite dev server proxies `/api` to the backend.

## Docker

```powershell
docker compose up --build
```

UI: http://localhost:8080 · API: http://localhost:8000/docs

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/upload` | Multipart file upload; starts analysis |
| POST | `/api/analyze/{id}` | Re-run analysis on an uploaded exhibit |
| GET | `/api/jobs/{id}` | Status, progress, and report when complete |
| GET | `/api/report/{id}` | Full JSON report (alias of `/api/jobs/{id}/report`) |
| GET | `/api/jobs/{id}/report` | Full JSON report |
| GET | `/api/jobs/{id}/export/html` | Court-record HTML |
| GET | `/api/jobs/{id}/export/pdf` | Printable PDF |
| GET | `/api/jobs/{id}/export/json` | Integration payload |

Max file size defaults to 100 MB (`MAX_FILE_MB`).

## Project layout

```
backend/app/engines/   metadata, image_forensics, ocr_text, pdf_integrity, structural, contextual_logic
backend/app/report/    scoring, HTML/PDF export
frontend/src/           upload, progress, report viewer
```

## Phase 2 (not in this build)

Batch upload, user login / roles, court CMS integration, cryptographic signature verification, and trained deepfake models.

## Handling evidence

Store scans with the case file. High-risk findings should be reviewed by staff; the system does not auto-escalate. Follow local rules on electronic evidence (including FRCP 26 where applicable) and your court's exhibit procedures.
