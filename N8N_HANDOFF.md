# LawSifter → n8n handoff

Prepared: 2026-09-15. Product: **LawSifter** (repo `Courtdocverifier`).  
Purpose: wire case scans and material alerts into n8n without re-deriving product rules from scratch.

There are **no outbound webhooks today**. n8n must drive LawSifter over HTTP and **poll** until work finishes.

---

## 1. What LawSifter does (for workflow design)

- Case workspace: many filings/exhibits under one matter (`cases` + `documents` in SQLite).
- Each file scan runs a multi-engine pipeline (metadata, PDF integrity, structural, image forensics, OCR, contextual/Gemini).
- Attorney interrupts are **gotchas** and **tainted-exhibit** cards — not every finding.
- Product rule of thumb: paper scans / NYSCEF noise is ignored; only element / procedure / credibility / damages interrupts ship. Full policy: `.cursor/rules/materiality-thresholds.mdc`.

**Do not alert in n8n on:**

- “Referenced Exhibit X is not in this case” (exhibit letters change between filing and recording — suppressed).
- Loan-status number drift (note rate, principal, installment after default).
- Caption / NYSCEF header / filing-stamp vs body date / bare ELA scan texture / jurat-only “edited” cards.
- Two competing filings (advocacy), bare traverse vs affidavit of service.

**Do alert on:**

- `tainted_support` / title `Exhibit appears fraudulently edited` with a **concrete** `evidence_quote` / explanation (e.g. stamp year conflict on the page).
- Verified gotchas with `verified: true` and case-impact buckets element / procedure / credibility / damages.
- Document `risk_level` of `red` after a completed scan (still verify against gotcha list — risk alone can lag policy).

---

## 2. Runtime & base URL

| Env | UI | API |
| --- | --- | --- |
| Local | `http://127.0.0.1:5173` (Vite proxies `/api`) | `http://127.0.0.1:8000` |
| Production | *(set your host)* | Same host + `/api` **or** API host |

n8n HTTP nodes should call the **API origin** directly (e.g. `http://127.0.0.1:8000/api/...`), not the Vite UI, unless you deliberately proxy.

**Auth today:** local-dev entitlement is usually open (`GET /api/entitlement` → `active: true`, `plan: local-dev`). Scan routes call `_ensure_entitled()` — if entitlement is off, expect **402**. No bearer token in the default open mode.

**Concurrency:** scan slots are capped at **2** globally. Full-case rescan paces in the background; do not fire N parallel `/scan` calls from n8n.

---

## 3. API map (automation-relevant)

All paths below are under `/api`.

### Health / entitlement

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Liveness |
| GET | `/entitlement` | Gate before scanning |

### Cases

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/cases` | `{ "cases": [ ... ] }` |
| POST | `/cases` | Body: `caption`, optional `court`, `index_number`, `notes` |
| GET | `/cases/{case_id}` | Case + `documents[]` (status, risk_level, scan_job_id, …) |
| POST | `/cases/{case_id}/archive` | Soft archive |
| DELETE | `/cases/{case_id}` | Hard delete |

### Documents / scans

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/cases/{case_id}/documents` | Multipart upload into the case |
| POST | `/cases/{case_id}/documents/{document_id}/scan` | Queue one file → `{ job_id, status: "queued" }`; **409** if already scanning |
| POST | `/cases/{case_id}/rescan` | **Returns immediately**; paces all files two-at-a-time. Body none. Response: `{ status: "started"\|"idle", queued, skipped, ... }`. **409** if a full-case rescan is already running |
| GET | `/cases/{case_id}/documents/{document_id}` | Doc + snapshot + job progress |

### Jobs / reports

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/jobs/{job_id}` | Progress; `?include_report=true` when complete |
| GET | `/jobs/{job_id}/report` | Full report JSON; **409** until ready |
| GET | `/jobs/{job_id}/export/json\|html\|pdf` | Artifacts |

### Case interrupts (preferred alert source)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/cases/{case_id}/gotchas` | `{ gotchas: [...], fact_conflicts: [...] }` — filtered board cards |

---

## 4. Useful response shapes

### `POST /cases/{id}/rescan` (immediate)

```json
{
  "case_id": "cfa0d57b6c9f460ba8aa577d28271223",
  "queued": 61,
  "skipped": 0,
  "skipped_documents": [],
  "status": "started"
}
```

Then poll `GET /cases/{id}` until no document has `status` in `queued` | `scanning`.

### Document (on case payload)

Important fields: `id`, `display_name`, `doc_kind`, `status` (`uploaded` | `queued` | `scanning` | `complete` | `failed`), `risk_level` (`green` | `yellow` | `red` | null), `scan_job_id`.

### Gotcha / tainted card (from `/gotchas`)

Prefer these fields for Slack/email copy:

| Field | Use |
| --- | --- |
| `title` | e.g. `Exhibit appears fraudulently edited` |
| `evidence_quote` | **Concrete defect text** (prefer this over boilerplate) |
| `why_it_may_matter` / `attorney_why` | Why it matters |
| `tainted_support` | `true` → exhibit authenticity interrupt |
| `document_id` | Deep-link / open report |
| `verified` | Should be true for shipped cards |

Example message template:

```
[{{ $json.title }}]
{{ $json.evidence_quote }}
Why: {{ $json.why_it_may_matter || $json.attorney_why }}
Case: {{ $('Set Case').item.json.case_id }} · Doc: {{ $json.document_id }}
```

