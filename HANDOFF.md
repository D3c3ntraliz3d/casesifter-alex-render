# Project Status (Handoff)

> **For n8n / automation wiring, use [`N8N_HANDOFF.md`](./N8N_HANDOFF.md)** (API map, poll patterns, alert filters, workflow recipes). This file is an older product overview and is partly stale (cases now live in SQLite; LawSifter case UI + gotchas are primary).

Court Exhibit Scanner / **LawSifter** — automated indicators of document alteration for court exhibit review. Decision-support only; it does not determine authenticity, fraud, or admissibility.

---

## What's Built

- **6-engine document fraud detection system**, orchestrated by `backend/app/pipeline.py`:

  | Engine | Module | Entry point |
  | --- | --- | --- |
  | Metadata | `engines/metadata.py` | `analyze_metadata` |
  | PDF integrity | `engines/pdf_integrity.py` | `analyze_pdf_integrity` |
  | Structural | `engines/structural.py` | `analyze_structural` |
  | Image forensics | `engines/image_forensics.py` | `analyze_image_forensics` |
  | OCR / text | `engines/ocr_text.py` | `analyze_ocr_text` |
  | Contextual | `engines/contextual_logic.py` | `analyze_contextual` |

- **Tier 1 — Local forensics** (metadata, PDF integrity, structural, image forensics, OCR/text, plus local entity/timeline checks in contextual): **WORKING**
- **Tier 2 — Entity timeline verification via Gemini** (questions only, no document content): **BUILT and exercised** on real jobs; never positively confirmed with a contradiction finding on the bank/utility exhibit (`external_verification: no_claims`)
- **Tier 3 — Comprehensive document analysis via Gemini**: **WORKING** on multi-statement exhibits. After removing National Grid/KeySpan/Brooklyn prompt priming (job `67e297f11e96462e92ac876343daf7a4`), tier 3 still found Verizon date issues and National Grid math, but **did not** emit `historical_impossibility` — that category had been prompt-coached. Tier 2 still returned a National Grid Brooklyn public-record contradiction when asked the generic “operating in that market on that date?” claim (no KeySpan dates in the prompt). Citibank spelling/math remain intermittent vs Gemini web.

---

## Current State

| Layer | Status | Notes |
| --- | --- | --- |
| Frontend | Working | React 18 + Vite upload UI, visual progress bar + stage list, 5-tab report viewer (`frontend/src/App.jsx`) |
| Backend | Working for MVP demo | FastAPI with 6 engines + native-PDF Gemini tier 3 validated on live exhibit |
| Storage | Working | Flat files under `backend/data/jobs/{job_id}/` (not a SQL DB) |
| Report HTML | Working | Court-oriented HTML + PDF + JSON export; AI labels in HTML detailed findings only |

### Storage layout (per job)

```
backend/data/jobs/{job_id}/
  progress.json
  report.json
  report.html
  report.pdf
  {uploaded_source_file}
  artifacts/          # ELA / copy-move heatmaps (*.png)
```

---

## What's Working

- **PDF integrity** — JavaScript, attachments, layers, unapplied redactions (`pdf_integrity.py`)
- **Metadata** — timestamps, authorship, editing software anomalies (`metadata.py`)
- **Image forensics** — Error Level Analysis (ELA) and copy-move/clone detection with heatmaps (`image_forensics.py`). ELA flags uneven JPEG recompression / splicing, **not** text overlays.
- **Text / OCR** — Embedded PDF text via pypdf; Tesseract for images or low-text PDFs (`ocr_text.py`). Invisible text, micro text, OCR-vs-PDF mismatch checks live here. Tesseract binary is a separate install; without it the module emits “OCR engine not available” and continues.
- **Structural** — page size / orientation / resolution consistency
- **Report generation** — Jinja2 HTML + ReportLab PDF; PDF failure is caught so HTML still writes
- **`.env` loading** — `config.py` resolves `.env` at the **project root** via absolute path (`Path(__file__).parent.parent.parent / ".env"`)
- **Gemini API** — confirmed sending/receiving on real jobs with `gemini-3.6-flash`. One earlier job failed with HTTP 404: `models/gemini-2.5-flash is no longer available to new users` — pin a 3.x model in `.env`; do not rely on the `config.py` default of `gemini-2.5-flash` for new keys.

### Engine split (do not conflate)

There is no single “PDF forensics” module. Common capabilities map as:

| Capability | Module |
| --- | --- |
| Embedded JavaScript | `pdf_integrity.py` |
| Metadata anomalies | `metadata.py` |
| Error Level Analysis | `image_forensics.py` |
| Text overlays / invisible text | `ocr_text.py` (+ optional Gemini `text_overlay` in tier 3) |

---

## What Needs Work

1. **Prompt tension (watchlist)** — `FRAUD_PROMPT` still mixes aggressive fraud posture with “return empty when ordinary.” Native PDF now finds real leads on the bank/utility exhibit; revisit only if clean exhibits start flooding speculative findings.
2. **Docker env** — `docker-compose.yml` now passes `GEMINI_*` / `CONTEXTUAL_*` from the host `.env` via `env_file` + explicit environment mappings. Rebuild containers after changing `.env`.
3. **No automated tests** — No `tests/`, `pytest`, or `conftest.py` in the repo.
4. **Later product (not MVP)** — Multi-document court-filing vs exhibit contradiction engine; auth/billing/SaaS.

