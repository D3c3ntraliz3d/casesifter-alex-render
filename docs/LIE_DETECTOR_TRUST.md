# CaseSifter lie detector — operator trust runbook

CaseSifter is an **attorney lie detector**: it surfaces documentary inconsistencies that can **win the case** (credibility wipeout, jurisdiction/occupancy element, damages). It is not a catalog of taught fraud motifs, and it is not a brief-writing assistant.

Quiet means: under the gates, the packet’s own papers do not assert the same nameable fact two incompatible ways. It does **not** mean every narrative inference was checked.

## What this is not (explicit non-goals)

CaseSifter does **not** ship:

- Ocular / voice / heart-rate “AI polygraphs” (EyeDetect-class tools)
- Linguistic deception scores (pronoun drop, cognitive-complexity, DeBERTa-on-fibbing datasets) as case-impact interrupts
- Free-form LLM passes that ask “list every deceptive sentence”
- Advocacy pairing of competing briefs (he-said-she-said)

Those products optimize for suspicion or anxiety, not attorney-winning documentary contradiction. Do not add them.

## AI engines (how models are used)

| Lane | Engine | Role |
|---|---|---|
| Multimodal exhibit forensics | **Gemini** (`gemini-3.6-flash`) | Native PDF/image + text for alteration / historical chrome |
| Gotcha verify | **Claude** when `ANTHROPIC_API_KEY` set (`GOTCHA_VERIFY_PROVIDER=auto`), else Gemini | Judges **already-proposed** claim/evidence pairs only |
| Extraction / clustering | Deterministic fact graph + regex bootstrap | Names attributes; invents no soft “lies” |

Claude is a **gated verifier**, not a lie oracle. Candidates are proposed first; `hard_veto_step` + four gates (same fact / against / material / quotes) + case-impact judge contain noise. Setting the Anthropic key does **not** open a second free-form suspicion firehose.

Ops: on Render analysis, set `ANTHROPIC_API_KEY` (sync secret) and leave `GOTCHA_VERIFY_PROVIDER=auto`. Leave Gemini keys for multimodal.

## Primary roadmap (not LLM shopping)

Best forward path remains:

1. Expand **grounded same-fact extraction** for attorney-winning attributes (identity, occupancy, income, later presence-on-date, …)
2. Keep **hard gates** + case-impact allowlist
3. Optionally improve verify judgment with Claude
4. Grow planted-BS evals — not foundation-model shopping

Tester misses that were never extracted as facts will not be fixed by swapping verifiers.

## What the attorney should hear

Ship only when all four hold:

1. **Same nameable fact** — who + attribute + value, grounded in OCR quotes  
2. **Evidence runs AGAINST** — values cannot both be true  
3. **Material** — hits **element / procedure / credibility / damages**  
4. **Both quotes in pulled text**

Examples of attorney-winning interrupts (same meta-rule, different attributes): identity (DOB), occupancy/residence, income vs tax AGI, employment tenure vs W-2, **presence-on-date / boarding-pass alibi**, **AOS vs process-server daily log**, **balance-on-date**, **servicer vs bank payment dates**, **employer-of-record**, fee/default-date conflicts, exhibit fraud forensics.

## Live blind-eval priority (2026-09-24 / 200110Z triage)

Primary buyers = bank / big-corp counsel. Prefer opposing BS early; also surface own-packet hazards.

**P0 regression shapes (ship):** personal vs substitute; AOS vs server log; multi-day/office presence; contract vs sought rate; payment-event cluster; residence vs utility; execution venue; **occupancy_start** same-person conflicts; occupancy before mortgage; **claimed_arrears drop** with zero payments on servicing history.

**Correct silence (must_suppress fixtures):** calibration plants (Marcus age vs Janet wire; medical office corroboration; recording lag; occupancy covenant; middle-name expansion; soft time hedge; sole-occupant vs AOS; denial of transfer notice; inspector unknown vs depo; marital decade gap; rental he-said; mortgage covenant vs amended; occupancy vs first utility absence; omitted mod request). Soft advocacy/inference plants stay quiet.

