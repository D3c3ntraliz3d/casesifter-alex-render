# HANDOFF v2 — CaseSifter: Web App with Local Files

**Replaces:** `HANDOFF-web-architecture.md` (v1 was written without seeing the code; this version was written after reading it).
**Repo:** `johnnyg123456/court-exhibit-scanner`
**Code reviewed:** local copy of `origin/cursor/cloud-agent-1790049794515-xh7tp` at **`95d11f1` (Sept 23)**. The branch tip on GitHub is `c55f027`, which is newer. **Cursor: diff `95d11f1..c55f027` first and correct anything below that has changed.**

**Goal (unchanged):** a hosted web app where the firm's original files stay on their computer. The server keeps reports, extracted facts and fingerprints, but never the originals. Clicking a finding opens the exact page of the local file.

---

## Part A — What exists today (verified in code)

### A1. Two app modes already exist

| Mode | Client | Server path | Where originals live | Cross-doc checks |
|---|---|---|---|---|
| **Case vault** (original web UI) | `frontend/` (React/Vite) | `/api/cases/...`, `/api/upload` | **Kept on the server for good** in `data/cases/{case_id}/documents/` (`cases_db.case_dir`) | **Yes**: `compare_to_case` + `find_gotchas` |
| **Hybrid / ephemeral** (current product direction) | `desktop/` (Electron) | `/api/v1/jobs` | **Copied** into Electron `userData/CaseSifterData/cases/...` (`local_store.addDocuments` → `fs.copyFileSync`); the server deletes its copy after the run | **No.** `v1_analysis._run_ephemeral` calls `run_pipeline` **without `case_id`**, so `compare_to_case` and `find_gotchas` never run |

`docs/RENDER_HYBRID.md` and `render.yaml` already describe the target ("ephemeral analysis + control plane, no document vault") on Render, with the marketing site as a Render static service.

### A2. Job lifecycle
- `storage.create_job` / `job_dir` → `data/jobs/{job_id}/` holding `progress.json`, the source file, `report.json`, `report.html`, `report.pdf` and `artifacts/` (ELA heatmaps).
- Engine order in `pipeline.run_pipeline`: metadata → pdf_integrity → structural → image_forensics → ocr_text → contextual (Tier 1/2/3) → template leftovers + statement dates (exhibits only) → **[if case_id]** save_scan_snapshot → `compare_to_case` → `find_gotchas` → triage → case impact judge → score → report.
- Ephemeral retention: when a job finishes, `maybe_delete_after_terminal` deletes the **source + artifacts** right away. **`report.json` and `report.html` stay up to `EPHEMERAL_TTL_HOURS=6`**, and the 15-minute sweeper removes them. Reports include `comparison.text_excerpt` = **first 24,000 characters of the document text**.

### A3. Case memory already exists (SQLite, `cases_db.py`)
- `documents` table already has **`sha256`**, `stored_path` and `doc_kind`.
- `scan_snapshots` table already stores `entities_json`, `amounts_json`, `facts_json`, `text_fingerprint`, `excerpt_hashes_json` and **`text_excerpt` (24k chars of raw text)**.
- This is local SQLite with no firm/tenant column.

### A4. Cross-document detection: the "known gap" is mostly closed
- `fact_extract.extract_facts` + `fact_graph.py` extract entity-scoped attributes, including **`employment_start`** and `employment_tax_year`; `case_consistency.compare_to_case` and `aggregate_fact_conflicts` compare them across documents.
- **Test run during this review** (throwaway script, no LLM keys): Doc A "employed by TechCore Solutions … since March 2019", Doc B "Employed since March 2022 at TechCore Solutions" → **HIGH: "RED FLAG: Employment start does not match 'VOE'", confidence 76**, for both `exhibit` and `court_filing` kinds. The aggregate check also returned HIGH.
- **Remaining weaknesses:**
  1. **The extraction is regex and brittle.** Of 4 rephrasings tested, 3 extracted nothing: "started working at … in March of 2019", "Start date with current employer: 3/2022", "began employment … on or about March 1, 2019". Only "Date of Hire: 03/01/2019" worked.
  2. `evals/fixtures/lie_detector/planted_bs_cases.json` has the Summers 2019/2022 case only as **`internal_binder`** (one document). There's **no cross-document version** of that fixture.
  3. **None of this runs in the ephemeral/desktop path** (see A1).

