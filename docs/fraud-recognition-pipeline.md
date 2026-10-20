# Fraud recognition and scan pipeline

CourtExhibitScanner (CaseSifter) is **systematic pattern detection and contradiction spotting with exhibit-fraud analysis**. It is not argument synthesis, impeachment pairing, or brief writing.

Incoming files are treated as **scans of paper** (flatbed, print-then-scan, NYSCEF packets), not native-born originals. Prefer signals that survive OCR. Ordinary scan texture, mixed page size, OCR noise, e-filing packaging, file-creation metadata, and generic font mismatch are not case interrupts.

This document describes how a file moves through the pipeline, how exhibit fraud differs from court-filing analysis, what counts as a material interrupt, how risk is scored, and how Case Impact surfaces results.

Canonical product rules live in [`.cursor/rules/materiality-thresholds.mdc`](../.cursor/rules/materiality-thresholds.mdc). Implementation centers on `backend/app/pipeline.py`, `scan_profile.py`, `finding_policy.py`, engines under `backend/app/engines/`, `gotcha.py`, `impact_judge.py`, and `backend/app/report/scoring.py`.

---

## 1. End-to-end pipeline

Scans run in two stages so the UI can show local module lights before Gemini finishes.

```
Upload → kind / scan profile → local OCR + forensics → Gemini visual / claims
       → fact extract → case consistency + gotchas → triage + Case Impact judge
       → score / report → document list + Case Impact board
```

### 1.1 Upload and identity

1. File lands in a job directory; SHA-256 and type sniff run.
2. Optional `doc_kind` and display name come from the UI or matter vault.
3. Progress updates as stages advance (`ingesting` → … → report).

### 1.2 Kind and scan profile (two lanes)

Profiles are resolved in `scan_profile.py`:

| Profile | Purpose |
| --- | --- |
| **`forensic`** | Exhibits / statements / images — forgery structure, ELA, statement-body checks, Gemini multimodal |
| **`court_filing`** | Pleadings and procedural filings — facts + gotchas; packaging noise skipped |

**Routing rules (simplified):**

- Explicit exhibit kinds (`exhibit`, `bill`, `statement`, `invoice`, …) → forensic.
- Explicit court-filing kinds (`motion`, `affidavit`, `order`, …) on **native-text** media → court filing.
- **Images and scan-heavy / image-only PDFs always stay forensic** (Gemini + ELA), even if kind looks pleading-like.
- Exhibit-shaped filenames (`EXHIBIT_S__41`, `EXHIBIT(S)`, …) force forensic even if kind was mis-set.
- Blank kind defaults **forensic** so real NYSCEF uploads are not silently skipped.
- Caption / NYSCEF stamp chrome alone never chooses court filing.
- Known boilerplate forms (RJI, e-filing consent, cover sheet, affidavit of service, …) can take court_filing / skip issuer Gemini — but a **statement-like body wins** (stamped utility bill stays forensic).

### 1.3 Local stage (`run_local_stage`)

Always:

- Metadata (timestamps, producers)
- OCR / text extract (and font notes where relevant)
- Local regex fact bootstrap

**Forensic only:**

- PDF integrity (AcroForm, JS, hybrid, …)
- Structural page size / rotation / resolution mix
- Image forensics (ELA heatmaps, solid overlays, copy-move where applicable)
- Deterministic statement engines: template leftovers, statement dates, issuer anachronisms, line self-contradictions, statement math

**Court filing:** those forgery modules are **skipped** with explicit reasons (mixed page size and AcroForm are normal in e-filed pleadings).

Local stage writes `local_stage.json` and publishes module statuses so document-row lights can turn green before Gemini.

### 1.4 Gemini / contextual stage (`run_gemini_stage`)

`contextual_logic` runs three tiers (each more opt-in than the last):

1. **Local** — entity extraction + deterministic timeline checks (nothing leaves the server).
2. **Claim verification** — short public-record style questions only (needs `GEMINI_API_KEY`); no full document upload.
3. **Document analysis / visual review** — exhibits only; needs `CONTEXTUAL_MULTIMODAL=true`. Sends downscaled page imagery or Files API URI. Court filings are `skipped_court_filing` and the native file is not sent.

Additional forensic passes when configured: logo/chrome era check (`logo_chrome.py`) on consumer statements.

Then:

- AI fact extract (merged with local facts)
- Case consistency vs prior snapshots (vault) or ephemeral peer snapshots (desktop)
- Gotcha proposal + verification (`gotcha.py`)
- `triage_findings` (materiality / noise)
- Case Impact judge (`impact_judge.py`)
- Risk score, executive summary, HTML/PDF report

### 1.5 Fail-closed visual review