---

## Next Immediate Task

**Closed (native PDF live validation):** Job `e646bb3371d8464199f62fd5bfb46f17` with `CONTEXTUAL_MULTIMODAL=true` and `gemini-3.6-flash`:

- `document_analysis_result.file_sent == "application/pdf"`
- `count: 2` (date inconsistency + math error on Verizon statement page 5)
- Prior image-mode baseline: five runs at `count: 0` / `pages_sent: 4`

**Suggested follow-ups for selling to attorneys:**

1. Demo a clean exhibit to confirm low false-positive rate under the aggressive prompt.
2. Add auth / multi-tenant packaging when the single-exhibit scan path is demo-ready.
3. Defer multi-doc court-filing vs evidence contradictions until single-PDF fraud scanning is productized.

---

## Architecture

```
Upload → pipeline.run_pipeline
           ├─ metadata
           ├─ pdf_integrity
           ├─ structural
           ├─ image_forensics  → artifacts/*.png
           ├─ ocr_text         → full_text popped into contextual
           └─ contextual_logic
                ├─ Tier 1: entities + timeline (local)
                ├─ Tier 3: native file + text → Gemini (if CONTEXTUAL_MULTIMODAL + key)
                └─ Tier 2: claim questions → Gemini (if key; skipped after tier-3 error)
           → score_risk → report.html / report.pdf / report.json
```

| File | Role |
| --- | --- |
| `backend/app/pipeline.py` | Orchestrates engines; `_source_for_contextual` gates file handoff |
| `backend/app/engines/contextual_logic.py` | Tier 1/2/3; `_encode_source_file`, `analyze_document` |
| `backend/app/config.py` | Pydantic settings from project-root `.env` |
| `backend/app/models.py` | `Finding`, `Report`, `ModuleResult`, etc. |
| `backend/app/report/generator.py` | HTML (Jinja2) + PDF (ReportLab) |
| `backend/app/api.py` | `/api/upload`, `/api/jobs/{id}`, exports |
| `frontend/src/App.jsx` | Upload, poll (~900 ms), tabs, export links |

### AI finding safety rails

Model-sourced findings are capped at **medium** severity, confidence discounted (~×0.7) then hard-capped at **60%**, marked `evidence.ai_generated: true`, filed under category `contextual`. Scoring requires ≥70% for red — no AI finding alone can turn a report red.

### API (no auth today)

Do not expose `:8000` publicly without a reverse proxy or auth layer.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness |
| POST | `/api/upload` | Multipart `file` + optional `submitted_by` |
| GET | `/api/jobs/{id}` | Progress; report null until complete |
| GET | `/api/jobs/{id}/report` | Full report JSON |
| GET | `/api/jobs/{id}/export/html\|pdf\|json` | Downloadable artifacts |

---

## Configuration (`.env` required)

Place `.env` at the **project root** (same level as `docker-compose.yml`). It is gitignored — never commit keys.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | empty | Enables tier 2 (and is required for tier 3) |
| `GEMINI_MODEL` | `gemini-2.5-flash` in code — **override** | Prefer a 3.x model available to your key (e.g. `gemini-3.6-flash`) |
| `CONTEXTUAL_MULTIMODAL` | `false` | Second opt-in: uploads the exhibit file to Google |
| `CONTEXTUAL_MAX_UPLOAD_MB` | `15` | Oversize → text-only analysis + note in extras |
| `CONTEXTUAL_MAX_AI_FINDINGS` | `12` | Cap on AI findings kept per scan |
| `GEMINI_DOCUMENT_TIMEOUT_S` | `90` | Tier 3 HTTP timeout |
| `CONTEXTUAL_MAX_CHARS` | `40000` | Text window for entities / prompt |
| `CONTEXTUAL_MAX_CLAIMS` | `5` | Tier 2 claim budget |
| `CONTEXTUAL_MIN_CONFIDENCE` | `70` | Model confidence floor before a finding is kept |
| `CONTEXTUAL_WINDOW` | `400` | Entity pairing distance for claims |

**Privacy reminder:** Tier 2 sends only a short generated question. Tier 3 sends the exhibit itself. Do not enable `CONTEXTUAL_MULTIMODAL` for sealed, privileged, or otherwise restricted evidence without confirming court rules.

Get a key: [Google AI Studio](https://aistudio.google.com/apikey).

### Run locally

```powershell
# Backend
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm run dev
```

UI (Vite): http://localhost:5173 · API docs: http://localhost:8000/docs

```powershell
docker compose up --build
```

UI: http://localhost:8080 · API: http://localhost:8000 — remember Gemini vars may need explicit compose passthrough.

---

## Known job history (context for the next person)

Under `backend/data/jobs/` there are ~24 prior runs. Recent contextual results on the bank/utility exhibit:

- One failure: `gemini-2.5-flash` → HTTP 404 (model unavailable to new users)
- Five successes with `gemini-3.6-flash`, image mode: `document_analysis: enabled`, **`count: 0`**, `pages_sent: 4`, `external_verification: no_claims`

Use `file_sent: "application/pdf"` vs `pages_sent` to distinguish a native-PDF run from those legacy jobs.