### A5. What leaves the server (third parties)

| Path | Sent | When |
|---|---|---|
| Tier 2 claim check | Generated questions only, no document content | `GEMINI_API_KEY` set |
| Tier 3 multimodal | **Original file (≤15 MB inline) + extracted text** → Gemini | `CONTEXTUAL_MULTIMODAL=true` **and not a court filing**. **`render.yaml` sets this to true; the `config.py` default is false** |
| Logo/chrome pass | Page images → Gemini | Multimodal on + `LOGO_CHROME_PASS` (config default **true**, `.env.example` says **false**) |
| Gotcha verifier | Claim and evidence text passages → **Claude** if `ANTHROPIC_API_KEY` is set, else Gemini (or OpenAI) | Case path only |
| Case chat (`/v1/case-chat`) | Client-supplied text excerpts → LLM | On use |

The code can't tell whether the Gemini key is **free or paid tier**. John must confirm it's a paid/Vertex key before client data goes through.

### A6. Finding schema and click-through
- `models.Finding`: `id, category, severity, title, explanation, why_suspicious, confidence, page (single int, optional), evidence (dict), recommended_action`.
- Cross-doc findings put `prior_document_id`, `prior_display_name`, `field`, `new_value` and `prior_value` in `evidence`, but **no page numbers and no quotes for either side**. Gotchas carry `claim_quote` and `evidence_quote` but **no page**.
- `extract_facts` runs on `full_text`, which is joined from `extracted_pages` in `ocr_text.py`, so **page boundaries are lost before facts are pulled**.
- Web UI: `DocumentPreview` loads the **server-stored file** in an `<iframe src=".../file#view=FitH">`. No page jump, no highlight.
- Desktop: `cs:open-doc` → `shell.openPath(stored_path)` opens the file in the default PDF app, with no page jump.

### A7. Auth, security and ops (⚠ needs attention)
- **`main.py` mounts the full `/api` router (case vault, `/api/cases`, document file download) on the same app as `/api/v1`, with `CORS allow_origins=["*"]`.** `render.yaml` deploys this app with `ENTITLEMENT_MODE=open`, and `ANALYSIS_API_KEYS` is optional. **If the Render analysis service is live, check right away** whether `/api/cases` and `/api/upload` are reachable without auth.
- `control_plane/main.py`: signup, login, Stripe and entitlement already exist. Users and sessions are stored in a JSON data file, **passwords are salted SHA-256 (should be argon2 or bcrypt)**, and **sessions never expire**.
- Sentry scrubbing exists (`sentry_scrub.py`: paths and emails).
- `config.py` defaults to `gemini_model="gemini-2.5-flash"`, which the old HANDOFF says is no longer available to new keys. `docker-compose.yml` defaults to `gemini-3.6-flash`.

---

## Part B — What changes from v1

| v1 said | Reality → v2 plan |
|---|---|
| "Add Postgres first" | SQLite case memory already exists. Postgres is still needed for multi-firm hosting, but **add it alongside the case-memory work (Phase 2)**, not as a separate first step |
| "Build Clerk/Auth0" | **Extend the existing control plane** (fix hashing and session expiry) instead of adding a new vendor |
| "Build a cross-doc engine (Phase 3)" | The engine exists. **The work is (a) making it run in the no-vault path and (b) making extraction robust** |
| "Fact extraction step" | Exists (regex). **Add LLM-assisted extraction with page + quote** |
| Electron "lower priority" | Electron already does local files + ephemeral server. **The browser client is a port of that flow**, plus a server-side fact store so history survives cleared browsers and works across machines |

---

## Part C — Phases (in order)