If a forensic scan **requires** visual review and Gemini never returns a successful file response, `ai_review` is `incomplete`. A would-be green result becomes **incomplete** — the file is **not cleared** as low risk. Yellow/red findings still stand.

---

## 2. Two lanes: exhibits vs court filings

### Forensic exhibit lane

Goal: detect that a **party-offered exhibit** is false, internally impossible, or fraudulently edited (bought bank/utility/invoice templates filled in, printed, and scanned are in scope).

Runs: PDF structure, image forensics, statement-body determinism, Gemini visual/chrome, forgery severity promotion, forgery risk floor.

### Court-filing lane

Goal: extract facts and find **cross-document gotchas** (claim vs hard evidence elsewhere in the matter).

Does **not** run exhibit forgery structure, whiteout/ELA/clone detection, or statement due-date math as packaging interrupts. Gemini native-file visual review is skipped.

Boilerplate procedural forms may also skip issuer/authorization Gemini and AI fact extract while still contributing text for case comparison when useful.

---

## 3. What fraud recognition is — and is not

### Is fraud recognition

- Impossible or irreconcilable labeled dates on a **consumer statement body**
- Statement arithmetic that does not reconcile (bank / utility / telecom style)
- Leftover bought-template chrome (“your company name”, specimen, lorem ipsum, …)
- Historical impossibility on the exhibit body (e.g. issuer + place + date that cannot coexist)
- Strong localized overlay / ELA **with statement / template content context**
- Logo / letterhead wrong for issuer or era (quoted evidence required)
- A party’s own exhibit undercutting their claim, or forensic flags on the exhibit they offered (`tainted_support`)

### Is not fraud recognition

| Noise / ordinary dispute | Why suppressed |
| --- | --- |
| He-said-she-said (denial vs opposing affidavit of service, competing motion recitals) | Ordinary traverse, not a gotcha |
| Evidence that **restates** or **supports** the claim | Directionality is FOR, not AGAINST |
| Shared topic, person, or caption only | Not the same nameable fact |
| Whole-page scan texture / soft ELA alone | Print-scan JPEG noise |
| NYSCEF header, filing-stamp vs body date, index year | Packaging |
| Caption / party list mismatch | Not an interrupt |
| Mixed page size, rotation, AcroForm on court filings | Normal e-filing |
| Metadata / editor software alone | Not proof of paper forgery |
| Font / spelling nuance without a field or row hook | Not material |
| Note rate vs default rate, principal, installment drift after missed payments | Expected loan-status movement |
| Loan math on recorded mortgages / deeds / assignments | Not bank statements |
| Arrears demand letters citing older installments | Expected |
| Exhibit letter/number label mismatches (“Exhibit A not in this case”) | Labels change between filings |
| Jurat-only “edited exhibit” cards | Not content fraud |
| Two motions, or claim vs proposed order | Advocacy, not a gotcha |
| Model pass without the four gotcha gates completed | Does not ship |

---

## 4. Material interrupts and gotchas

### Case-impact allowlist

Ship an interrupt only when the finding hits at least one bucket:

- **Element** — e.g. personal jurisdiction / service, standing, default
- **Procedure / foundation** — authority to act, entered court act, same-CRFN discharge vs continued reliance
- **Credibility** — party’s own exhibit is false or internally impossible
- **Damages** — fee/amount that is not ordinary loan-status movement

Case-level structural interrupts stay sparse (e.g. conflicting first-default / notice / foreclosure dates that are not same-month noise; non-amortization fee conflicts).

### Gotcha definition (four gates)

A gotcha is a claim checked against real evidence (sworn, documentary, or entered court act) that passes **all four**:

1. **Same nameable fact** (who + attribute + value — not shared boilerplate tokens)
2. **Evidence runs AGAINST** the claim (not restating or supporting it)
3. **Material** to element / procedure / credibility / damages
4. **Both quotes** are grounded in the pulled text

Ambiguous pairs are suppressed. Types are only:

`factual_contradiction` · `chronology_inconsistency` · `material_omission` · `procedural_foundational` · `admission_against_interest`

Verification is gated (`gotcha.py`): candidates are proposed, then verified (Claude when configured, else Gemini). A model “pass” without all four completed steps does not ship. Board materiality also requires `verified` and excludes weak evidence tiers.

### Triage (`finding_policy.triage_findings`)

- Drops header / arrears / nuance noise
- Promotes strong forgery indicators to **critical**
- Demotes non-material leftovers to **info** so they cannot yellow a file
- Packaging finding IDs (page size, metadata gaps, AcroForm, …) never drive yellow/red

### Case Impact judge (`impact_judge.py`)

Re-weighs red/yellow gotchas and fraud findings. Deterministic rules keep standalone exhibit fraud material; Claude (when available) demotes token collisions, he-said-she-said, and boilerplate overlap to `noise`. When uncertain → noise.

