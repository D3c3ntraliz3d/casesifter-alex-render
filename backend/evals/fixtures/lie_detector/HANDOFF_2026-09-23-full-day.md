# Lie-detector handoff — 2026-09-23 (full day)

Blind eval from `casesifter-testing` (CaseSifter never loaded `plants.json`).
Buyer framing: bank / big-corp counsel — opposing BS early + own-packet hazards.

## Product rule

Treat **live** P0 shapes as regression. **Do not** optimize sprint work for harness mock fallback packs (UA220 / 220 Maple / income vs AGI / DOB±1) until live generators are richer.

## P0 fixtures in this repo

Live `planted-11467-*` / `planted-702161-*` txt trees were **not vendored** into this CaseSifter checkout. Reconstructed foreclosure-shaped cases live in `planted_bs_cases.json`:

| Fixture id | Shape |
|---|---|
| `p0_aos_vs_boarding_pass_alibi` | Personal service vs boarding pass / flight same day |
| `p0_aos_vs_server_daily_log` | AOS personal service vs server daily log |
| `p0_same_date_balance_collision` | `$398,450` vs `$412,680` as of Jan 18, 2023 |
| `p0_servicer_vs_bank_payment_dates` | Servicer Dec 5 / Jan 8 vs bank Dec 1 / Jan 2 |
| `p0_employer_service_narrative_collision` | Brooklyn Medical Center vs Presbyterian Hospital |

Earlier same-day plants (`p001_*`, `p002_*`, `p003_*`, `p008_*`) remain.

## Blind re-scan (when harness packs are available)

```bash
# *.txt only — never plants.json / casesifter-report.json
cd backend
for f in /path/to/casesifter-testing/test-cases/runs/2026-09-23/<runId>/planted-<seed>/*.txt; do
  python -m app.scan_cli "$f" --doc-kind exhibit --json-out "/tmp/cs-$(basename "$f").json"
done
```

## Keep green (caught today)

Arrearage / distribution arithmetic, RPAPL 1304 date math, Hauppauge→Brooklyn travel, notice-before-assignment, edited-exhibit / occupancy procedural interrupts.
