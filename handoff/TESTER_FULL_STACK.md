# CaseSifter / Courtdocverifier — tester handoff (analysis stack)

Full scanable backend + **headless CLI**. Not the Electron desktop UI.

## Clone (preferred)

```bash
git clone -b cursor/cloud-agent-1790049794515-xh7tp \
  https://github.com/johnnyg123456/court-exhibit-scanner.git
cd court-exhibit-scanner/backend
python -m venv .venv && source .venv/bin/activate   # or Windows Activate.ps1
pip install -r requirements.txt
cp ../.env.example ../.env
# edit ../.env — set GEMINI_API_KEY=…  (GEMINI_MODEL already gemini-3.6-flash)
```

## Headless scan CLI

```bash
cd backend
python -m app.scan_cli /path/to/exhibit.pdf --doc-kind exhibit --json-out ./out.json
python -m app.scan_cli /path/to/complaint.pdf --doc-kind complaint
```

Writes risk level, findings JSON, and `report.html` under the jobs directory.

## Gemini

| Setting | Value |
|---|---|
| `GEMINI_MODEL` | `gemini-3.6-flash` |
| `CONTEXTUAL_MULTIMODAL` | `true` → original PDF/image sent as base64 `inline_data` + text excerpt |
| Court filings | Native file upload skipped (text/case logic only) |

## Claude (optional gated verifier)

| Setting | Value |
|---|---|
| `GOTCHA_VERIFY_PROVIDER` | `auto` (default) |
| `ANTHROPIC_API_KEY` | Set to prefer Claude for gotcha verify only |
| `CLAUDE_MODEL` | `claude-sonnet-4-5-20250929` |

Claude judges **proposed** claim/evidence pairs behind hard vetoes + four gates. It does not run a free-form “find all lies” pass. Primary quality path remains grounded fact extraction — see `docs/LIE_DETECTOR_TRUST.md`.

Use `.env.example` only as a template — never commit live keys.

## Optional: HTTP API instead of CLI

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
# then POST /api/v1/jobs with the file (see docs/RENDER_HYBRID.md)
```