---

## 5. Primary exhibit-fraud signals

Primary signals are **content that survives a scan**. Secondary image signals need statement / template context.

| Signal | Engine / type | Notes |
| --- | --- | --- |
| Impossible statement dates | `statement_dates` → `date_inconsistency` | Due date / period that cannot belong to statement date; court stamps ignored |
| Consumer statement math | `statement_body` → `math_error` | Requires digit quotes; loan amortization / default-notice math suppressed |
| Line self-contradiction | `statement_body` → `line_self_contradiction` | Same line asserts incompatible place spellings, etc. |
| Historical impossibility | `statement_body` / Gemini → `historical_impossibility` | e.g. National Grid + Brooklyn/(718) before KeySpan NYC close |
| Template leftovers | `template_fabrication` | Specimen / placeholder chrome on the OCR text |
| Logo / chrome mismatch | `logo_chrome` → `logo_chrome_mismatch` | Needs quoted evidence or logo note (≥12 chars); issuer name alone is not enough |
| Concealment overlay | Image / Gemini → `text_overlay` | Whiteout / solid paste; legitimate 22 NYCRR redactions demoted to noise |
| Localized ELA / graphic edit | `image_forensics` + `review_print_scan_ela` | On scan-heavy pages: interrupt only with **print_scan_review** + content hook (e.g. template leftovers). Whole-page texture alone never interrupts pleadings/deeds as a standalone interrupt |
| Field-scoped font mismatch | Gemini / OCR nuance promoted | Amount, date, or name field only — not generic typeface notes |

**Forgery floor:** any material forged-exhibit signal floors document risk at **100**. Clean checks cannot pull that floor down (`forgery_risk_band`).

---

## 6. Risk scoring philosophy

From `report/scoring.py` and materiality rules:

- Scores come from **verified material findings only** — never document type or title.
- Two affidavits (or two exhibits) score differently if one has verified gotchas / forgery and the other does not.
- Weights: severity × confidence; gotcha types use `TYPE_SCORE_WEIGHT` (procedural / admission weigh more); `tainted_support` and self-undercut multiply further.
- Bands: green &lt; 35, yellow &lt; 70, red ≥ 70; forged exhibit → critical banner and floor 100.
- Incomplete required visual review → not green clearance.
- Local-only review (no multimodal) is labeled as such — not a completed visual clear.

---

## 7. How results surface in the UI

### Document list / triage

Per document after scan:

- **Risk band** — green / yellow / red / incomplete
- **Profile tags** — short labels from analysis types (Statement dates, Statement math, Template chrome, …) via `scan_signals.profile_tags`
- **Info summary** — one-line what-the-file-is + top flags
- **Module lights** — Meta, PDF, Image, OCR, Dates, Logic, etc., including early local_done statuses

Filters can narrow by risk and tags. Incomplete visual review must not read as “cleared.”

### Per-document report

Executive summary, top findings, recommended actions, optional ELA artifacts, score drivers. Forged exhibits use the critical-risk banner copy.

### Case Impact board

Matter-level board (`CaseGotchaBoard` in the frontend; API `list_case_gotchas` / `case_impact`):

- **Material** interrupts (gotchas + documentary fact conflicts)
- **Altered** count for fraud / tainted-support findings
- Collapsed **noise** (demoted pairs) available on demand
- Rows link back to source documents / reports
- Prefer aggregate fact-conflict cards over duplicate pairwise gotcha rows for the same field

Desktop ephemeral scans without a vault `case_id` rebuild cross-doc conflicts from cached report facts (see operator notes in `docs/LIE_DETECTOR_TRUST.md`); an empty gotcha list there is not proof the packet is clean until that aggregate has run.

---

## 8. Quick reference: module map

| Stage | Module | Court filing | Forensic exhibit |
| --- | --- | --- | --- |
| Local | metadata | yes | yes |
| Local | pdf_integrity / structural / image_forensics | skip | yes |
| Local | ocr_text | yes (facts) | yes |
| Local | statement_dates / body / template | skip | yes |
| Gemini | contextual entities / claims | yes (no native file) | yes |
| Gemini | multimodal document analysis | skip | when enabled + required |
| Gemini | logo_chrome | no | consumer statements |
| Case | fact extract, consistency, gotchas | yes | yes |
| Gate | triage + impact judge + score | yes | yes |

---

## Related docs

- [`DATA_FLOW.md`](DATA_FLOW.md) — retention, Tier 3 Gemini upload, keys
- [`LIE_DETECTOR_TRUST.md`](LIE_DETECTOR_TRUST.md) — operator trust, green meaning, eval priorities
- [`.cursor/rules/materiality-thresholds.mdc`](../.cursor/rules/materiality-thresholds.mdc) — always-on product rules for agents