### Phase 0 — Lock down the live analysis service (do first, small)
1. Add `APP_MODE=analysis|vault` (default `analysis` in production). In `analysis` mode, `main.py` mounts **only** `/api/health`, `/api/entitlement` and `/api/v1/*`. The vault router only mounts in `vault` (local or dev) mode.
2. Require auth on every `/api/v1/*` call: an `ANALYSIS_API_KEYS` token **or** a valid control-plane session (verify the token against `/v1/entitlement`, with a short cache). Refuse to start in production if neither is configured.
3. Restrict CORS to the app domain(s) via env.
4. Control plane: move to argon2/bcrypt (rehash on next login), add session expiry (e.g. 14 days) and logout invalidation.
5. Change the `config.py` model default to `gemini-3.6-flash`. Make `.env.example` match the real defaults (`LOGO_CHROME_PASS`, `CONTEXTUAL_MULTIMODAL`, model).
6. Add `GEMINI_TIER=paid|vertex|free`; refuse `free` when `APP_ENV=production`.

**Done when:** an unauthenticated request to `/api/cases`, `/api/upload` or `/api/v1/jobs` on the deployed service returns 401/404, and tests cover it.

### Phase 1 — Page-anchored facts and findings (needed for click-through on any platform)
1. In `ocr_text.py`, return `page_offsets` (char start per page) alongside `full_text`. Add a helper `char_to_page(offset)`.
2. `extract_facts` / `extract_grounded_facts`: record `page`, `char_start`, `char_end` and `quote` for every fact.
3. `compare_to_case` / `_case_finding`: add `evidence.refs = [{document_id, sha256, page, char_start, char_end, quote}, …]` for **both** sides (new doc and prior doc). Keep the existing keys for backward compatibility.
4. Gotchas: add a page and sha256 to `claim_quote` / `evidence_quote` the same way.
5. Store `documents.sha256` for every document (the column exists; make sure it's always filled).

**Done when:** every cross-doc finding and gotcha on 11467-2007 has refs with page numbers for each document it cites.

### Phase 2 — Server-side case memory for the no-vault path
1. Postgres (SQLAlchemy + Alembic), tables scoped by `firm_id`: `firms`, `users` (linked to control-plane accounts), `cases`, `documents (sha256, filename, page_count, doc_kind, filed_date, court_doc_url, superseded_by)`, `snapshots (facts, entities, amounts, fingerprints, excerpt_hashes, findings_summary)`, `findings` + `finding_refs`, `analysis_runs (tokens, cost)`. Migrate the `cases_db` SQLite schema into it. Keep SQLite for `APP_MODE=vault` / local only.
2. **Raw text decision (John to approve):** `scan_snapshots.text_excerpt` (24k chars) feeds `gotcha.retrieve_evidence` and case chat. Options:
   - **(a) Recommended:** store only **short encrypted passages** (a ±400-char window around each extracted fact and claim) plus facts. Drop the 24k excerpt. Measure the gotcha eval before and after.
   - (b) The client sends prior excerpts with each job, the same pattern as `/v1/case-chat`. This is stateless, but history is lost if browser storage is cleared.
   - (c) Keep the 24k excerpt, encrypted. Simplest, but weakens the "we don't keep your documents" claim.
3. `/api/v1/jobs` accepts `case_id` (auth-scoped to the firm) → `run_pipeline(case_id=…, document_id=…)` so `compare_to_case` + `find_gotchas` run against the stored snapshots. Originals are still deleted after the run.
4. Stop persisting `comparison.text_excerpt` in stored reports (send it to the client once if the UI needs it).
5. Report retention: delete `report.json`/`report.html` once the client confirms it has fetched them (`DELETE /v1/jobs/{id}` already exists). Keep the 6h sweeper as a backstop; consider lowering it to 1h.
6. Field-level encryption for passages, quotes and fact values (`FIELD_ENCRYPTION_KEY`).

**Done when:** two documents uploaded through `/v1/jobs` to the same case produce the 2019 vs 2022 HIGH finding, with no originals on disk afterward (verified by test), and firm A can't read firm B's case (test).

### Phase 3 — Browser client with a local folder (port the Electron flow into `frontend/`)
1. "Connect case folder" → `showDirectoryPicker()`; store the handle in IndexedDB keyed by case. Use `queryPermission` / `requestPermission` with a "Reconnect folder" button.
2. Index: recursive walk, SHA-256 in a Web Worker (streaming). Build a `sha256 → handle` map. Skip files the server already has a snapshot for.
3. Analyze: upload to `/api/v1/jobs` with `case_id`; poll; fetch the report; ack the delete.
4. **Replace the `<iframe>` `DocumentPreview`** with a **PDF.js viewer** that reads the **local** file, opens it at `ref.page`, and highlights `char_start–char_end` (falling back to a text search on `quote`). Every cross-doc finding shows one button per ref ("Open VOE p. 3", "Open Loan App p. 1").
5. Moved/renamed file: re-index; if it's still missing, show "Locate file" with a picker that checks the hash before accepting.
6. `court_doc_url` → "Open court record" button when present.
7. Safari/Firefox → banner saying Chrome or Edge is required.
8. Cleared browser data → only the folder link is lost; reports, facts and history come from the server.

**Done when (Playwright, Chromium):** connect folder → analyze 2 docs → click a cross-doc finding → the correct local PDF opens at the correct page with the passage highlighted. It still works after a rename, and after clearing site data and reconnecting.

### Phase 4 — Extraction robustness (makes cross-doc checks reliable on real files)
1. Add Gemini-assisted fact extraction (strict JSON schema: subject, predicate, value, page, quote) **in addition to** the regex extractors. The regex stays as the deterministic baseline; LLM facts go through the same `compare_to_case` scoring. Cap confidence and label as `ai_generated` per the existing safety rails.
2. Add these fixtures to `planted_bs_cases.json`:
   - `summers_employment_start_cross_doc` (two documents, **must_ship HIGH**)
   - the 3 rephrasings that currently fail ("started working at … in March of 2019", "Start date with current employer: 3/2022", "began employment … on or about March 1, 2019")
   - a clean twin (same date, different wording → must not flag)
3. `run_gotcha_eval.py` must keep `all_pass` across every existing section.

### Phase 5 — Year-over-year docket comparison
1. When a new document arrives, suggest a match to a prior version (same doc kind + overlapping subjects); the user confirms. Set `superseded_by`.
2. "Changes since prior filing" report section: each changed fact shows old value (filed date, page) → new value (page), plus severity. **Works from stored facts only**; test with the old file deleted.

### Phase 6 — Retire the server vault in production
- Vault endpoints only exist in `APP_MODE=vault` (local/dev or an opt-in "cloud copy" for specific firms, stored encrypted in S3/R2 with a per-firm prefix; off by default).
- Update `docs/RENDER_HYBRID.md` and the privacy wording to match what is actually stored, including the remaining report window and the third parties in A5.

### Phase 7 — Electron
- Keep it as an optional wrapper around the same `frontend/` build. Use the same server fact store, or a local-only backend for firms that refuse any hosting. No feature forks.

---

## Part D — Checkpoints for John

1. **Phase 0** (security), then confirm the live service is locked down.
2. **Decide the raw-text option** in Phase 2.2 (recommended: a).
3. **Phase 1 + 2**, then demo: two-document 2019/2022 finding through the no-vault path, with page refs.
4. **Phase 3**, then demo: click-through to the exact local page.
5. **Phase 4** eval results, then **Phase 5**.

At each checkpoint, update `HANDOFF.md` (done / blocked / next) and paste the `run_gotcha_eval.py` summary.

## Part E — Beta acceptance checklist
- [ ] No unauthenticated access to any analysis or vault endpoint on the deployed service
- [ ] Production refuses a free-tier Gemini key
- [ ] No original file on the server after a run; reports deleted after client ack (≤ TTL backstop)
- [ ] Cross-doc findings run in the no-vault path, with page refs on both sides
- [ ] 2019/2022 cross-doc fixture and the 3 rephrasings ship HIGH; clean twins don't flag
- [ ] All 3 forged exhibits in 11467-2007 still caught; Sept 23 planted frauds still caught
- [ ] Click-through to the exact local page works after rename and after clearing browser data
- [ ] Firm data isolation tested
- [ ] Privacy page lists exactly what is stored and which third parties receive what (A5)
