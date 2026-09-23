# CaseSifter lie detector — operator trust runbook

CaseSifter is an **attorney lie detector**: it surfaces documentary inconsistencies that can **win the case** (credibility wipeout, jurisdiction/occupancy element, damages). It is not a catalog of taught fraud motifs, and it is not a brief-writing assistant.

Quiet means: under the gates, the packet’s own papers do not assert the same nameable fact two incompatible ways. It does **not** mean every narrative inference was checked.

## What the attorney should hear

Ship only when all four hold:

1. **Same nameable fact** — who + attribute + value, grounded in OCR quotes  
2. **Evidence runs AGAINST** — values cannot both be true  
3. **Material** — hits **element / procedure / credibility / damages**  
4. **Both quotes in pulled text**

Examples of attorney-winning interrupts (same meta-rule, different attributes): identity (DOB), occupancy/residence, income vs tax AGI, employment tenure vs W-2, fee/default-date conflicts, exhibit fraud forensics.

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

Employment, DOB, residence, and income conflicts are **instances** of “same person, incompatible documentary values,” not separate products.

## Architecture (operator view)

```
OCR text → grounded facts (who / attribute / value / quote)
        → cluster same nameable fact
        → incompatible value pairs
        → materiality four gates
        → case-impact interrupt (or stay quiet)
```

Regex/bootstrap extractors name **attributes**. The consistency engine decides when two values cannot both be true. Prefer extending grounded extraction over growing a lie-type enum.

## How to seed-test a real case

Use this when a live case is green and you need proof the detector is alive.

### 1. Confirm the clean baseline

Scan the real packet. Note green / no credibility fact-conflict cards.

### 2. Flip one grounded fact

Pick a fact the OCR already sees (hire date, W-2 year, first-default date, fee amount). In a **copy** of one exhibit or binder page:

- Change only that value (e.g. employment start `March 2019` → `March 2022`), **or**  
- Append a second documentary recital for the same person/employer with an incompatible value  

Keep person and employer the same. Do not invent a second employer (that is a clean twin, not a lie).

### 3. Re-scan the mutated copy

Expect a **credibility** interrupt: same nameable fact, incompatible documentary values, both quotes present.

### 4. Restore and confirm quiet again

Revert the mutation. Green should return. That cycle is the trust proof — not a growing keyword list.

### Planted shapes to keep in regression

| Shape | Expect |
|---|---|
| Same person/employer, two hire dates (Summers-style binder) | Ship |
| Paystub hire date after W-2 tax year, same employer | Ship |
| Fee / first-default date conflict across docs | Ship |
| Same person, **different** employers (near-miss) | Quiet |
| Two recitals that **agree** | Quiet |
| Seeded flip on an otherwise clean packet | Ship |

Fixtures live under `backend/evals/fixtures/lie_detector/`. Run:

```bash
cd backend && python -m evals.run_gotcha_eval
```

The `LIE DETECTOR` block must show `all_pass: true`.

## Missed lie → fixture (not a new lie type)

When a human finds documentary BS the system missed:

1. Capture the OCR excerpts (both incompatible values + entity)  
2. Add a planted case under `fixtures/lie_detector/` with `must_ship: true`  
3. Fix the **engine** (grounding, clustering, compatibility) — do **not** add `if employment… if HOA…` specialty branches unless you are naming a new extractable **attribute**  
4. Add a clean twin that must stay quiet  

## Suppress reminders (still out of scope)

Loan-status movement (note vs default rate, remaining principal, installment after misses), caption/NYSCEF noise, OCR garbage, ordinary disputes, jurat-only spam, whole-page scan texture without a content hook, recorded-instrument math quirks.

## Success criteria (product language)

- Novel planted BS that fits “same nameable fact / incompatible documentary values / material” is caught **without** a new hardcoded lie type for that story  
- Summers and paystub-vs-W-2 pass as instances of that rule  
- Real cases may stay green when seeded flips still interrupt  
- CaseSifter finds **documentary BS in the story**, not “we listed every fraud motif”
