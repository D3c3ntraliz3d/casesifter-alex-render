# Fraud detection accuracy research (scan-of-paper exhibits)

Companion to [`fraud-recognition-pipeline.md`](fraud-recognition-pipeline.md) and [`.cursor/rules/materiality-thresholds.mdc`](../.cursor/rules/materiality-thresholds.mdc). This note surveys what actually works for **print-then-scan / flatbed / NYSCEF** exhibits, then maps accuracy architecture onto CaseSifter’s existing stack. It does not change product rules: content-first exhibit fraud, sparse interrupts, no advocacy synthesis.

---

## 1. Executive summary — accuracy principles

For CaseSifter’s threat model (bought bank/utility/invoice templates filled in, printed, and scanned; or field-level paste on a consumer statement), high accuracy means **high precision on case-impact interrupts**, not maximum recall of every pixel anomaly.

**Principles**

1. **Content that survives OCR wins.** Impossible statement dates, irreconcilable statement math, leftover template chrome, line self-contradictions, and issuer/era impossibilities are the primary detectors. They degrade gracefully under JPEG, DPI change, and e-filing stamps.
2. **Pixel forensics are secondary and gated.** After a print-scan cycle, ELA, copy-move, and generic “manipulation nets” lose discriminative power and produce whole-page texture false positives. Use them only with a statement/template **content hook**, never as standalone interrupts on pleadings or clean decoys.
3. **Lane separation is an accuracy control.** Forensic profile for exhibits; court-filing profile skips packaging forgery modules. Misrouting a stamped bill to court filing is a false negative; misrouting a motion to forensic is a false-positive factory.
4. **Confirmation gates beat single-signal scores.** Materiality (`finding_policy`), forgery floor, fail-closed visual review, and Case Impact judge are the fusion layer — not a learned ensemble score on document type.
5. **Fail closed on incomplete visual review.** Absence of a completed multimodal pass is not green clearance (`ai_review=incomplete`).
6. **Silence is a feature.** Most real packets should stay quiet. Eval harnesses must measure **must_suppress** as hard as **must_fire**.