**Desktop board:** ephemeral `/api/v1/jobs` scans omit `case_id`, so cross-doc conflicts are rebuilt from cached report facts via `POST /api/v1/fact-conflicts` (see `fact_conflicts_from_packet`). Do not treat an empty desktop gotcha list as proof the packet is clean until that aggregate has run.

**Mock template:** ignore UA220 / Maple / DOB±1 / income-AGI harness fallback.

**Track C:** keep tamper interrupts; clean consumer statements must not upgrade soft ELA / logo-chrome / math opinions to material without leftover template chrome or a substantive quoted content hook (`exhibit-clean-bank_*.pdf` stays green).

Live pack paths (when present — never load `plants.json`):

```
casesifter-testing/test-cases/runs/2026-09-24/<runId>/planted-<seed>/*.txt
```

Reconstructed fixtures: `p0_*` and `calib_*` in `planted_bs_cases.json`.

## What green means

Green (no case-impact interrupt) means all of:

1. **Same nameable fact** — person/entity + attribute + value, grounded in OCR quotes  
2. **No AGAINST pair** — documentary values are compatible (or only ordinary loan-status movement)  
3. **Materiality gates** — nothing hit element / procedure / credibility / damages  
4. **Quotes in pulled text** — both sides attest against the excerpt  

Silence is expected on most real packets. Trust that silence when seeded flips still interrupt.

## What green does *not* mean

- Every possible story-level inference (e.g. flight schedules vs signing cities) was checked  
- Advocacy / he-said-she-said between competing briefs was scored  
- Shared topic, caption, or party list was treated as a conflict  
- A specialty detector was invented per lie motif  
- Physiological or linguistic “deception probability” was scored  

Employment, DOB, residence, and income conflicts are **instances** of “same person, incompatible documentary values,” not separate products.

## Architecture (operator view)

```
OCR text → grounded facts (who / attribute / value / quote)
        → cluster same nameable fact
        → incompatible value pairs
        → (optional) LLM four-gate verify on proposed pairs
        → materiality / case-impact
        → interrupt (or stay quiet)
```

Regex/bootstrap extractors name **attributes**. The consistency engine decides when two values cannot both be true. Prefer extending grounded extraction over growing a lie-type enum or swapping foundation models.

## How to seed-test a real case

Use this when a live case is green and you need proof the detector is alive.

### 1. Confirm the clean baseline

Scan the real packet. Note green / no credibility fact-conflict cards.

### 2. Flip one grounded fact

Pick a fact the OCR already sees (hire date, W-2 year, first-default date, fee amount, DOB, residence, presence-on-date, stated principal). In a **copy** of one exhibit or binder page:

- Change only that value (e.g. employment start `March 2019` → `March 2022`), **or**  
- Append a second documentary recital for the same person/employer with an incompatible value, **or**
- For presence plants: flip AOS personal-service place/date against a flight / conference / out-of-state bank spend for the same person+date
- For principal plants: drop judgment principal below note principal while keeping arrears / no-payments language

Keep person and employer the same. Do not invent a second employer (that is a clean twin, not a lie).

### 3. Re-scan the mutated copy

Expect a **credibility** (or occupancy/service **element**, or **damages** for principal decline) interrupt: same nameable fact, incompatible documentary values, both quotes present.

### 4. Restore and confirm quiet again

Revert the mutation. Green should return. That cycle is the trust proof — not a growing keyword list.

### Planted shapes to keep in regression