---

## 5. Suggested n8n workflows

### A. Watch a case for new material interrupts

1. **Cron** (e.g. every 5–15 min) or Manual Trigger  
2. HTTP GET `/api/cases/{case_id}/gotchas`  
3. Function/Code: keep items where `tainted_support === true` **or** (`verified === true` && title/severity not empty)  
4. Dedupe on `id` or hash(`document_id` + `title` + `evidence_quote`) via static data / Data Store  
5. Notify (Slack / email / SMS) with the template above  
6. Optional: HTTP GET `/api/jobs/{scan_job_id}/export/html` and attach link

### B. Ingest a file into a case and wait for the report

1. Webhook / form / Drive trigger with binary file + `case_id`  
2. HTTP POST multipart `/api/cases/{case_id}/documents`  
3. HTTP POST `/api/cases/{case_id}/documents/{document_id}/scan`  
4. Wait loop: GET `/api/jobs/{job_id}` every 15–30s until `status` is `complete` or `failed` (timeout ≥ 15–20 min for large PDFs)  
5. On complete: GET `/api/jobs/{job_id}/report` and/or case `/gotchas`  
6. Branch: if red / tainted → alert; else log quiet success

### C. Full-case rescan after a policy change

1. Manual / webhook with `case_id`  
2. GET `/api/entitlement` — abort if not `active`  
3. POST `/api/cases/{case_id}/rescan`  
4. Poll GET `/api/cases/{case_id}` until busy count (`queued`+`scanning`) is 0  
5. GET `/gotchas` → notify only **new** cards vs prior snapshot stored in n8n

**Do not** implement C by looping POST `/scan` for every document without pacing — the server already paces rescan; parallel client storms hang the API.

### D. Nightly health + entitlement check

1. Cron  
2. GET `/health` + GET `/entitlement`  
3. Alert only on failure / `active: false`

---

## 6. Polling cheat sheet

| Wait for | Poll | Done when |
| --- | --- | --- |
| One job | `GET /jobs/{job_id}` | `status ∈ {complete, failed}` |
| Case quiet after rescan | `GET /cases/{case_id}` | no docs with `status ∈ {queued, scanning}` |
| Board updates | `GET /cases/{case_id}/gotchas` | after case is quiet |

Suggested intervals: **15–30s** job poll; **30–60s** case quiet poll. Full Alexander-sized matters (60+ PDFs) can run a long time at 2 concurrent scans.

---

## 7. Local run (for n8n on the same machine)

```powershell
# API
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 127.0.0.1 --port 8000

# UI (optional)
cd frontend
npm run dev
```

n8n → `http://127.0.0.1:8000`. If n8n runs in Docker on Windows, use `http://host.docker.internal:8000` instead of `127.0.0.1`.

Project root `.env` (gitignored): `GEMINI_API_KEY`, prefer `GEMINI_MODEL=gemini-3.6-flash`, `CONTEXTUAL_MULTIMODAL=true` only when court rules allow sending the exhibit.

---

## 8. Product rules n8n must not fight

Canonical rule file: `.cursor/rules/materiality-thresholds.mdc`.

Recent product decisions (2026-09):

| Decision | Implication for n8n |
| --- | --- |
| Suppress missing exhibit-letter alerts | Drop `field === "missing_exhibit"` / titles like “Referenced Exhibit … is not in this case” |
| Tainted cards need concrete defect text | Prefer `evidence_quote` / underlying date/math/template findings; ignore generic “appears fraudulently edited” alone |
| Case **Rescan all** is paced server-side | One POST `/rescan`, then poll; don’t fan out |
| Scan texture / packaging not interrupts | Don’t page on ELA-only or metadata-only findings |

---

## 9. Gaps / ask engineering next

1. **Outbound webhook** on `job complete` and `gotcha created` would remove polling (ideal n8n Trigger).  
2. **API key / auth** for any non-localhost n8n.  
3. **Stable gotcha IDs** for reliable dedupe across rescans (today hash title+quotes if needed).  
4. Commit/push uncommitted UI+API changes (`rescan`, missing-exhibit suppress, tainted copy) before relying on a remote host.

---

## 10. Smoke test (copy into n8n or curl)

```http
GET {{base}}/api/health
GET {{base}}/api/entitlement
GET {{base}}/api/cases
GET {{base}}/api/cases/{{case_id}}/gotchas
POST {{base}}/api/cases/{{case_id}}/rescan
GET {{base}}/api/cases/{{case_id}}
```

Expect: health 200, entitlement active, gotchas JSON, rescan `status: "started"` in &lt;1s, then documents eventually leave `scanning`.

---

## 11. Key code pointers

| Area | Path |
| --- | --- |
| HTTP API | `backend/app/api.py` |
| Case DB | `backend/app/cases_db.py` |
| Gotchas / tainted cards | `backend/app/gotcha.py` |
| Finding materiality | `backend/app/finding_policy.py` |
| Case consistency | `backend/app/case_consistency.py` |
| Pipeline | `backend/app/pipeline.py` |
| UI case workspace + Rescan all | `frontend/src/App.jsx` |
| Materiality policy | `.cursor/rules/materiality-thresholds.mdc` |

Older overview (partly stale on storage — cases now use SQLite `backend/data/cases.db`): `HANDOFF.md`.