These align with SWGDE-style practice: combine **image structure** and **scene/content** examination, qualify limitations, and never equate “no ELA hit” with authenticity ([SWGDE 18-I-001](https://www.swgde.org/documents/published-complete-listing/18-i-001-best-practices-for-image-authentication/); NIST OSAC listing of the same).

---

## 2. What works / fails after print-scan

### 2.1 Threat model vs channel

| Attack pattern | Channel after print-scan | Primary reliable signal |
| --- | --- | --- |
| Store-bought template + typed fields | Content + residual chrome in OCR | Template leftovers, logo/era mismatch, dates/math |
| Field paste / whiteout before print | Often flattened; solid white regions may survive | Contiguous solid overlay + statement context; content impossibility |
| Digital splice then re-JPEG / print-scan | Compression history homogenized | Weak ELA; do not interrupt alone |
| Copy-move of glyphs/amounts | Repeated characters confuse detectors; print-scan worsens | Prefer math/date inconsistency; copy-move only on non–scan-heavy |
| He-said-she-said / opposing affidavits | N/A (not exhibit fraud) | Suppress (gotcha gates / impact judge) |
| Court-filing packaging (NYSCEF, AcroForm, mixed size) | Normal e-file noise | Suppress via profile + `PACKAGING_IDS` |

Print-scan is a known **counter-forensic** operation for pixel detectors: models trained on digital edits often collapse after print-scan; device variability (printer/scanner pairs) hurts generalization (see printing/scanning counter-forensics literature, e.g. EURASIP JIVP 2022). Conversely, text-based consistency (dates, arithmetic) was already a winning track in document fraud contests (e.g. ICPR Find it! style text checks) precisely because it does not depend on JPEG history.

### 2.2 Technique-by-technique (scan-of-paper)

| Technique | After print / flatbed / NYSCEF | CaseSifter stance |
| --- | --- | --- |
| **OCR-grounded dates** | Strong if stamps are excluded | Primary — `statement_dates` |
| **Statement arithmetic** | Strong with digit quotes; OCR glue hurts | Primary — `statement_body` math; require `\d` in quotes |
| **Template residue** | Strong (“your company name”, specimen, lorem) | Primary — `template_fabrication` |
| **Historical / issuer era** | Strong when place + brand + date conflict | Primary — issuer anachronisms + Gemini with quotes |
| **Logo / letterhead era** | Medium; models hallucinate | Gate on quoted evidence / logo note ≥12 chars |
| **Solid whiteout / overlay** | Medium–strong when contiguous pure regions remain | Secondary with `content_hook` / `print_scan_review` |
| **ELA** | Weak–misleading on scan-heavy; edges always hot | Soft info unless strong + template leftovers |
| **Copy-move** | Weak on scan-heavy (repeated glyphs, JPEG) | Skipped when `scan_heavy`; never packaging interrupt |
| **Lighting / noise inconsistency** | Research-grade (scanner noise, chromatic fringe on inserted glyphs) | Not shipped; optional medium-term experiment |
| **Metadata / editor software** | Unreliable for paper forgeries | Demoted (`md-editor-software` not material) |
| **Font mismatch (page-wide)** | Noise on OCR/scans | Demote nuance; allow only **field-scoped** amount/date/name |
| **Deep forgery localization nets** | Often fail under resize/recompress/print-scan; text–BG bias | Do not gate interrupts on them without content confirm |

**ELA limits (practical):** ELA assumes heterogeneous JPEG histories. Multiple compression cycles, print-scan, and lossless intermediates flatten maps; high-contrast text edges always look “hot.” A finding of “no ELA anomaly” does **not** mean the exhibit is authentic — only that this particular artifact class was not found (SWGDE-style qualification).

**Copy-move limits:** Scanned text has many near-identical glyphs; detectors confuse clones with natural repetition. Useful mainly on photo exhibits or digitally born rasters — already reflected by skipping copy-move on `scan_heavy` in `image_forensics.py`.

---

## 3. Content-first vs pixel-forensics — when each is reliable

```
                    Survives print-scan?
  Content checks ──────────────────────────► YES (primary interrupt path)
  (dates, math, chrome, era)

  Pixel checks ───► Usually NO alone ──► YES only if content hook
  (ELA, clone, soft texture)              (template leftovers / statement body)
```

| Situation | Prefer | Why |
| --- | --- | --- |
| Utility/bank/telecom statement body | Content engines | Fields are nameable; OCR quotes ground the finding |
| Bought template with placeholder chrome | Content + optional ELA promote | Chrome is the smoking gun; ELA confirms typed fields |
| Clean genuine bill with NYSCEF stamp | Suppress packaging; keep dates | Stamp vs body is not fraud |
| Photo of a signature pad / non-text image | Pixel + multimodal | Less OCR structure |
| Pleading / affidavit / RJI | Facts + gotchas only | Forgery modules are FP-prone |
| OCR-starved scan | Fail-closed multimodal | Do not invent green from empty text |

**Multi-signal fusion (recommended pattern):** treat signals as **layers with AND/OR gates**, not a weighted sum that can “average away” forgery:

- **L1 — Deterministic content** (OR within class): date impossibility ∨ math ∨ line contradiction ∨ template leftovers ∨ issuer anachronism → material / forgery floor.
- **L2 — Pixel structure** (AND with L1 or explicit statement hook): strong localized ELA ∨ solid overlay **only if** template leftovers or consumer-statement context.
- **L3 — Multimodal confirm** (Gemini): logo/chrome, visual paste; quotes required; incomplete → not green.
- **L4 — Case impact** (gotcha four gates + impact judge): tainted_support / credibility; he-said-she-said → noise.

This matches SWGDE’s “content + structure” methodology without claiming lab-grade authentication from ELA alone.

---

## 4. Recommended detection stack (aligned to CaseSifter materiality)

### 4.1 Signal layers

| Layer | Role | Interrupt? |
| --- | --- | --- |
| **A. Profile / kind** (`scan_profile.py`) | Forensic vs court_filing; statement body beats stamp | Routing only |
| **B. Deterministic statement** (`statement_dates`, `statement_body`, `template_fabrication`) | Primary exhibit fraud | Yes → critical / floor 100 |
| **C. Image structure** (`image_forensics`) | Heatmaps, solid overlay; soft ELA on scans | Only with `print_scan_review` / `content_hook` |
| **D. Multimodal** (`contextual_logic`, `logo_chrome`, `statement_visual`) | Visual/era confirm | Yes if quoted + material types; else incomplete |
| **E. Policy fusion** (`finding_policy`, `scoring`, `impact_judge`) | Promote forgery, demote packaging/nuance | Final attorney-facing band |

### 4.2 Confirmation gates (keep / tighten)

Already in code — treat as the accuracy architecture, not optional polish:

1. **Quote grounding** — math needs digits in quoted evidence; logo needs ≥12-char quote or logo note.
2. **Statement hooks** — `STATEMENT_FORGERY_HOOK_RE` / temporal hooks; allonge vs note ≠ statement temporal class.
3. **Print-scan ELA gate** — `review_print_scan_ela`: strong ELA + **template leftovers** (not brand name alone).
4. **Packaging denylist** — `PACKAGING_IDS`, header/arrears/filing-vs-body regexes.
5. **Forgery floor** — `forgery_risk_band` → 100; clean checks cannot pull down.
6. **Fail-closed visual** — required review incomplete ≠ green (`apply_review_presentation`).
7. **Gotcha four gates** — same fact / AGAINST / material / dual quotes; model pass without gates does not ship.
8. **Redaction exclusion** — 22 NYCRR-style overlays → noise, not concealment.

### 4.3 Calibration targets

| Metric | Target (operating point) |
| --- | --- |
| Precision on forged-exhibit interrupts (critical band / floor 100) | Prefer ≥0.95 on held-out decoys + real planted forgeries |
| False yellow/red on clean consumer statements | ≈0 on `exhibit-clean-*` / genuine-with-stamp fixtures |
| False green on known forged statements (S-41 class) | 0 when OCR retains date/math/chrome hooks |
| Incomplete visual rate when multimodal required | Track; never map to green |
| Court-filing packaging interrupts | 0 |

Do **not** optimize macro-F1 across all finding types — that rewards packaging noise recall.

---

## 5. Gap analysis vs current pipeline

Canonical flow: [`fraud-recognition-pipeline.md`](fraud-recognition-pipeline.md). Implementation anchors: `pipeline.py`, `scan_profile.py`, `finding_policy.py`, engines under `backend/app/engines/`, `report/scoring.py`, `scan_signals.py`.

### 5.1 Keep (already correct for accuracy)

| Piece | Why it is right |
| --- | --- |
| Two-lane profile + media override | Prevents packaging FPs; keeps scan-heavy exhibits forensic |
| Deterministic statement engines before Gemini | Content-first; stable under OCR noise |
| Soft ELA on `scan_heavy` + promote only with leftovers | Matches print-scan science |
| Solid-overlay path with `content_hook` | Survives when ELA fails |
| `triage_findings` / `is_material_finding` / forgery floor | Fusion + precision control |
| Fail-closed incomplete visual review | Avoids false clearance |
| Eval seeds: `test_forgery_risk_floor`, `test_s41_statement_signals`, `test_visual_review_fail_closed` | Precision/recall fixtures for the real threat |

### 5.2 Strengthen (near-term, high ROI)

| Gap | Risk today | Recommendation | Status |
| --- | --- | --- | --- |
| OCR glue / missing tokens | Misses on live NYSCEF vs clean fixtures | Expand garbled fixtures (pattern in `test_s41_statement_signals`); harden token-optional regexes | Done — glued/mistyped labels in `statement_dates` / `statement_body`; garbled pack in `test_s41_statement_signals` |
| Math coverage breadth | Only some statement layouts | Add balance-forward / payment / period identity checks as deterministic rules with digit quotes | Done — amount-due identity + disagreeing Amount Due lines |
| Issuer/era knowledge sparse | Misses outside National Grid/(718) class | Small curated issuer×market×date tables + quote requirement (avoid open-ended model “era” claims) | Done — `ISSUER_MARKET_RULES` (+ PSEG LI); AI historical needs curated brand + date quote |
| Logo/chrome hallucination | FP on clean decoys | Already gated; add golden **must_suppress** multimodal fixtures when Gemini enabled | Done — `test_logo_chrome_must_suppress` (skips without key) |
| ELA promote condition narrow | Only leftovers, not other L1 hits | Optionally allow promote when **any** L1 content finding on same page (still never ELA alone) | Done — `review_print_scan_ela` + `test_ela_l1_promote` |
| Calibration dashboards | Ad-hoc pytest | Aggregate precision/suppress rates in CI summary from fixture packs | Done as dedicated command (not wired into security.yml): `python -m evals.fraud_calibration_summary` from `backend/` |

**Calibration summary (local / optional CI):** from `backend/`, run `python -m evals.fraud_calibration_summary`. Prefer that over expanding `security.yml` unless the pack stays fast and deterministic-only.

### 5.3 Add (medium-term, research-backed)

| Addition | Rationale | Constraint |
| --- | --- | --- |
| **Page-local fusion object** | Explicit `{page, L1[], L2[], gate}` for audit | Do not change materiality rules |
| **Scanner noise / chromatic fringe pilot** | Gardella et al. (ICCVW 2025 VisionDocs): inserted glyphs lack scan noise/fringe | Offline eval only until FP rate known on NYSCEF grayscale |
| **Field-ROI ELA** (amount/date boxes) | Literature: whole-page ELA is FP-prone; field-scoped is closer to field font gate | Still require L1 or template hook |
| **Glyph-aware copy-move** (optional) | Abramova/Uhl-style OCR-class matching | Only if scan_heavy false or photo exhibits; default off |
| **Planted print-scan corpus** | Train/eval thresholds on *your* channel | Prefer fixtures over buying generic deep models |

### 5.4 Demote / do not pursue for interrupts

| Temptation | Why demote |
| --- | --- |
| Standalone deep forgery localization as case impact | Brittle under print-scan; text–BG bias (ADCD-Net class papers still need distortion-robust eval) |
| Metadata / producer / CreateDate as forgery | Explicitly out of scope for paper exhibits |
| Page-wide font / spelling / “AI vibe” | Nuance types → info |
| Physiological / linguistic “lie” scores | Out of product scope ([`LIE_DETECTOR_TRUST.md`](LIE_DETECTOR_TRUST.md)) |
| Raising ELA severity without content hook | Violates materiality; burns trust on clean scans |

### 5.5 Architecture sketch (target)

```
Upload → profile (forensic | court_filing)
      → L1 content engines (always on forensic)
      → L2 image structure (forensic; soft by default)
      → gate: L2 → material iff L1 hook | template | solid+hook
      → L3 multimodal (required on forensic when configured)
      → fail-closed if L3 required & incomplete
      → triage + forgery floor + impact judge
      → score / Case Impact
```

---

## 6. Roadmap + eval plan

### 6.1 Near-term (1–2 cycles)

1. **Freeze the operating point in docs + CI**
   - Document “primary = L1; L2 secondary” in this file + keep pipeline doc consistent (already is).
   - CI job that runs forgery-focused evals as a named group:
     - `test_forgery_risk_floor.py`
     - `test_s41_statement_signals.py`
     - `test_visual_review_fail_closed.py`
     - related issuer/boilerplate / materiality copy tests

2. **Grow OCR-realistic positive fixtures**
   - Pattern: brand + bill date + (718) + dual spellings **without** clean “Brooklyn” tokens (`test_s41_statement_signals`).
   - Add 5–10 more garbled variants from real redacted NYSCEF OCR dumps (no PII).

3. **Grow must_suppress negatives**
   - Genuine bill + 2023 stamp (already).
   - Clean bank statement + soft ELA (Track C in trust runbook).
   - Arrears language with coherent cycle (already).
   - Court redaction overlay (already).
   - Loan amortization / recorded instrument math.

4. **Optional L2 gate tweak (small, tested)**
   - Allow `review_print_scan_ela` promote when strong ELA **and** (template leftovers **or** same-page L1 finding), still never ELA alone.
   - Lock with clean-decoy tests so Track C stays green.

5. **Multimodal contract tests**
   - Extend fail-closed tests for logo_chrome quote length and `ai_review` incomplete messaging (no “routine file” / “no alteration” clearance language).

### 6.2 Medium-term (quarter)

1. **Page-level fusion audit trail** in report extras (which gate fired) for attorney trust and debugging.
2. **Issuer knowledge pack** versioned JSON: issuer × geography × effective dates, consumed by `statement_body` with mandatory quotes.
3. **Print-scan threshold calibration set**: same forged PDF → print → flatbed → NYSCEF-like JPEG; retune ELA score/blob cutoffs; keep L1 unchanged.
4. **Pilot local noise/chromatic inserted-character detector** offline; promote to interrupt only if FP≈0 on clean statements.
5. **Gotcha ↔ forgery join**: when L1 fires on a party’s own exhibit, ensure `tainted_support` path is exercised in matter-level evals (not only document score).

### 6.3 Measurable eval ideas (fit existing `backend/evals/`)

| Eval idea | Shape | Success |
| --- | --- | --- |
| **Forgery floor pack** | Extend `test_forgery_risk_floor` with table of `(text, must_band)` | Known forgeries ≥85/100 floor; cleans &lt;50 |
| **Garbled OCR pack** | Parametrize `test_s41_statement_signals` | Each real OCR shape fires ≥1 L1 |
| **Suppress pack** | Parametrize stamp / arrears / redaction / clean+ELA | Zero material findings |
| **Visual fail-closed** | Keep `test_visual_review_fail_closed` | incomplete ≠ green; forbids clearance phrases |
| **Profile routing** | Unit tests on `finalize_scan_profile` | Stamped bill → forensic; RJI native → court_filing |
| **Fusion gate** | Findings with ELA-only vs ELA+template | Only second is material |
| **Live seed protocol** | Trust runbook flip-one-fact | Detector alive without motif shopping |
| **Optional Gemini golden** | Marked `@pytest.mark.integration` | Logo mismatch needs quotes; clean decoy quiet |

Avoid loading private `plants.json` in CI; keep reconstructed fixtures in-repo as today.

### 6.4 What “done” looks like for high accuracy

- L1 catches the S-41 / National Grid / Citibank class even under OCR glue.
- Clean consumer statements and stamped genuine bills never yellow from ELA/logo alone.
- Incomplete multimodal never reads as cleared.
- Court filings never interrupt on AcroForm / page size / soft ELA.
- Risk scores remain finding-driven, never kind/title-driven.

---

## 7. Sources / references (conservative)

Established practice and peer literature — concepts over product hype:

1. **SWGDE**, *Best Practices for Image Authentication* (18-I-001-2.0) — examine **content and structure**; qualify limitations; integrity (hash) ≠ authenticity. Listed via NIST OSAC / [swgde.org](https://www.swgde.org/documents/published-complete-listing/18-i-001-best-practices-for-image-authentication/).
2. **SWGDE**, *Best Practices for Maintaining the Integrity of Imagery* (17-I-001) — chain of custody / hashing for integrity, distinct from scene authenticity.
3. **Gardella et al.**, *Scanned documents forensics: detecting inserted characters through noise and chromatic artifacts*, ICCV Workshops (VisionDocs) 2025 — shows generic image-forgery methods underperform on documents; scanner noise / chromatic fringe as document-specific cues.
4. **Printing and scanning as counter-forensics** — e.g. work in *EURASIP Journal on Image and Video Processing* (2022) on print-scan defeating digital manipulation detectors; motivates content-first design.
5. **Device / print-copy forensics surveys** — e.g. *EURASIP J. Adv. Signal Process.* (2014) on detecting documents forged by printing and copying; printer/scanner identification is a different problem than CaseSifter interrupts.
6. **Copy-move on scanned text** — Abramova & Uhl, *Detecting Copy–Move Forgeries in Scanned Text Documents* (EI 2016) — OCR-aware matching needed; naive CMFD false positives on repeated glyphs.
7. **Document forgery contests / text checks** — ICPR “Find it!” style evaluations: text consistency (dates, calculations) as a first-class track alongside image methods.
8. **ELA practical limits** — compression-history method; fails after multiple JPEG / print-scan cycles; edge artifacts expected (widely documented in forensic teaching materials; do not treat commercial ELA blogs as standards).
9. **Internal canon** — `.cursor/rules/materiality-thresholds.mdc`, `docs/fraud-recognition-pipeline.md`, `docs/LIE_DETECTOR_TRUST.md`.

---

## 8. File map (for implementers)

| Concern | Primary files |
| --- | --- |
| Profile routing | `backend/app/scan_profile.py` |
| Local + Gemini orchestration | `backend/app/pipeline.py` |
| Materiality / fusion | `backend/app/finding_policy.py` |
| Dates / math / era | `backend/app/engines/statement_dates.py`, `statement_body.py` (`ISSUER_MARKET_RULES`) |
| Template + ELA promote | `backend/app/engines/template_fabrication.py` (`review_print_scan_ela`) |
| Pixel structure | `backend/app/engines/image_forensics.py` |
| Multimodal / logo | `backend/app/engines/contextual_logic.py`, `logo_chrome.py`, `statement_visual.py` |
| Score / fail-closed presentation | `backend/app/report/scoring.py` |
| UI tags | `backend/app/scan_signals.py` |
| Evals | `backend/evals/test_forgery_risk_floor.py`, `test_s41_statement_signals.py`, `test_ela_l1_promote.py`, `test_logo_chrome_must_suppress.py`, `test_visual_review_fail_closed.py` |
| Calibration summary | `backend/evals/fraud_calibration_summary.py` (`python -m evals.fraud_calibration_summary`) |

---

*This research document is advisory. Shipping interrupts still require the four materiality / gotcha gates and the case-impact allowlist. When ambiguous, suppress.*