| Shape | Expect |
|---|---|
| Same person/employer, two hire dates (Summers-style binder) | Ship |
| Paystub hire date after W-2 tax year, same employer | Ship |
| Fee / first-default date conflict across docs | Ship |
| DOB conflict across party docs | Ship |
| Primary residence vs license/tax address | Ship |
| Stated income vs AGI (large divergence) | Ship |
| AOS personal service vs flight / conference / out-of-state spend (same person+date) | Ship (element) |
| AOS personal service vs boarding pass same day | Ship (element) |
| AOS personal service vs process-server daily log (nail-mail / other address) | Ship (credibility) |
| Two balances on the same named date | Ship (damages) |
| Servicer payment dates vs bank debit dates (same cycle) | Ship (credibility) |
| Employer on service narrative vs employment verification | Ship (credibility) |
| Judgment principal drop vs note principal with arrears / no payments | Ship (damages) |
| Distinct sale/deed grantee names (Highland vs IKJG) | Ship |
| Highland vs Highland (same grantee; missing IKJG not invented) | Quiet |
| Mock UA220 / Maple / DOB±1 template (harness fallback) | Deprioritize — not live P0 |
| Same person, **different** employers (near-miss) | Quiet |
| Two recitals that **agree** | Quiet |
| Seeded flip on an otherwise clean packet | Ship |

Fixtures live under `backend/evals/fixtures/lie_detector/`. Run:

```bash
cd backend && python -m evals.run_gotcha_eval
```

The `LIE DETECTOR` block must show `all_pass: true`.

## Blind re-scan of a planted pack (no answer key)

When an external tester drops a planted packet, re-score **blind**:

1. Use only the documentary text files (`.txt` / PDF / images).  
2. **Do not** open `plants.json`, `casesifter-report.json`, or any planted-ID key.  
3. Headless CLI (one file at a time) or the analysis API:

```bash
cd backend
# Prefer txt/PDF exhibits only — exclude plants.json and prior reports
for f in /path/to/pack/*.txt; do
  case "$f" in *plants.json*| *casesifter-report.json*) continue ;; esac
  python -m app.scan_cli "$f" --doc-kind exhibit --json-out "/tmp/cs-$(basename "$f").json"
done
```

4. Score recall against the tester’s sealed answer key **after** the run.  
5. Misses that were never extracted as grounded facts → expand `fact_graph` attributes (presence-on-date, stated_principal, …). Do **not** loosen the four gates or add a free-form LLM lie hunt.

Quiet on Highland-only / noise plants (P006/P007) is correct when there is no grounded AGAINST pair.

## Missed lie → fixture (not a new lie type)

When a human finds documentary BS the system missed:

1. Capture the OCR excerpts (both incompatible values + entity)  
2. Add a planted case under `fixtures/lie_detector/` with `must_ship: true`  
3. Fix the **engine** (grounding, clustering, compatibility) — do **not** add `if employment… if HOA…` specialty branches unless you are naming a new extractable **attribute**  
4. Add a clean twin that must stay quiet  
5. Do **not** “fix” by adding linguistic deception scores or a free-form LLM lie hunt  

## Suppress reminders (still out of scope)

Loan-status movement (note vs default rate, remaining principal **increases**, installment after misses), caption/NYSCEF noise, OCR garbage, ordinary disputes, jurat-only spam, whole-page scan texture without a content hook, recorded-instrument math quirks.

**Exception:** a **judgment/JFS principal that drops** below an earlier note/assignment principal while the packet asserts arrears / missed payments without payment history is **not** ordinary movement — ship under damages.

## Success criteria (product language)

- Novel planted BS that fits “same nameable fact / incompatible documentary values / material” is caught **without** a new hardcoded lie type for that story  
- Summers and paystub-vs-W-2 pass as instances of that rule  
- Real cases may stay green when seeded flips still interrupt  
- CaseSifter finds **documentary BS in the story**, not “we listed every fraud motif” and not “an AI polygraph scored the witness”

## Historical restore point

The 2026-09-22 detector tip (lie detector ship + EXHIBIT forensic path) is tagged
`restore-2026-09-22-detector` → `3c41141`. See [RESTORE_POINTS.md](RESTORE_POINTS.md)
for checkout commands and how current scan-first HEAD compares.
