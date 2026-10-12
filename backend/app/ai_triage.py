"""Content-based AI triage: does this document assert facts?

Three buckets (never filename-driven):

1. ``full``  — party/witness/attorney asserts facts, or offered as proof.
2. ``light`` — court orders/judgments/decisions; extract holdings only.
3. ``none``  — pure procedural / court admin text; local extract only.

Low classification confidence always falls through to ``full``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

AiBucket = Literal["full", "light", "none"]
AiConfidence = Literal["high", "low"]

BUCKET_FULL = "full"
BUCKET_LIGHT = "light"
BUCKET_NONE = "none"

# Sworn / jurat proof — beats hard procedural forms (RJI, NOA, e-filing).
SWORN_PROOF_RE = re.compile(
    r"(?i)("
    r"duly\s+sworn|"
    r"being\s+duly\s+sworn|"
    r"under\s+(?:the\s+)?penalty\s+of\s+perjury|"
    r"under\s+penalties?\s+of\s+perjury|"
    r"depose(?:s|d)?\s+and\s+say|"
    r"subscribed\s+and\s+sworn|"
    r"notary\s+public|"
    r"\bjurat\b|"
    r"before\s+me\s+(?:this|the)\s+\d|"
    r"sworn\s+to\s+before\s+me|"
    r"\baffidavit\b"
    r")"
)

# --- Bucket 1 strong: fact exhibits / affirmative papers (after hard-none check) ---
STRONG_FULL_RE = re.compile(
    r"(?i)("
    r"duly\s+sworn|"
    r"being\s+duly\s+sworn|"
    r"under\s+(?:the\s+)?penalty\s+of\s+perjury|"
    r"under\s+penalties?\s+of\s+perjury|"
    r"affirm(?:s|ed)?\s+(?:under|pursuant)\s+(?:the\s+)?penalt|"
    r"affirmation\s+(?:under|pursuant)\s+(?:the\s+)?penalt|"
    r"depose(?:s|d)?\s+and\s+say|"
    r"personal\s+knowledge|"
    r"true\s+and\s+correct|"
    r"subscribed\s+and\s+sworn|"
    r"notary\s+public|"
    r"\bjurat\b|"
    r"before\s+me\s+(?:this|the)\s+\d|"
    r"sworn\s+to\s+before\s+me|"
    r"\baffidavit\b|"
    r"attorney\s+affirmation|"
    r"affirmation\s+in\s+(?:support|opposition|reply)|"
    r"referee(?:'s)?\s+report|"
    r"report\s+of\s+(?:the\s+)?referee|"
    r"account\s*(?:number|no\.?)|"
    r"amount\s*due|"
    r"statement\s*date|"
    r"previous\s*balance|"
    r"principal\s+balance|"
    r"monthly\s+(?:payment|installment)|"
    r"interest\s+rate|"
    r"mortgage\s+(?:note|loan)|"
    r"promissory\s+note|"
    r"invoice\s*(?:number|no\.?)|"
    r"billing\s*(?:period|date)|"
    r"service\s*address|"
    r"exhibit\s+[A-Z0-9]|"
    r"annexed\s+(?:hereto|as\s+exhibit)|"
    r"true\s+and\s+accurate\s+copy"
    r")"
)

# Pleadings / discovery demands that assert facts (after procedural/order checks).
PLEADING_FULL_RE = re.compile(
    r"(?i)("
    r"\bamended\s+complaint\b|"
    r"\bcomplaint\b|"
    r"\banswer\b(?!\s+to\s+the\s+calendar)|"
    r"\bcounterclaim\b|"
    r"\bcross-?claim\b|"
    r"third-?party\s+complaint|"
    r"wherefore.{0,120}demands?\s+judgment|"
    r"plaintiff\s+respectfully\s+(?:shows|alleges|states)|"
    r"defendant\s+respectfully\s+(?:shows|alleges|states)|"
    r"interrogator(?:y|ies)|"
    r"request(?:s)?\s+for\s+admission|"
    r"deposition\s+(?:of|transcript|upon)|"
    r"examination\s+before\s+trial|"
    r"bill\s+of\s+particulars|"
    r"notice\s+of\s+motion|"
    r"order\s+to\s+show\s+cause|"
    r"\bsummons\b|"
    r"\baffirmation\b"
    r")"
)

# --- Bucket 2: court speaks (order / judgment / decision) ---
LIGHT_SIGNAL_RE = re.compile(
    r"(?i)("
    r"\bordered\b|"
    r"\badjudged\b|"
    r"\badjudged\s+and\s+decreed\b|"
    r"it\s+is\s+(?:hereby\s+)?ordered|"
    r"it\s+is\s+(?:hereby\s+)?adjudged|"
    r"ordered\s*,?\s*adjudged|"
    r"\bdecree(?:d|s)?\b|"
    r"this\s+court\s+(?:finds|holds|concludes)|"
    r"the\s+court\s+(?:finds|holds|concludes|orders)|"
    r"memorandum\s+(?:and\s+)?(?:decision|order)|"
    r"decision\s+and\s+order|"
    r"short\s+form\s+order|"
    r"\bjudgment\s+(?:of|is\s+hereby|entered)|"
    r"enter\s+judgment|"
    r"so\s+ordered"
    r")"
)

# --- Bucket 3: pure procedural / admin / scheduling ---
NONE_SIGNAL_RE = re.compile(
    r"(?i)("
    r"notice\s+of\s+electronic\s+filing|"
    r"notification\s+of\s+electronic\s+filing|"
    r"consent\s+to\s+(?:electronic\s+)?e-?filing|"
    r"consent\s+to\s+electronic\s+filing|"
    r"e-?filing\s+consent|"
    r"notice\s+of\s+appearance|"
    r"request\s+for\s+judicial\s+intervention|"
    r"document\s+cover\s+sheet|"
    r"civil\s+cover\s+sheet|"
    r"nyscef\s+(?:case\s+)?confirmation|"
    r"confirmation\s+notice|"
    r"notice\s+of\s+adjournment|"
    r"adjourned\s+(?:to|until)|"
    r"adjournment\s+of\s+(?:the\s+)?(?:sale|hearing|conference|auction)|"
    r"sale\s+date\s+(?:is\s+)?(?:adjourned|changed|continued)|"
    r"hearing\s+date\s+(?:is\s+)?(?:adjourned|changed|continued)|"
    r"notice\s+of\s+(?:sale\s+)?(?:date\s+)?change|"
    r"calendar\s+(?:call|notice|control)|"
    r"notice\s+of\s+motion\s+calendar|"
    r"notice\s+of\s+entry|"
    r"stipulation\s+(?:of\s+)?adjournment|"
    r"stipulation\s+adjourning|"
    r"adjournment\s+of\s+motion|"
    r"preliminary\s+conference\s+stipulation\s+and\s+order\s+form|"
    r"track\s+assignment\s+notice"
    r")"
)

# Weak full signals — alone they are low-confidence (still → full).
WEAK_FULL_RE = re.compile(
    r"(?i)("
    r"please\s+take\s+notice|"
    r"attorney\s+for\s+(?:plaintiff|defendant)|"
    r"respectfully\s+submitted"
    r")"
)


@dataclass(frozen=True)
class AiTriage:
    bucket: AiBucket
    reason: str
    confidence: AiConfidence
    signal: str = ""

    @property
    def runs_gemini(self) -> bool:
        return self.bucket != BUCKET_NONE

    @property
    def light_only(self) -> bool:
        return self.bucket == BUCKET_LIGHT

    def as_dict(self) -> dict[str, str]:
        return {
            "ai_bucket": self.bucket,
            "ai_bucket_reason": self.reason,
            "ai_bucket_confidence": self.confidence,
            "ai_bucket_signal": self.signal,
        }


def _match_token(match: re.Match[str] | None) -> str:
    if not match:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", match.group(0).lower()).strip("_")[:64]


def triage_document_ai(text: str, *, document_label: str | None = None) -> AiTriage:
    """Classify AI intensity from document *content* only.

    Filename / display name are ignored. Empty or unreadable text → full (low).
    """
    head = (text or "")[:16000]
    label = (document_label or "document").strip() or "document"
    if not head.strip():
        result = AiTriage(
            bucket=BUCKET_FULL,
            reason="low_confidence_empty_text",
            confidence="low",
            signal="",
        )
        logger.info(
            "ai_triage[%s] bucket=%s reason=%s confidence=%s signal=-",
            label,
            result.bucket,
            result.reason,
            result.confidence,
        )
        return result

    sworn_m = SWORN_PROOF_RE.search(head)
    strong_m = STRONG_FULL_RE.search(head)
    light_m = LIGHT_SIGNAL_RE.search(head)
    none_m = NONE_SIGNAL_RE.search(head)
    pleading_m = PLEADING_FULL_RE.search(head)
    weak_m = WEAK_FULL_RE.search(head)
    summons_m = re.search(r"(?i)\bsummons\b", head[:2000])

    # Priority:
    # 1) Sworn proof / jurat → full (beats RJI/NOA chrome on an AOS).
    # 2) Strong fact papers (affirmation in support, exhibits, statements) → full
    #    (beats incidental ORDERED quotes inside motion papers).
    # 3) Court order/judgment → light.
    # 4) Hard procedural forms (e-filing, NOA, adjournment, RJI title) → none
    #    unless a leading summons caption forces full.
    # 5) Pleadings / discovery → full.
    # 6) Default → full (low confidence).
    if sworn_m:
        token = _match_token(sworn_m)
        result = AiTriage(
            bucket=BUCKET_FULL,
            reason=f"full_signal_{token}",
            confidence="high",
            signal=token,
        )
    elif strong_m:
        token = _match_token(strong_m)
        result = AiTriage(
            bucket=BUCKET_FULL,
            reason=f"full_signal_{token}",
            confidence="high",
            signal=token,
        )
    elif light_m:
        token = _match_token(light_m)
        result = AiTriage(
            bucket=BUCKET_LIGHT,
            reason=f"light_signal_{token}",
            confidence="high",
            signal=token,
        )
    elif none_m and not summons_m:
        token = _match_token(none_m)
        result = AiTriage(
            bucket=BUCKET_NONE,
            reason=f"none_signal_{token}",
            confidence="high",
            signal=token,
        )
    elif pleading_m:
        token = _match_token(pleading_m)
        result = AiTriage(
            bucket=BUCKET_FULL,
            reason=f"full_signal_{token}",
            confidence="high",
            signal=token,
        )
    elif weak_m:
        token = _match_token(weak_m)
        result = AiTriage(
            bucket=BUCKET_FULL,
            reason=f"low_confidence_{token}",
            confidence="low",
            signal=token,
        )
    else:
        result = AiTriage(
            bucket=BUCKET_FULL,
            reason="low_confidence_default",
            confidence="low",
            signal="",
        )

    logger.info(
        "ai_triage[%s] bucket=%s reason=%s confidence=%s signal=%s",
        label,
        result.bucket,
        result.reason,
        result.confidence,
        result.signal or "-",
    )
    return result


# Backward-compatible shim for call sites that still ask "should we skip AI?"
def boilerplate_skip_reason(
    text: str = "",
    *,
    filename: str = "",
    display_name: str = "",
    doc_kind: str | None = None,
) -> str | None:
    """Deprecated: map Bucket 3 → skip token. Filename/doc_kind ignored."""
    del filename, display_name, doc_kind
    triage = triage_document_ai(text)
    if triage.bucket == BUCKET_NONE:
        return triage.reason
    return None
