"""Headless CaseSifter scan — run one file through the full analysis pipeline.

Usage (from backend/ with deps installed and repo-root .env or env vars set):

  python -m app.scan_cli /path/to/exhibit.pdf
  python -m app.scan_cli /path/to/filing.pdf --doc-kind complaint --json-out report.json
  python -m app.scan_cli ./samples/bill.pdf --doc-kind exhibit

Requires GEMINI_API_KEY for contextual / multimodal lanes. Prefer:
  GEMINI_MODEL=gemini-3.6-flash
  CONTEXTUAL_MULTIMODAL=true
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CaseSifter headless scan (pipeline only — no desktop UI)."
    )
    parser.add_argument("path", type=Path, help="PDF or image exhibit/filing to scan")
    parser.add_argument(
        "--doc-kind",
        default=None,
        help="Optional kind hint: exhibit, complaint, motion, affidavit, … "
        "(affects court-filing vs forensic profile)",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Label for the report (defaults to filename)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write full report JSON here (default: <jobs>/<id>/report.cli.json)",
    )
    parser.add_argument(
        "--copy-html",
        type=Path,
        default=None,
        help="Copy report.html to this path after the scan",
    )
    parser.add_argument(
        "--submitted-by",
        default="headless-cli",
        help="Audit trail submitter label",
    )
    args = parser.parse_args(argv)

    source = args.path.expanduser().resolve()
    if not source.is_file():
        print(f"error: file not found: {source}", file=sys.stderr)
        return 2

    # Import after argv parse so --help works without full env.
    from app.pipeline import run_pipeline
    from app.storage import create_job, job_dir

    job_id = uuid.uuid4().hex
    create_job(job_id, source.name)
    # Pipeline expects the source under the job dir in some code paths; copy for isolation.
    dest = job_dir(job_id) / source.name
    if dest.resolve() != source:
        shutil.copy2(source, dest)

    print(f"scanning job_id={job_id} file={source.name}", flush=True)
    report = run_pipeline(
        job_id,
        dest,
        submitted_by=args.submitted_by,
        display_name=args.display_name or source.name,
        doc_kind=args.doc_kind,
    )

    out_path = args.json_out or (job_dir(job_id) / "report.cli.json")
    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump(mode="json")
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    html_src = job_dir(job_id) / "report.html"
    if args.copy_html and html_src.exists():
        copy_to = args.copy_html.expanduser().resolve()
        copy_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(html_src, copy_to)
        print(f"html={copy_to}", flush=True)

    print(f"risk={report.risk_level} score={report.risk_score}", flush=True)
    print(f"findings={len(report.findings)} top={len(report.top_findings)}", flush=True)
    print(f"json={out_path}", flush=True)
    if html_src.exists():
        print(f"html_job={html_src}", flush=True)
    for finding in report.top_findings[:8]:
        print(
            f"  [{finding.severity}] {finding.category}: {finding.title}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
