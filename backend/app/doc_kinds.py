"""Stable document-kind slugs and UI grouping.

Extend this catalog rather than renaming existing codes. Blank / unknown kind
stays forensic at scan time; court-filing and reference kinds take the packaging
lane (facts + case compare, not exhibit-fraud forensics).
"""

from __future__ import annotations

# (slug, label) — labels are attorney-facing; slugs persist in the DB.
DOC_KIND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Pleadings",
        [
            ("complaint", "Complaint / Amended Complaint"),
            ("petition", "Petition / Amended Petition"),
            ("verified_pleading", "Verified Complaint / Verified Petition"),
            ("answer", "Answer / Amended Answer"),
            ("new_matter", "New Matter"),
            ("affirmative_defenses", "Affirmative Defenses"),
            ("counterclaim", "Counterclaim"),
            ("cross_claim", "Cross-claim"),
            ("third_party_complaint", "Third-Party Complaint"),
            ("reply", "Reply to Counterclaim / Reply to New Matter"),
        ],
    ),
    (
        "Fact statements",
        [
            ("statement_of_facts", "Statement of Undisputed Material Facts"),
            ("counter_statement", "Counter-Statement / Response to Statement of Facts"),
            ("rule_19a_statement", "Rule 19-a Statement (NY) / Concise Statement of Facts"),
        ],
    ),
    (
        "Testimony / discovery",
        [
            ("deposition_transcript", "Deposition transcripts"),
            ("hearing_transcript", "Hearing transcripts"),
            ("trial_transcript", "Trial transcripts"),
            ("transcript", "Transcript (other)"),
            ("interrogatory_answers", "Interrogatory answers"),
            ("rfa_responses", "Responses to Requests for Admission"),
            ("rfp_responses", "Responses to Document Requests"),
        ],
    ),
    (
        "Arguments",
        [
            ("memorandum", "Memorandum of Law / Brief in Support"),
            ("brief", "Brief"),
            ("opposition", "Opposition / Response / Answer to Motion"),
            ("reply_brief", "Reply Brief / Reply Memorandum"),
            ("motion_summary_judgment", "Motion for Summary Judgment"),
            ("motion_dismiss", "Motion to Dismiss / Preliminary Objections"),
            ("motion_vacate", "Motion to Vacate / Set Aside"),
            ("motion_reargue", "Motion to Reargue / Reconsider"),
            ("order_to_show_cause", "Order to Show Cause (with supporting papers)"),
            ("pretrial_memorandum", "Pretrial Memorandum / Pretrial Statement"),
            ("proposed_findings", "Proposed Findings of Fact and Conclusions of Law"),
            ("appellate_brief", "Appellate Briefs (Appellant, Appellee, Reply)"),
            ("motion", "Motion (other)"),
        ],
    ),
    (
        "Reference only",
        [
            ("order", "Orders"),
            ("decision", "Decisions / Opinions"),
            ("judgment", "Judgments"),
        ],
    ),
    (
        "Other filings",
        [
            ("affidavit", "Affidavit"),
            ("affirmation", "Affirmation"),
            ("proof_of_service", "Affidavit of service"),
            ("summons", "Summons"),
            ("stipulation", "Stipulation"),
            ("letter", "Letter"),
            ("notice", "Notice"),
            ("filing", "Administrative filing"),
            ("rji", "RJI"),
            ("notice_of_pendency", "Notice of pendency"),
            ("calendar", "Calendar"),
        ],
    ),
    (
        "Exhibits",
        [
            ("exhibit", "Exhibit"),
            ("bill", "Statement / bill"),
            ("evidence", "Evidence"),
        ],
    ),
]

# Orders / decisions / judgments — what the judge relied on; not lie-scan targets.
REFERENCE_KINDS = frozenset({"order", "decision", "judgment"})

# Pleadings, arguments, testimony, fact statements, reference, and procedural filings.
COURT_FILING_KINDS = frozenset(
    {
        # Pleadings
        "complaint",
        "petition",
        "verified_pleading",
        "answer",
        "new_matter",
        "affirmative_defenses",
        "counterclaim",
        "cross_claim",
        "third_party_complaint",
        "reply",
        # Fact statements
        "statement_of_facts",
        "counter_statement",
        "rule_19a_statement",
        # Testimony / discovery
        "deposition_transcript",
        "hearing_transcript",
        "trial_transcript",
        "transcript",
        "interrogatory_answers",
        "rfa_responses",
        "rfp_responses",
        # Arguments
        "memorandum",
        "brief",
        "opposition",
        "reply_brief",
        "motion_summary_judgment",
        "motion_dismiss",
        "motion_vacate",
        "motion_reargue",
        "order_to_show_cause",
        "pretrial_memorandum",
        "proposed_findings",
        "appellate_brief",
        "motion",
        # Reference
        *REFERENCE_KINDS,
        # Other filings
        "affidavit",
        "affirmation",
        "letter",
        "stipulation",
        "summons",
        "notice",
        "filing",
        "rji",
        "proof_of_service",
        "notice_of_pendency",
        "calendar",
    }
)

EXHIBIT_KINDS = frozenset(
    {
        "exhibit",
        "bill",
        "statement",
        "invoice",
        "check",
        "receipt",
        "photo",
        "image",
        "evidence",
    }
)


def kind_label(slug: str | None) -> str:
    code = (slug or "").strip().lower()
    for _group, options in DOC_KIND_GROUPS:
        for value, label in options:
            if value == code:
                return label
    return code.replace("_", " ").title() if code else ""


def flat_kind_options() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for _group, options in DOC_KIND_GROUPS:
        out.extend(options)
    return out
