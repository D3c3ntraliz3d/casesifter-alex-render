"""Grounded documentary fact graph — same nameable fact, not a lie taxonomy.

Extracts entity-scoped attributes (person/employer + attribute + value + quote)
so novel documentary contradictions can conflict without a new hardcoded lie type
per story. Regex/bootstrap extractors name *attributes*; the consistency engine
decides when two values cannot both be true.
"""

from __future__ import annotations

import re
from typing import Any

from dateutil import parser as dateparser

from app.fact_extract import DATE_TOKEN, _date

# Documentary attributes eligible for same-entity conflict.
# Attribute names only — the attorney interrupt is credibility / element when
# the party's own papers cannot both be true.
GRAPH_ATTRIBUTES: dict[str, dict[str, Any]] = {
    "employment_start": {
        "label": "Employment start",
        "kind": "date",
        "documentary": True,
        "impact": "credibility",
    },
    "employment_tax_year": {
        "label": "Employment tax year (W-2)",
        "kind": "year",
        "documentary": True,
        "impact": "credibility",
    },
    "date_of_birth": {
        "label": "Date of birth",
        "kind": "date",
        "documentary": True,
        "impact": "credibility",
        "deadline": True,
    },
    "residence_address": {
        "label": "Residence / occupancy address",
        "kind": "address",
        "documentary": True,
        "impact": "element",
    },
    "annual_income": {
        "label": "Annual income (stated or tax)",
        "kind": "money",
        "documentary": True,
        "impact": "credibility",
        "abs": 8000.0,
        "pct": 0.20,
    },
    "presence_on_date": {
        "label": "Presence / location on a date",
        "kind": "presence",
        "documentary": True,
        "impact": "credibility",
    },
    "stated_principal": {
        "label": "Stated principal",
        "kind": "money",
        "documentary": True,
        "impact": "damages",
        "abs": 1000.0,
        "pct": 0.005,
    },
    "sale_grantee": {
        "label": "Sale / deed grantee",
        "kind": "entity",
        "documentary": True,
        "impact": "credibility",
    },
    "service_attempt": {
        "label": "Process-server service attempt",
        "kind": "service_attempt",
        "documentary": True,
        "impact": "credibility",
    },
    "balance_on_date": {
        "label": "Balance on a named date",
        "kind": "money_on_date",
        "documentary": True,
        "impact": "damages",
        "abs": 1000.0,
        "pct": 0.005,
    },
    "loan_payment_date": {
        "label": "Loan payment date (servicer vs bank)",
        "kind": "payment_date",
        "documentary": True,
        "impact": "credibility",
    },
    "employer_of_record": {
        "label": "Employer of record",
        "kind": "entity",
        "documentary": True,
        "impact": "credibility",
    },
    "stated_interest_rate": {
        "label": "Stated interest rate",
        "kind": "rate",
        "documentary": True,
        "impact": "damages",
        "abs": 0.10,
    },
    "execution_venue": {
        "label": "Affidavit execution venue",
        "kind": "venue",
        "documentary": True,
        "impact": "credibility",
    },
}

# Attribute keys that participate in gotcha-style case conflict aggregation.
GRAPH_CONFLICT_KEYS = frozenset(GRAPH_ATTRIBUTES.keys())

MONTHS = (
    r"January|February|March|April|May|June|July|August|September|October|November|December"
)

# "Employed since March 2022" / "employed since March 15, 2022"
EMPLOYED_SINCE_RE = re.compile(
    rf"(?i)(?:employed\s+since|date\s+of\s+hire|hire\s+date|start\s+date|"
    rf"commenced\s+(?:employment|work)|began\s+(?:employment|work))"
    rf"\D{{0,24}}({DATE_TOKEN})"
)

# "in the position of Senior Business Analyst since March 2019"
POSITION_SINCE_RE = re.compile(
    rf"(?i)(?:in\s+the\s+position\s+of|as\s+(?:a|an)\s+)"
    rf"([A-Za-z][\w .&'-]{{1,60}}?)"
    rf"\s+since\s+({DATE_TOKEN})"
)

# "has been employed by TechCore Solutions ... since March 2019"
EMPLOYED_BY_SINCE_RE = re.compile(
    rf"(?i)(?:has\s+been\s+)?employed\s+by\s+"
    rf"([A-Z][A-Za-z0-9 &.'-]{{2,60}}?)"
    rf"(?:\.|,|\s+located|\s+in\b).{{0,100}}?"
    rf"since\s+({DATE_TOKEN})"
)

# "Michael Summers: … Employed since …" or name near employment language
PERSON_NEAR_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3})\b"
    r"(?:\s*[:,]\s*|\s+(?:is|has\s+been|was)\s+)"
)

PERSON_STOP = frozenset(
    {
        "employment",
        "verification",
        "commitment",
        "mortgage",
        "corporation",
        "issue",
        "date",
        "letter",
        "borrowers",
        "summit",
        "loan",
        "officer",
        "annual",
        "salary",
        "current",
        "senior",
        "business",
        "analyst",
        "registered",
        "nurse",
        "form",
        "statement",
        "employee",
        "employer",
        "summons",
        "complaint",
        "upon",
        "the",
        "true",
        "copy",
        "copies",
        "each",
        "delivering",
        "affidavit",
        "service",
        "process",
        "server",
        "company",
        "solutions",
        "medical",
        "center",
        "new",
        "jersey",
        "cherry",
        "hill",
        "maple",
        "avenue",
        "market",
        "street",
        "francisco",
        "san",
        "queens",
        "brooklyn",
        "manhattan",
        "adjusted",
        "gross",
        "income",
        "primary",
        "residence",
        "driver",
        "license",
        "uniform",
        "residential",
        "application",
        "deposition",
        "transcript",
        "continuously",
    }
)

EMPLOYER_NEAR_RE = re.compile(
    r"(?:"
    r"\b(?:[Aa]t|[Ww]ith|[Ff]or)\s+"
    r"(?P<preposition>[A-Z][A-Za-z0-9 &.'-]{1,50}?"
    r"(?:Inc|LLC|LLP|Corp|Corporation|Solutions|Systems|Center|Company|Hospital)\.?)"
    r"|"
    r"\b(?P<title>[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9&.']+){0,3}\s+"
    r"(?:Solutions|Systems|Corporation|Company|Center|Hospital|Inc|LLC))\b"
    r")"
)

EMPLOYEE_LABEL_RE = re.compile(
    r"(?i)\b(?:employee(?:'s)?\s+name|employee|borrower|applicant|"
    r"deponent|taxpayer|name)\s*[:#]\s*"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3})"
)

EMPLOYER_LABEL_RE = re.compile(
    r"(?i)\b(?:employer(?:'s)?\s+name|employer)\s*[:#]\s*"
    r"([A-Z][A-Za-z0-9 &.'-]{2,70})"
)

EMPLOYER_NOISE = re.compile(
    r"(?i)\b(employed|employment|position|salary|located|since|borrower|verification)\b"
)

# W-2 tax year: OCR spans newlines; use DOTALL-style [\s\S].
W2_YEAR_RE = re.compile(
    r"(?i)(?:form\s*w-?2|wage\s+and\s+tax\s+statement)[\s\S]{0,400}?"
    r"(?:(?:tax\s+)?year|for\s+(?:the\s+)?year|calendar\s+year)?\D{0,20}"
    r"(20\d{2})"
)

W2_YEAR_ALT_RE = re.compile(
    r"(?i)(?:tax\s+year|for\s+(?:the\s+)?(?:calendar\s+)?year)\s*(20\d{2})"
    r"[\s\S]{0,200}?(?:form\s*w-?2|wage\s+and\s+tax)"
)

# "Form W-2" present in doc + a clear tax year elsewhere on the page
W2_DOC_MARKER_RE = re.compile(r"(?i)\b(?:form\s*w-?2|wage\s+and\s+tax\s+statement)\b")
TAX_YEAR_ONLY_RE = re.compile(
    r"(?i)(?:tax\s+year|calendar\s+year|for\s+(?:the\s+)?year)\s*[:#]?\s*(20\d{2})"
)

# Paystub hire after a W-2 year is impossible for continuous employment at that employer.
PAYSTUB_HIRE_RE = re.compile(
    rf"(?i)(?:hire\s+date|date\s+of\s+hire|start\s+date|employee\s+start)"
    rf"\D{{0,20}}({DATE_TOKEN}|({MONTHS})\.?\s+\d{{4}}|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}})"
)

# Person identity / occupancy / income — attorney-winning documentary attributes.
DOB_RE = re.compile(
    rf"(?i)(?:date\s+of\s+birth|d\.?o\.?b\.?|birth\s+date|born\s+on)"
    rf"\s*[:#]?\s*({DATE_TOKEN}|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}})"
)

RESIDENCE_RE = re.compile(
    r"(?i)(?:primary\s+residence|resides?\s+at|residential\s+address|"
    r"home\s+address|dwelling\s+(?:at|address)|I\s+live\s+at|"
    r"(?:has\s+)?(?:lived|resided)\s+continuously\s+at|"
    r"continuously\s+(?:resided|lived)\s+at|"
    r"(?:driver'?s?\s+)?licen[cs]e\s+address|mailing\s+address\s+on\s+(?:tax|return)|"
    r"(?:tax\s+return|form\s*1040).{0,40}address)"
    r"\s*[:#]?\s*"
    r"([0-9]{1,6}(?:-[0-9]{1,4})?\s+[A-Za-z0-9 .'-]{2,60}(?:,?\s*[A-Za-z .'-]{2,40}){0,3})"
)

# Bare "220 Maple … Queens" after primary-residence language without "at"
RESIDENCE_INLINE_RE = re.compile(
    r"(?i)primary\s+residence\s+"
    r"([0-9]{1,6}(?:-[0-9]{1,4})?\s+[A-Za-z0-9 .'-]{2,80})"
)

INCOME_MONTHLY_RE = re.compile(
    r"(?i)(?:gross\s+monthly(?:\s+income)?|monthly\s+(?:gross\s+)?income|"
    r"base\s+monthly\s+(?:income|salary))"
    r"[^\d$]{0,24}\$?\s*([\d,]{2,12}(?:\.\d{2})?)"
)

INCOME_ANNUAL_RE = re.compile(
    r"(?i)(?:(?:adjusted\s+gross\s+income|\bAGI\b)|"
    r"annual\s+(?:gross\s+)?(?:income|salary)|gross\s+annual(?:\s+income)?|"
    r"yearly\s+(?:gross\s+)?income)"
    r"[^\d$]{0,24}\$?\s*([\d,]{2,12}(?:\.\d{2})?)"
)

INCOME_APPROX_ANNUAL_RE = re.compile(
    r"(?i)(?:gross\s+monthly|monthly\s+income)[^\d$]{0,40}"
    r"\$?\s*([\d,]{2,12}(?:\.\d{2})?)"
    r"[^\n]{0,40}(?:≈|~|approximately|about|roughly)?\s*"
    r"(?:\$?\s*([\d,]{2,12})\s*k\b|\$?\s*([\d,]{2,12}(?:\.\d{2})?)\s*(?:per\s+year|/yr|annual))"
)

# Personal service AOS: On DATE … I served … upon NAME … personally … at place
PERSONAL_SERVICE_DATE_LEAD_RE = re.compile(
    rf"(?i)on\s+({DATE_TOKEN}),?\s+at\s+(?:approximately\s+)?"
    rf"(\d{{1,2}}:\d{{2}}\s*[ap]\.?m\.?),"
    rf"\s+I\s+served\s+.{{0,100}}?upon\s+((?:[A-Z][A-Za-z'-]+\s+){{1,3}}[A-Z][A-Za-z'-]+)"
    rf".{{0,180}}?personally"
    rf".{{0,140}}?(?:at\s+)([0-9]{{1,6}}(?:-[0-9]{{1,4}})?\s+[A-Za-z0-9 .,'-]{{3,80}})"
)

PERSONAL_SERVICE_SAME_DATE_RE = re.compile(
    rf"(?i)I\s+served\s+(?:the\s+same\s+documents\s+)?upon\s+"
    rf"([A-Z][A-Za-z .'-]{{2,60}}?)"
    rf"\s+on\s+the\s+same\s+date,\s*({DATE_TOKEN})"
    rf".{{0,140}}?personally"
    rf".{{0,140}}?(?:at\s+)([0-9]{{1,6}}(?:-[0-9]{{1,4}})?\s+[A-Za-z0-9 .,'-]{{3,80}})"
)

FLIGHT_DEPART_RE = re.compile(
    rf"(?i)(?:departed|depart|flew)\s+from\s+"
    rf"([A-Za-z][A-Za-z .'-]{{2,40}}?(?:Airport|Field))"
    rf".{{0,120}}?on\s+({DATE_TOKEN})"
)

OUT_OF_STATE_RE = re.compile(
    rf"(?i)out\s+of\s+state.{{0,120}}?on\s+({DATE_TOKEN})"
    rf"|on\s+({DATE_TOKEN}).{{0,100}}?out\s+of\s+state"
)

CONFERENCE_RE = re.compile(
    rf"(?i)(?:attended|attending)\s+(?:the\s+)?"
    rf"([A-Za-z0-9 &'.,-]{{4,80}}?Conference)"
    rf".{{0,120}}?from\s+({DATE_TOKEN})\s*(?:through|-|to|–|—)\s*({DATE_TOKEN})"
)

WIFE_TRAVELING_RE = re.compile(
    rf"(?i)(?:my\s+wife|spouse),\s*([A-Z][a-z]+(?:\s+[A-Z][a-z']+){{0,2}})"
    rf",?\s+was\s+also\s+traveling.{{0,80}}?to\s+([A-Za-z .'-]{{2,40}})"
    rf".{{0,100}}?on\s+({DATE_TOKEN})"
)

BANK_SPEND_RE = re.compile(
    rf"(?i)({DATE_TOKEN})\s*[-–—]\s*(?:Debit\s+Card|Purchase|POS)?"
    rf"\s*[-–—]?\s*([A-Za-z0-9 &'.,-]{{3,70}})"
    rf"\s*[-–—]\s*\$\s*[\d,]+\.\d{{2}}"
)

NOTE_PRINCIPAL_RE = re.compile(
    r"(?i)(?:principal\s+amount|original\s+(?:loan\s+)?principal|principal\s+sum(?:\s+of)?)"
    r"[^\d$]{0,24}\$?\s*([\d,]{3,12}(?:\.\d{2})?)"
)

JUDGMENT_PRINCIPAL_LINE_RE = re.compile(
    r"(?i)(?:^|\n)\s*principal\s*:\s*\$?\s*([\d,]{3,12}(?:\.\d{2})?)"
)

ARREARS_HINT_RE = re.compile(
    r"(?i)\b(arrears|missed\s+payments?|four\s+missed|default(?:ed)?\s+on|"
    r"no\s+payments?\s+(?:were\s+)?(?:made|received))\b"
)

SALE_GRANTEE_RE = re.compile(
    r"(?i)(?:successful\s+bidder\s+was|"
    r"authorize\s+the\s+delivery\s+of\s+a\s+referee'?s?\s+deed\s+to|"
    r"referee'?s?\s+deed\s+(?:delivered\s+)?to|"
    r"deed\s+delivered\s+to)\s*"
    r"((?:[A-Z][A-Za-z0-9&'.-]+(?:\s+[A-Z0-9][A-Za-z0-9&'.-]*){0,5})"
    r"(?:\s+(?:LLC|Inc\.?|Corp\.?|L\.?P\.?|LLP))?)"
)

NYC_PLACE_TOKENS = frozenset(
    {
        "brooklyn",
        "queens",
        "manhattan",
        "bronx",
        "bedford",
        "new york",
        "nyc",
        "hauppauge",
        "long island",
    }
)
REMOTE_PLACE_TOKENS = frozenset(
    {
        "chicago",
        "illinois",
        "out of state",
        "california",
        "san francisco",
        "orlando",
        "miami",
        "dallas",
        "houston",
        "atlanta",
        "las vegas",
        "denver",
        "seattle",
        "boston",
        "detroit",
        "phoenix",
    }
)
AIRPORT_DEPART_TOKENS = frozenset(
    {"laguardia", "jfk", "kennedy", "newark", "lga", "ewr", "airport"}
)



BOARDING_PASS_RE = re.compile(
    rf"(?i)boarding\s+pass.{{0,200}}?"
    rf"from\s+((?:[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){{0,3}}\s+)?Airport)"
    rf".{{0,40}}?on\s+({DATE_TOKEN})"
)

# Process server daily log / AOS method
NAIL_MAIL_RE = re.compile(
    rf"(?i)(?:nail[- ]and[- ]mail|nailed\s+(?:and\s+)?mail(?:ed)?|"
    rf"affixed\s+(?:a\s+copy|the\s+summons).{{0,40}}?mail)"
    rf".{{0,100}}?(?:at\s+)?(?:approximately\s+)?"
    rf"(?:(\d{{1,2}}:\d{{2}}\s*[ap]\.?m\.?)|"
    rf"on\s+({DATE_TOKEN}))"
    rf".{{0,120}}?(?:at\s+)?([0-9]{{1,6}}(?:-[0-9]{{1,4}})?\s+[A-Za-z0-9 .,'-]{{3,80}})?"
)

SERVICE_PERSONAL_TIME_RE = re.compile(
    rf"(?i)(?:on\s+({DATE_TOKEN}))?.{{0,60}}?"
    rf"(?:at\s+(?:approximately\s+)?)?(\d{{1,2}}:\d{{2}}\s*[ap]\.?m\.?)"
    rf".{{0,120}}?(?:I\s+)?"
    rf"(?:personally\s+served|served.{{0,100}}?personally|effected\s+personal\s+service)"
    rf".{{0,100}}?(?:upon\s+|at\s+)?"
    rf"(?:((?:[A-Z][A-Za-z'-]+\s+){{1,3}}[A-Z][A-Za-z'-]+)\s+)?"
    rf"(?:at\s+)?([0-9]{{1,6}}(?:-[0-9]{{1,4}})?\s+[A-Za-z0-9 .,'-]{{3,80}})"
)

SERVER_LOG_LINE_RE = re.compile(
    rf"(?i)(?:^|\n)\s*(?:[-*•]\s*)?(?:({DATE_TOKEN})\s+)?"
    rf"(\d{{1,2}}:\d{{2}}\s*[ap]\.?m\.?)\s*[-–—:]?\s*"
    rf"(personal(?:ly)?(?:\s+service)?|nail[- ]and[- ]mail|attempt(?:ed)?|"
    rf"no\s+answer|posted|mailed)"
    rf".{{0,40}}?at\s+([0-9]{{1,6}}(?:-[0-9]{{1,4}})?\s+[A-Za-z0-9 .,'-]{{3,70}})"
)

PROCESS_SERVER_NAME_RE = re.compile(
    r"(?i)\bI,\s*((?:[A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3})),"
    r"\s*(?:being\s+duly\s+sworn|a\s+process\s+server|state)"
)

# Balance as of / outstanding on a named date
BALANCE_ON_DATE_RE = re.compile(
    rf"(?i)(?:(?:outstanding|unpaid|principal)?\s*balance|amount\s+due|total\s+due|"
    rf"sum\s+due|indebtedness)"
    rf"[^\d$]{{0,40}}\$?\s*([\d,]{{3,12}}(?:\.\d{{2}})?)"
    rf".{{0,60}}?(?:as\s+of|on|dated)\s+({DATE_TOKEN})"
    rf"|(?:as\s+of|on)\s+({DATE_TOKEN})"
    rf"[^\d$]{{0,40}}(?:(?:outstanding|unpaid|principal)?\s*balance|amount\s+due)"
    rf"[^\d$]{{0,24}}\$?\s*([\d,]{{3,12}}(?:\.\d{{2}})?)"
)

# Frozen principal across months still "as of" earlier date restated later
BALANCE_AS_OF_RE = re.compile(
    rf"(?i)(?:principal\s+(?:balance\s+)?(?:remains|was|is)|balance\s+(?:remains|was|is))"
    rf"[^\d$]{{0,24}}\$?\s*([\d,]{{3,12}}(?:\.\d{{2}})?)"
    rf".{{0,80}}?(?:as\s+of|on)\s+({DATE_TOKEN})"
)

# Servicer: payments received / credited on dates
SERVICER_PAYMENT_RE = re.compile(
    rf"(?i)(?:(?:trial\s+)?payments?\s+(?:were\s+)?(?:received|credited|posted|made)|"
    rf"received\s+(?:trial\s+)?payments?)"
    rf".{{0,120}}?({DATE_TOKEN})(?:\s*(?:,|and|&)\s*({DATE_TOKEN}))?"
)

SERVICER_PAYMENT_LINE_RE = re.compile(
    rf"(?i)(?:payment\s+(?:received|credited|posted)|credited)\s*"
    rf"(?:on\s+)?({DATE_TOKEN})"
    rf"(?:[^\d$]{{0,40}}\$?\s*([\d,]{{2,12}}(?:\.\d{{2}})?))?"
)

BANK_DEBIT_RE = re.compile(
    rf"(?i)({DATE_TOKEN})\s*[-–—]\s*"
    rf"(?:ACH|Debit|Withdrawal|Auto[- ]Pay|Mortgage\s+Payment|Payment)?"
    rf"[^\n]{{0,40}}?(?:mortgage|loan|servicer|payment)?"
    rf"[^\n]{{0,40}}\$\s*([\d,]+\.\d{{2}})"
)

# "on (my )?way to EMPLOYER" on AOS narrative
WAY_TO_EMPLOYER_RE = re.compile(
    r"(?i)(?:on\s+(?:my\s+)?way\s+to|employed\s+at|works?\s+at|works?\s+for|"
    r"place\s+of\s+employment)\s+"
    r"((?:[A-Z][A-Za-z0-9&'.-]+(?:\s+[A-Z0-9][A-Za-z0-9&'.-]*){0,5})"
    r"(?:\s+(?:Hospital|Center|Medical|Clinic|Corp|LLC|Inc))?)"
)

EMPLOYER_VERIFICATION_RE = re.compile(
    r"(?i)employer\s*(?:name)?\s*:\s*"
    r"((?:[A-Z][A-Za-z0-9&'.-]+(?:[ \t]+[A-Z0-9][A-Za-z0-9&'.-]*){0,5})"
    r"(?:[ \t]+(?:Hospital|Center|Medical|Clinic|Corp|LLC|Inc))?)"
)


SUBSTITUTE_SERVICE_RE = re.compile(
    rf"(?i)(?:on\s+({DATE_TOKEN}))?.{{0,60}}?"
    rf"(?:at\s+(?:approximately\s+)?)?(\d{{1,2}}:\d{{2}}\s*[ap]\.?m\.?)?"
    rf".{{0,80}}?(?:substitute\s+service|suitable\s+age\s+and\s+discretion|"
    rf"left\s+(?:a\s+copy|copies)\s+with|person\s+of\s+suitable\s+age)"
    rf".{{0,120}}?(?:at\s+)?([0-9]{{1,6}}(?:-[0-9]{{1,4}})?\s+[A-Za-z0-9 .,'-]{{3,80}})?"
)

FLIGHT_RETURN_RE = re.compile(
    rf"(?i)(?:return(?:ed|ing)?|arrived|landing)\s+"
    rf"(?:(?:on|via)\s+)?(?:[A-Za-z]+\s+Flight\s+\d+\s+)?"
    rf"(?:on\s+)?({DATE_TOKEN})"
    rf"|"
    rf"(?:Flight\s+\d+)\s+on\s+({DATE_TOKEN}).{{0,40}}?(?:return|arrived|landing)"
)

OFFICE_APPOINTMENT_RE = re.compile(
    rf"(?i)(?:at\s+my\s+(?:medical\s+)?office|seeing\s+patients|"
    rf"appointment\s+with|was\s+at\s+(?:the\s+)?(?:office|clinic))"
    rf".{{0,120}}?on\s+({DATE_TOKEN})"
    rf"(?:.{{0,40}}?(?:at\s+(?:approximately\s+)?)?(\d{{1,2}}:\d{{2}}\s*[ap]\.?m\.?))?"
)

CONTRACT_RATE_RE = re.compile(
    r"(?i)(?:(?:mortgage\s+)?note|promissory\s+note|contract).{0,120}?"
    r"(?:interest\s+(?:rate\s+)?(?:of\s+)?|rate\s+of)\s*"
    r"(\d{1,2}(?:\.\d{1,4})?)\s*%"
    r"|(?:interest\s+(?:rate\s+)?(?:of\s+)?|rate\s+of)\s*"
    r"(\d{1,2}(?:\.\d{1,4})?)\s*%"
    r".{0,80}?(?:per\s+annum|fixed)"
)

SOUGHT_RATE_RE = re.compile(
    r"(?i)(?:seeks?\s+interest|interest\s+(?:at|of)|judgment\s+.{0,40}?interest)"
    r"[^\d%]{0,40}(\d{1,2}(?:\.\d{1,4})?)\s*%"
)

EXECUTION_VENUE_RE = re.compile(
    r"(?i)(?:(?:state\s+of\s+new\s+york\s*,?\s*)?county\s+of\s+"
    r"(Suffolk|Kings|Queens|New\s+York|Bronx|Nassau|Richmond|Westchester)"
    r"|sworn\s+(?:to\s+)?(?:before\s+me\s+)?in\s+"
    r"(Suffolk|Kings|Queens|New\s+York|Bronx|Nassau)\s+County"
    r"|executed\s+in\s+(Suffolk|Kings|Queens|New\s+York|Bronx|Nassau)\s+County)"
)

PAYMENT_EVENT_CLAIM_RE = re.compile(
    rf"(?i)(?:failed\s+to\s+(?:make\s+)?(?:the\s+)?(?:payment|installment)|"
    rf"payment\s+(?:due|made)|wire\s+transfer|confirmation)"
    rf".{{0,80}}?({DATE_TOKEN})"
)

UTILITY_SERVICE_ADDRESS_RE = re.compile(
    r"(?i)(?:service\s+address|billing\s+address|account\s+address)\s*[:#]?\s*"
    r"([0-9]{1,6}(?:-[0-9]{1,4})?\s+[A-Za-z0-9 .'-]{2,60}(?:,?\s*[A-Za-z .'-]{2,40}){0,3})"
)


def extract_grounded_facts(text: str, limit: int = 40) -> list[dict[str, Any]]:
    """Pull entity-scoped documentary attributes from OCR text."""
    body = text or ""
    if not body.strip():
        return []
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(rec: dict[str, Any]) -> bool:
        entity = _entity_key(rec)
        token = (rec["key"], entity, str(rec.get("value")))
        if token in seen:
            return False
        seen.add(token)
        facts.append(rec)
        return len(facts) >= limit

    for match in EMPLOYED_SINCE_RE.finditer(body):
        parsed = _date(match.group(1))
        if not parsed:
            continue
        before = body[max(0, match.start() - 320) : match.start()]
        after = body[match.end() : match.end() + 160]
        clause = body[max(0, match.start() - 140) : match.start()]
        person = _nearest_person(clause) or _nearest_person(before)
        # Prefer after-window ("at TechCore…") over clause, which may start mid-word.
        employer = (
            _nearest_employer(after)
            or _nearest_employer(before)
            or _nearest_employer(clause)
        )
        rec = _graph_record(
            "employment_start",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            employer=employer,
            extra=parsed,
        )
        if _add(rec):
            return facts

    for match in POSITION_SINCE_RE.finditer(body):
        parsed = _date(match.group(2))
        if not parsed:
            continue
        before = body[max(0, match.start() - 320) : match.start()]
        after = body[match.end() : match.end() + 120]
        person = _nearest_person(before)
        employer = (
            _nearest_employer(before)
            or _nearest_employer(after)
            or _nearest_employer(body[max(0, match.start() - 120) : match.end()])
        )
        rec = _graph_record(
            "employment_start",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            employer=employer,
            extra=parsed,
        )
        if _add(rec):
            return facts

    for match in EMPLOYED_BY_SINCE_RE.finditer(body):
        parsed = _date(match.group(2))
        if not parsed:
            continue
        employer = match.group(1).strip().rstrip(".,")
        before = body[max(0, match.start() - 200) : match.start()]
        person = _nearest_person(before)
        rec = _graph_record(
            "employment_start",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            employer=employer,
            extra=parsed,
        )
        if _add(rec):
            return facts

    for match in PAYSTUB_HIRE_RE.finditer(body):
        raw = match.group(1)
        parsed = _date(raw)
        if not parsed:
            continue
        window = body[max(0, match.start() - 220) : match.end() + 120]
        # Prefer paystub context
        if not re.search(r"(?i)\b(pay\s*stub|earnings\s+statement|hire\s+date|employee\s+id)\b", window):
            # Still allow if explicit hire date phrase matched
            if not re.search(r"(?i)hire|start\s+date", match.group(0)):
                continue
        person = _nearest_person(window)
        employer = _nearest_employer(window)
        rec = _graph_record(
            "employment_start",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            employer=employer,
            extra=parsed,
        )
        if _add(rec):
            return facts

    for pattern in (W2_YEAR_RE, W2_YEAR_ALT_RE):
        for match in pattern.finditer(body):
            year = int(match.group(1))
            if year < 1990 or year > 2100:
                continue
            window = body[max(0, match.start() - 120) : match.end() + 200]
            person = _nearest_person(window)
            employer = _nearest_employer(window)
            rec = _graph_record(
                "employment_tax_year",
                year,
                str(year),
                match.group(0)[:220],
                person=person,
                employer=employer,
            )
            if _add(rec):
                return facts

    # Fallback: W-2 marker anywhere + explicit tax year on the page
    if W2_DOC_MARKER_RE.search(body):
        for match in TAX_YEAR_ONLY_RE.finditer(body):
            year = int(match.group(1))
            if year < 1990 or year > 2100:
                continue
            window = body[max(0, match.start() - 200) : match.end() + 200]
            person = _nearest_person(window) or _nearest_person(body[:400])
            employer = _nearest_employer(window) or _nearest_employer(body[:500])
            rec = _graph_record(
                "employment_tax_year",
                year,
                str(year),
                match.group(0)[:220],
                person=person,
                employer=employer,
            )
            if _add(rec):
                return facts

    # --- Person documentary attributes (identity / occupancy / income) ---
    for match in DOB_RE.finditer(body):
        parsed = _date(match.group(1))
        if not parsed:
            continue
        window = body[max(0, match.start() - 200) : match.end() + 80]
        person = _nearest_person(window) or _nearest_person(body[:300])
        rec = _graph_record(
            "date_of_birth",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            extra={**parsed, "deadline": True},
        )
        if _add(rec):
            return facts

    for pattern in (RESIDENCE_RE, RESIDENCE_INLINE_RE):
        for match in pattern.finditer(body):
            raw_addr = match.group(1).strip().rstrip(".,;")
            raw_addr = re.split(r"(?i)\b(?:continuously|since|for\s+\d+\s+years?)\b", raw_addr)[0].strip(" ,.")
            norm = _norm_address(raw_addr)
            if not norm or len(norm) < 6:
                continue
            window = body[max(0, match.start() - 220) : match.end() + 80]
            # Prefer person labeled earlier in the document when window is address-heavy
            person = None
            for pref in (
                re.search(
                    r"(?i)(?:borrower|defendant|deponent|customer|account\s+holder)\s*:\s*"
                    r"((?:[A-Z][a-z]+(?:[ \t]+[A-Z][a-z']+){1,2}))",
                    body[: match.start()] or body[:400],
                ),
                re.search(
                    r"\b((?:[A-Z][a-z]+(?:[ \t]+[A-Z][a-z']+){1,2}))\s+"
                    r"(?:states?|swears?|testifies|affirms?)\b",
                    body[: match.start()] or body[:400],
                ),
            ):
                if pref:
                    person = pref.group(1)
                    break
            if not person:
                person = (
                    _nearest_person(body[: match.start()])
                    or _nearest_person(body[:400])
                    or _nearest_person(window)
                )
            # Reject street-looking persons
            if person and re.search(
                r"(?i)\b(street|avenue|boulevard|road|drive|place|astoria|vernon|bedford)\b",
                person,
            ):
                person = _nearest_person(body[:400])
            rec = _graph_record(
                "residence_address",
                norm,
                raw_addr[:120],
                match.group(0)[:220],
                person=person,
            )
            if _add(rec):
                return facts

    for match in INCOME_APPROX_ANNUAL_RE.finditer(body):
        monthly = _parse_money(match.group(1))
        annual = None
        if match.group(2):
            annual = _parse_money(match.group(2))
            if annual is not None and annual < 1000:
                annual = annual * 1000.0  # "$90k"
        elif match.group(3):
            annual = _parse_money(match.group(3))
        if annual is None and monthly is not None:
            annual = monthly * 12.0
        if annual is None:
            continue
        window = body[max(0, match.start() - 200) : match.end() + 40]
        person = (
            _nearest_person(window)
            or _nearest_person(body[: match.start()])
            or _nearest_person(body[:300])
        )
        rec = _graph_record(
            "annual_income",
            round(annual, 2),
            f"${annual:,.0f}/yr",
            match.group(0)[:220],
            person=person,
            extra={"income_source": "monthly_stated"},
        )
        if _add(rec):
            return facts

    for match in INCOME_MONTHLY_RE.finditer(body):
        monthly = _parse_money(match.group(1))
        if monthly is None or monthly < 100:
            continue
        annual = monthly * 12.0
        window = body[max(0, match.start() - 200) : match.end() + 40]
        person = (
            _nearest_person(window)
            or _nearest_person(body[: match.start()])
            or _nearest_person(body[:300])
        )
        rec = _graph_record(
            "annual_income",
            round(annual, 2),
            f"${monthly:,.0f}/mo (~${annual:,.0f}/yr)",
            match.group(0)[:220],
            person=person,
            extra={"income_source": "monthly", "monthly": monthly},
        )
        if _add(rec):
            return facts

    for match in INCOME_ANNUAL_RE.finditer(body):
        annual = _parse_money(match.group(1))
        if annual is None or annual < 1000:
            continue
        window = body[max(0, match.start() - 200) : match.end() + 40]
        person = (
            _nearest_person(window)
            or _nearest_person(body[: match.start()])
            or _nearest_person(body[:300])
        )
        source = "agi" if re.search(r"(?i)\bAGI\b|adjusted\s+gross", match.group(0)) else "annual"
        rec = _graph_record(
            "annual_income",
            round(annual, 2),
            f"${annual:,.0f}/yr",
            match.group(0)[:220],
            person=person,
            extra={"income_source": source},
        )
        if _add(rec):
            return facts

    # Resolve process-server name early so AOS personal-service leads can also
    # stamp service_attempt (needed for AOS vs daily-log pairing).
    server_name = None
    m_server = PROCESS_SERVER_NAME_RE.search(body)
    if m_server:
        server_name = m_server.group(1).strip()
    if not server_name and re.search(
        r"(?i)affidavit\s+of\s+service|process\s+server|daily\s+log", body[:240]
    ):
        m_top = re.search(
            r"(?i)\bI,\s*((?:[A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3}))\b",
            body[:400],
        )
        if m_top:
            server_name = m_top.group(1).strip()

    # --- presence / personal service vs alibi (P001 / P002 / P008) ---
    for match in PERSONAL_SERVICE_DATE_LEAD_RE.finditer(body):
        parsed = _date(match.group(1))
        time_s = (match.group(2) or "").strip().lower()
        person = _clean_served_name(match.group(3))
        place = (match.group(4) or "").strip().rstrip(".,;")
        if not parsed or not person or not place:
            continue
        # Own AOS personal service is also a service_attempt for the server.
        if time_s:
            srec = _service_attempt_record(
                server_name,
                parsed["value"],
                parsed.get("display") or parsed["value"],
                time_s,
                place,
                "personal",
                match.group(0)[:220],
            )
            if _add(srec):
                return facts
        rec = _presence_record(
            person,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            place,
            "served_personally",
            match.group(0)[:220],
            impact="element",
        )
        if _add(rec):
            return facts

    for match in PERSONAL_SERVICE_SAME_DATE_RE.finditer(body):
        person = _clean_served_name(match.group(1))
        parsed = _date(match.group(2))
        place = (match.group(3) or "").strip().rstrip(".,;")
        if not parsed or not person or not place:
            continue
        rec = _presence_record(
            person,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            place,
            "served_personally",
            match.group(0)[:220],
            impact="element",
        )
        if _add(rec):
            return facts

    for match in FLIGHT_DEPART_RE.finditer(body):
        place = (match.group(1) or "").strip()
        parsed = _date(match.group(2))
        if not parsed or not place:
            continue
        window = body[max(0, match.start() - 400) : match.end() + 80]
        person = _nearest_person(window) or _nearest_person(body[:400])
        # Affidavit first-person: "I, Marcus Thompson"
        if not person:
            m_i = re.search(
                r"(?i)\bI,\s*([A-Z][a-z]+(?:\s+[A-Z][a-z']+){1,2})\b",
                body[: match.start()],
            )
            if m_i:
                person = m_i.group(1)
        rec = _presence_record(
            person,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            place,
            "departed_flight",
            match.group(0)[:220],
        )
        if _add(rec):
            return facts

    for match in OUT_OF_STATE_RE.finditer(body):
        raw_date = match.group(1) or match.group(2)
        parsed = _date(raw_date) if raw_date else None
        if not parsed:
            continue
        window = body[max(0, match.start() - 400) : match.end() + 40]
        person = _nearest_person(window) or _nearest_person(body[:400])
        if not person:
            m_i = re.search(
                r"(?i)\bI,\s*([A-Z][a-z]+(?:\s+[A-Z][a-z']+){1,2})\b",
                body[: match.start()],
            )
            if m_i:
                person = m_i.group(1)
        rec = _presence_record(
            person,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            "out of state",
            "out_of_state",
            match.group(0)[:220],
        )
        if _add(rec):
            return facts

    for match in CONFERENCE_RE.finditer(body):
        conf = (match.group(1) or "").strip()
        start_p = _date(match.group(2))
        end_p = _date(match.group(3))
        if not start_p:
            continue
        window = body[max(0, match.start() - 500) : match.end() + 80]
        # Prefer named traveler in nearby wife/spouse or first person
        person = None
        wife = WIFE_TRAVELING_RE.search(window) or WIFE_TRAVELING_RE.search(body)
        # Conference often attributed to the deponent; also Diane via wife clause
        person = _nearest_person(window) or _nearest_person(body[:400])
        place = conf
        # Infer city from nearby "Chicago" etc.
        city_m = re.search(
            r"(?i)\b(Chicago|Illinois|Orlando|Miami|Dallas|Houston|Atlanta|"
            r"Las Vegas|Denver|Seattle|Boston|Detroit|Phoenix)\b",
            window,
        )
        if city_m:
            place = f"{city_m.group(1)} ({conf})"
        dates = [start_p]
        if end_p and end_p.get("value") != start_p.get("value"):
            dates.append(end_p)
            # Also mid-range single-day: use start as the conflict day (service day)
        for parsed in dates[:1]:
            rec = _presence_record(
                person,
                parsed["value"],
                parsed.get("display") or parsed["value"],
                place,
                "conference",
                match.group(0)[:220],
            )
            if _add(rec):
                return facts
        # Explicit: for each day in range up to 4 days, stamp presence
        if start_p and end_p:
            try:
                from datetime import datetime, timedelta

                d0 = datetime.strptime(str(start_p["value"])[:10], "%Y-%m-%d").date()
                d1 = datetime.strptime(str(end_p["value"])[:10], "%Y-%m-%d").date()
                cur = d0
                n = 0
                while cur <= d1 and n < 5:
                    if cur != d0:  # start already added
                        rec = _presence_record(
                            person,
                            cur.isoformat(),
                            cur.isoformat(),
                            place,
                            "conference",
                            match.group(0)[:220],
                        )
                        if _add(rec):
                            return facts
                    cur = cur + timedelta(days=1)
                    n += 1
            except (ValueError, TypeError):
                pass

    for match in WIFE_TRAVELING_RE.finditer(body):
        person = (match.group(1) or "").strip()
        place = (match.group(2) or "").strip()
        parsed = _date(match.group(3))
        if not person or not place or not parsed:
            continue
        rec = _presence_record(
            person,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            place,
            "traveling",
            match.group(0)[:220],
        )
        if _add(rec):
            return facts

    for match in BANK_SPEND_RE.finditer(body):
        parsed = _date(match.group(1))
        place = (match.group(2) or "").strip().rstrip(".,;")
        if not parsed or not place:
            continue
        # Only keep spends that look remote (city/hotel), not local grocery noise
        region = _presence_region(place, "bank_spend")
        if region != "remote":
            continue
        window = body[max(0, match.start() - 500) : match.end()]
        person = _nearest_person(window) or _nearest_person(body[:400])
        if not person:
            m_i = re.search(
                r"(?i)\b(?:account\s+holder|cardholder|borrower)\s*:\s*"
                r"([A-Z][a-z]+(?:\s+[A-Z][a-z']+){1,2})",
                body,
            )
            if m_i:
                person = m_i.group(1)
            else:
                m_i = re.search(
                    r"(?i)\bI,\s*([A-Z][a-z]+(?:\s+[A-Z][a-z']+){1,2})\b",
                    body[: match.start()] or body,
                )
                if m_i:
                    person = m_i.group(1)
        rec = _presence_record(
            person,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            place,
            "bank_spend",
            match.group(0)[:220],
        )
        if _add(rec):
            return facts

    # --- principal decline (P003) ---
    arrears = bool(ARREARS_HINT_RE.search(body))
    for match in NOTE_PRINCIPAL_RE.finditer(body):
        amount = _parse_money(match.group(1))
        if amount is None or amount < 1000:
            continue
        # Skip judgment-style lines caught here
        if re.search(r"(?i)judgment|total\s+amount\s+due", match.group(0)):
            continue
        rec = _graph_record(
            "stated_principal",
            round(amount, 2),
            f"${amount:,.2f}",
            match.group(0)[:220],
            extra={
                "principal_role": "note",
                "arrears_context": arrears,
            },
        )
        if _add(rec):
            return facts

    # Judgment principal: prefer line "Principal: $..." in a judgment / amount-due block
    judgmentish = bool(
        re.search(r"(?i)judgment\s+of\s+foreclosure|total\s+amount\s+due|amount\s+due\s+as\s+of", body)
    )
    for match in JUDGMENT_PRINCIPAL_LINE_RE.finditer(body):
        amount = _parse_money(match.group(1))
        if amount is None or amount < 1000:
            continue
        if not judgmentish and not arrears:
            # Still allow if nearby block mentions judgment within 400 chars
            near = body[max(0, match.start() - 400) : match.end() + 80]
            if not re.search(r"(?i)judgment|amount\s+due|foreclosure", near):
                continue
        rec = _graph_record(
            "stated_principal",
            round(amount, 2),
            f"${amount:,.2f}",
            match.group(0)[:220],
            extra={
                "principal_role": "judgment",
                "arrears_context": arrears or bool(
                    ARREARS_HINT_RE.search(body[max(0, match.start() - 500) : match.end() + 200])
                ),
            },
        )
        if _add(rec):
            return facts

    # --- sale grantee (P006 only when distinct names) ---
    for match in SALE_GRANTEE_RE.finditer(body):
        name = (match.group(1) or "").strip().rstrip(".,;")
        name = re.sub(r"\s+", " ", name)
        if len(name) < 4:
            continue
        # Drop trailing "to vacate" / OSC chrome
        if re.search(r"(?i)\b(vacate|order|court|plaintiff|defendant)\b", name):
            continue
        rec = _graph_record(
            "sale_grantee",
            _norm_name(name),
            name,
            match.group(0)[:220],
            extra={"grantee_display": name},
        )
        if _add(rec):
            return facts

    # --- boarding pass (supports P0#1 alibi) ---
    for match in BOARDING_PASS_RE.finditer(body):
        place = (match.group(1) or match.group(4) or "").strip()
        raw_date = match.group(2) or match.group(3)
        parsed = _date(raw_date) if raw_date else None
        if not parsed:
            continue
        if not place:
            place = "boarding pass destination"
        window = body[max(0, match.start() - 400) : match.end() + 40]
        person = _nearest_person(window) or _nearest_person(body[:400])
        if not person:
            m_i = re.search(
                r"(?i)\b(?:passenger|name)\s*:\s*((?:[A-Z][a-z]+(?:\s+[A-Z][a-z']+){1,2}))",
                window,
            )
            if m_i:
                person = m_i.group(1)
        rec = _presence_record(
            person,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            place,
            "boarding_pass",
            match.group(0)[:220],
        )
        if _add(rec):
            return facts

    # --- process server service_attempt (P0#2) ---
    for match in SERVICE_PERSONAL_TIME_RE.finditer(body):
        parsed = _date(match.group(1)) if match.group(1) else None
        if not parsed:
            before = body[max(0, match.start() - 200) : match.start()]
            dm = re.search(rf"(?i)({DATE_TOKEN})", before)
            parsed = _date(dm.group(1)) if dm else None
        time_s = (match.group(2) or "").strip().lower()
        place = (match.group(4) or "").strip().rstrip(".,;")
        if not parsed or not place:
            continue
        rec = _service_attempt_record(
            server_name,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            time_s,
            place,
            "personal",
            match.group(0)[:220],
        )
        if _add(rec):
            return facts
        # Also stamp presence for the person served (alibi/office conflicts)
        served = _clean_served_name(match.group(3)) if match.lastindex and match.lastindex >= 3 else None
        if not served:
            # Prefer "upon NAME"; avoid (?i) so "served the Summons…" cannot capture.
            nm = re.search(
                r"\bupon\s+((?:[A-Z][A-Za-z'-]+\s+){1,2}[A-Z][A-Za-z'-]+)",
                match.group(0),
            ) or re.search(
                r"\b(?:personally\s+served|served)\s+"
                r"((?:[A-Z][A-Za-z'-]+\s+){1,2}[A-Z][A-Za-z'-]+)",
                match.group(0),
            )
            if nm:
                served = _clean_served_name(nm.group(1))
        if served:
            prec = _presence_record(
                served,
                parsed["value"],
                parsed.get("display") or parsed["value"],
                place,
                "served_personally",
                match.group(0)[:220],
                impact="element",
            )
            if _add(prec):
                return facts

    for match in NAIL_MAIL_RE.finditer(body):
        time_s = (match.group(1) or "").strip().lower()
        parsed = _date(match.group(2)) if match.group(2) else None
        place = ""
        if match.lastindex and match.lastindex >= 3 and match.group(3):
            place = match.group(3).strip().rstrip(".,;")
        if not parsed:
            before = body[max(0, match.start() - 240) : match.start()]
            dm = re.search(rf"(?i)on\s+({DATE_TOKEN})", before) or re.search(
                rf"(?i)({DATE_TOKEN})", before
            )
            parsed = _date(dm.group(1)) if dm else None
        if not parsed:
            continue
        if not place:
            pm = re.search(
                r"(?i)at\s+([0-9]{1,6}\s+[A-Za-z0-9 .,'-]{3,70})",
                body[match.start() : match.end() + 120],
            )
            place = pm.group(1).strip().rstrip(".,;") if pm else "property"
        rec = _service_attempt_record(
            server_name,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            time_s or "unspecified",
            place,
            "nail_mail",
            match.group(0)[:220],
        )
        if _add(rec):
            return facts

    log_date = None
    m_log_date = re.search(
        rf"(?i)(?:daily\s+log|service\s+log|log\s+for)\s*(?:dated\s+|of\s+)?({DATE_TOKEN})",
        body,
    )
    if m_log_date:
        log_date = _date(m_log_date.group(1))
    for match in SERVER_LOG_LINE_RE.finditer(body):
        parsed = _date(match.group(1)) if match.group(1) else log_date
        time_s = (match.group(2) or "").strip().lower()
        method_raw = (match.group(3) or "").strip().lower()
        place = (match.group(4) or "").strip().rstrip(".,;") if match.group(4) else ""
        if not parsed or not time_s or not place:
            continue
        if "nail" in method_raw or "mail" in method_raw or "post" in method_raw:
            method = "nail_mail"
        elif "personal" in method_raw:
            method = "personal"
        elif "substitut" in method_raw:
            method = "substitute"
        else:
            method = "attempt"
        on_date = parsed["value"] if isinstance(parsed, dict) else str(parsed)
        date_display = (parsed.get("display") if isinstance(parsed, dict) else None) or str(on_date)
        rec = _service_attempt_record(
            server_name,
            on_date,
            date_display,
            time_s,
            place,
            method,
            match.group(0)[:220],
        )
        if _add(rec):
            return facts

    # --- balance_on_date (P0#3) ---
    for match in BALANCE_ON_DATE_RE.finditer(body):
        if match.group(1) and match.group(2):
            amount = _parse_money(match.group(1))
            parsed = _date(match.group(2))
        else:
            parsed = _date(match.group(3)) if match.group(3) else None
            amount = _parse_money(match.group(4)) if match.group(4) else None
        if amount is None or amount < 1000 or not parsed:
            continue
        rec = _graph_record(
            "balance_on_date",
            round(amount, 2),
            f"${amount:,.2f} as of {parsed.get('display') or parsed['value']}",
            match.group(0)[:220],
            extra={"on_date": str(parsed["value"])[:10]},
        )
        if _add(rec):
            return facts

    for match in BALANCE_AS_OF_RE.finditer(body):
        amount = _parse_money(match.group(1))
        parsed = _date(match.group(2))
        if amount is None or amount < 1000 or not parsed:
            continue
        rec = _graph_record(
            "balance_on_date",
            round(amount, 2),
            f"${amount:,.2f} as of {parsed.get('display') or parsed['value']}",
            match.group(0)[:220],
            extra={"on_date": str(parsed["value"])[:10]},
        )
        if _add(rec):
            return facts

    # --- loan_payment_date (P0#4) ---
    for match in SERVICER_PAYMENT_RE.finditer(body):
        for gi in (1, 2):
            if not match.group(gi):
                continue
            parsed = _date(match.group(gi))
            if not parsed:
                continue
            rec = _graph_record(
                "loan_payment_date",
                str(parsed["value"])[:10],
                f"servicer credited {parsed.get('display') or parsed['value']}",
                match.group(0)[:220],
                extra={
                    "payment_role": "servicer_credit",
                    "on_date": str(parsed["value"])[:10],
                },
            )
            if _add(rec):
                return facts

    for match in SERVICER_PAYMENT_LINE_RE.finditer(body):
        parsed = _date(match.group(1))
        if not parsed:
            continue
        amount = _parse_money(match.group(2)) if match.lastindex and match.lastindex >= 2 else None
        rec = _graph_record(
            "loan_payment_date",
            str(parsed["value"])[:10],
            f"servicer credited {parsed.get('display') or parsed['value']}",
            match.group(0)[:220],
            extra={
                "payment_role": "servicer_credit",
                "on_date": str(parsed["value"])[:10],
                "payment_amount": amount,
            },
        )
        if _add(rec):
            return facts

    for match in BANK_DEBIT_RE.finditer(body):
        parsed = _date(match.group(1))
        amount = _parse_money(match.group(2))
        if not parsed:
            continue
        quote = match.group(0)
        near = body[max(0, match.start() - 80) : match.end() + 40]
        if not re.search(r"(?i)mortgage|loan|servicer|payment|ach|auto", quote + near):
            continue
        rec = _graph_record(
            "loan_payment_date",
            str(parsed["value"])[:10],
            f"bank debit {parsed.get('display') or parsed['value']}"
            + (f" ${amount:,.2f}" if amount else ""),
            quote[:220],
            extra={
                "payment_role": "bank_debit",
                "on_date": str(parsed["value"])[:10],
                "payment_amount": amount,
            },
        )
        if _add(rec):
            return facts

    # --- employer_of_record (P0#5) ---
    for match in WAY_TO_EMPLOYER_RE.finditer(body):
        emp = _clean_employer(match.group(1) or "")
        if not emp or not _employer_ok(emp):
            continue
        person = None
        m_def = re.search(
            r"(?i)(?:upon|served)\s+((?:[A-Z][A-Za-z'-]+\s+){1,3}[A-Z][A-Za-z'-]+)",
            body,
        )
        if m_def:
            person = _clean_served_name(m_def.group(1))
        if not person:
            m_def = re.search(
                r"(?i)(?:deponent|defendant|borrower|employee)\s*:\s*"
                r"((?:[A-Z][a-z]+(?:[ \t]+[A-Z][a-z']+){1,2}))",
                body,
            )
            if m_def:
                person = m_def.group(1)
        if not person:
            window = body[max(0, match.start() - 300) : match.end() + 40]
            person = _nearest_person(window) or _nearest_person(body[:400])
        rec = _graph_record(
            "employer_of_record",
            _norm_name(emp),
            emp,
            match.group(0)[:220],
            person=person,
            employer=emp,
            extra={"employer_source": "service_narrative"},
        )
        if _add(rec):
            return facts

    for match in EMPLOYER_VERIFICATION_RE.finditer(body):
        emp = _clean_employer(match.group(1) or "")
        if not emp or not _employer_ok(emp):
            continue
        person = None
        m_emp = re.search(
            r"(?i)(?:employee|borrower|deponent|defendant)\s*:\s*"
            r"((?:[A-Z][a-z]+(?:[ \t]+[A-Z][a-z']+){1,2}))",
            body,
        )
        if m_emp:
            person = m_emp.group(1)
        if not person:
            window = body[max(0, match.start() - 300) : match.end() + 80]
            person = (
                _nearest_person(window)
                or _nearest_person(body[:400])
                or _nearest_person(body[match.end() : match.end() + 200])
            )
        rec = _graph_record(
            "employer_of_record",
            _norm_name(emp),
            emp,
            match.group(0)[:220],
            person=person,
            employer=emp,
            extra={"employer_source": "employment_record"},
        )
        if _add(rec):
            return facts

    # --- substitute service (personal vs substitute same day) ---
    for match in SUBSTITUTE_SERVICE_RE.finditer(body):
        parsed = _date(match.group(1)) if match.group(1) else None
        if not parsed:
            before = body[max(0, match.start() - 240) : match.start()]
            dm = re.search(rf"(?i)on\s+({DATE_TOKEN})", before) or re.search(
                rf"(?i)({DATE_TOKEN})", before
            )
            parsed = _date(dm.group(1)) if dm else None
        time_s = (match.group(2) or "").strip().lower() or "unspecified"
        place = (match.group(3) or "").strip().rstrip(".,;") if match.group(3) else ""
        if not parsed:
            continue
        if not place:
            pm = re.search(
                r"(?i)at\s+([0-9]{1,6}\s+[A-Za-z0-9 .,'-]{3,70})",
                body[match.start() : match.end() + 120],
            )
            place = pm.group(1).strip().rstrip(".,;") if pm else "property"
        rec = _service_attempt_record(
            server_name,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            time_s,
            place,
            "substitute",
            match.group(0)[:220],
        )
        # Fix server_name - it's in extract scope
        if _add(rec):
            return facts

    # --- multi-day flight presence (depart + return stamps intervening days) ---
    departures = []
    for match in FLIGHT_DEPART_RE.finditer(body):
        place = (match.group(1) or "").strip()
        parsed = _date(match.group(2))
        if parsed:
            departures.append((parsed, place, match.group(0)[:220]))
    returns = []
    for match in FLIGHT_RETURN_RE.finditer(body):
        raw = match.group(1) or match.group(2)
        parsed = _date(raw) if raw else None
        if parsed:
            returns.append((parsed, match.group(0)[:220]))
    if departures and returns:
        from datetime import datetime, timedelta
        person = None
        m_i = re.search(
            r"(?i)\bI,\s*([A-Z][a-z]+(?:\s+[A-Z][a-z']+){1,2})\b",
            body[:500],
        )
        if m_i:
            person = m_i.group(1)
        if not person:
            person = _nearest_person(body[:400])
        for dep, place, quote in departures:
            for ret, rquote in returns:
                try:
                    d0 = datetime.strptime(str(dep["value"])[:10], "%Y-%m-%d").date()
                    d1 = datetime.strptime(str(ret["value"])[:10], "%Y-%m-%d").date()
                except (TypeError, ValueError):
                    continue
                if d1 <= d0 or (d1 - d0).days > 21:
                    continue
                cur = d0
                n = 0
                while cur <= d1 and n < 16:
                    rec = _presence_record(
                        person,
                        cur.isoformat(),
                        cur.isoformat(),
                        place or "out of state",
                        "trip_away",
                        quote[:120] + " / " + rquote[:80],
                    )
                    if _add(rec):
                        return facts
                    cur = cur + timedelta(days=1)
                    n += 1

    # --- office appointment presence ---
    for match in OFFICE_APPOINTMENT_RE.finditer(body):
        raw = match.group(1)
        parsed = _date(raw) if raw else None
        if not parsed:
            continue
        time_s = ""
        if match.lastindex and match.lastindex >= 2 and match.group(2):
            time_s = match.group(2)
        window = body[max(0, match.start() - 400) : match.end() + 40]
        person = _nearest_person(window) or _nearest_person(body[:400])
        if not person:
            m_i = re.search(
                r"(?i)\bI,\s*([A-Z][a-z]+(?:\s+[A-Z][a-z']+){1,2})\b",
                body[: match.start()],
            )
            if m_i:
                person = m_i.group(1)
        place = "medical office"
        am = re.search(
            r"(?i)(?:office|clinic|practice)\s+at\s+([0-9]{1,6}\s+[A-Za-z0-9 .,'-]{3,60})",
            window,
        )
        if am:
            place = am.group(1).strip().rstrip(".,;")
        rec = _presence_record(
            person,
            parsed["value"],
            parsed.get("display") or parsed["value"],
            place,
            "office_appointment",
            match.group(0)[:220],
        )
        if _add(rec):
            return facts

    # --- utility service address as residence ---
    for match in UTILITY_SERVICE_ADDRESS_RE.finditer(body):
        addr = (match.group(1) or "").strip().rstrip(".,;")
        if len(addr) < 8:
            continue
        window = body[max(0, match.start() - 200) : match.end() + 40]
        person = _nearest_person(window) or _nearest_person(body[:300])
        # customer name on utility
        cm = re.search(
            r"(?i)(?:customer|account\s+holder|service\s+for)\s*:\s*"
            r"((?:[A-Z][a-z]+(?:[ \t]+[A-Z][a-z']+){1,2}))",
            body,
        )
        if cm:
            person = cm.group(1)
        rec = _graph_record(
            "residence_address",
            _norm_address(addr),
            addr,
            match.group(0)[:220],
            person=person,
            extra={"address_source": "utility"},
        )
        if _add(rec):
            return facts

    # --- stated interest rate note vs sought ---
    for match in CONTRACT_RATE_RE.finditer(body):
        raw = match.group(1) or match.group(2)
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            continue
        if rate < 0.5 or rate > 25:
            continue
        # Prefer note context
        near = body[max(0, match.start() - 80) : match.end() + 40]
        if not re.search(r"(?i)note|promissory|contract|mortgage|per\s+annum|fixed", near):
            continue
        rec = _graph_record(
            "stated_interest_rate",
            rate,
            f"{rate}%",
            match.group(0)[:220],
            extra={"rate_role": "note"},
        )
        if _add(rec):
            return facts

    for match in SOUGHT_RATE_RE.finditer(body):
        try:
            rate = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if rate < 0.5 or rate > 25:
            continue
        near = body[max(0, match.start() - 60) : match.end() + 40]
        if not re.search(r"(?i)seek|judgment|motion|plaintiff|per\s+annum", near):
            continue
        # Skip if clearly "note rate" restatement
        if re.search(r"(?i)\bnote\b", near) and not re.search(r"(?i)seek|judgment|motion", near):
            continue
        rec = _graph_record(
            "stated_interest_rate",
            rate,
            f"{rate}%",
            match.group(0)[:220],
            extra={"rate_role": "sought"},
        )
        if _add(rec):
            return facts

    # --- payment event claims (plaintiff / defendant / bank) ---
    for match in PAYMENT_EVENT_CLAIM_RE.finditer(body):
        parsed = _date(match.group(1))
        if not parsed:
            continue
        quote = match.group(0)[:220]
        low = quote.lower()
        if "failed" in low or "due" in low:
            role = "plaintiff_due"
        elif "wire" in low or "confirmation" in low or "bank" in low:
            role = "bank_debit"
        elif "made" in low or "paid" in low:
            role = "defendant_paid"
        else:
            role = "payment_claim"
        # Month key for clustering same installment
        month_key = str(parsed["value"])[:7]
        rec = _graph_record(
            "loan_payment_date",
            str(parsed["value"])[:10],
            f"{role} {parsed.get('display') or parsed['value']}",
            quote,
            extra={
                "payment_role": role,
                "on_date": str(parsed["value"])[:10],
                "payment_month": month_key,
            },
        )
        if _add(rec):
            return facts

    # --- execution venue ---
    for match in EXECUTION_VENUE_RE.finditer(body):
        county = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if not county:
            continue
        # Date near jurat / affidavit
        before = body[max(0, match.start() - 200) : match.end() + 120]
        dm = re.search(rf"(?i)(?:sworn|executed|dated|on)\s+({DATE_TOKEN})", before)
        parsed = _date(dm.group(1)) if dm else None
        if not parsed:
            dm = re.search(rf"(?i)({DATE_TOKEN})", before)
            parsed = _date(dm.group(1)) if dm else None
        if not parsed:
            continue
        server = None
        m_server = PROCESS_SERVER_NAME_RE.search(body) or re.search(
            r"(?i)\bI,\s*((?:[A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3}))\b",
            body[:400],
        )
        if m_server:
            server = m_server.group(1).strip()
        rec = _graph_record(
            "execution_venue",
            _norm_name(county),
            f"{county} County",
            match.group(0)[:220],
            person=server,
            extra={"on_date": str(parsed["value"])[:10], "venue_county": county},
        )
        if _add(rec):
            return facts

    return facts


def _clean_served_name(raw: str | None) -> str | None:
    if not raw:
        return None
    name = re.sub(r"\s+", " ", raw).strip(" ,.")
    # Trim trailing "by delivering" / "personally" fragments if capture overran
    name = re.split(r"(?i)\bby\b|\bwith\b|\bat\b|\bpersonally\b", name)[0].strip(" ,.")
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return None
    if any(p.lower() in PERSON_STOP for p in parts):
        return None
    # Title-case normalize ALL CAPS AOS names
    return " ".join(p[:1].upper() + p[1:].lower() if p.isupper() else p for p in parts)


def _service_attempt_record(
    server: str | None,
    on_date: str,
    date_display: str,
    time_s: str,
    place: str,
    method: str,
    quote: str,
) -> dict[str, Any]:
    value = f"{method}|{_norm_time(time_s)}|{_norm_address(place)}"
    display = f"{server or 'process server'}: {method} at {time_s} @ {place} on {date_display}"
    return _graph_record(
        "service_attempt",
        value,
        display,
        quote,
        person=server,
        extra={
            "on_date": str(on_date)[:10],
            "service_time": _norm_time(time_s),
            "service_place": place,
            "service_method": method,
            "impact": "credibility",
        },
    )


def _norm_time(raw: str) -> str:
    m = re.search(r"(?i)(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", raw or "")
    if not m:
        return (raw or "").strip().lower()
    hour = int(m.group(1))
    minute = int(m.group(2))
    ap = m.group(3).lower()
    if ap == "p" and hour != 12:
        hour += 12
    if ap == "a" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _minutes_apart(a: str, b: str) -> int | None:
    def _parts(t: str) -> tuple[int, int] | None:
        m = re.match(r"^(\d{2}):(\d{2})$", t or "")
        if not m:
            return None
        return int(m.group(1)), int(m.group(2))

    pa, pb = _parts(a), _parts(b)
    if not pa or not pb:
        return None
    return abs((pa[0] * 60 + pa[1]) - (pb[0] * 60 + pb[1]))


def _presence_region(place: str, status: str) -> str:
    low = (place or "").lower()
    if status in {"departed_flight", "boarding_pass", "trip_away"} or any(
        tok in low for tok in AIRPORT_DEPART_TOKENS
    ):
        return "departed"
    if status == "office_appointment":
        return "local_other"
    if status in {"out_of_state", "traveling", "conference", "bank_spend"} or any(
        tok in low for tok in REMOTE_PLACE_TOKENS
    ):
        return "remote"
    if status == "served_personally" or any(tok in low for tok in NYC_PLACE_TOKENS):
        return "local_serve"
    if any(tok in low for tok in NYC_PLACE_TOKENS):
        return "local_serve"
    return "other"


def _presence_record(
    person: str | None,
    on_date: str,
    date_display: str,
    place: str,
    status: str,
    quote: str,
    *,
    impact: str | None = None,
) -> dict[str, Any]:
    region = _presence_region(place, status)
    display = f"{person or 'person'} @ {place} on {date_display} ({status})"
    value = f"{region}|{status}|{_norm_name(place)[:40]}"
    extra = {
        "on_date": str(on_date)[:10],
        "place": place,
        "presence_status": status,
        "presence_region": region,
        "impact": impact or GRAPH_ATTRIBUTES["presence_on_date"].get("impact"),
    }
    return _graph_record(
        "presence_on_date",
        value,
        display,
        quote,
        person=person,
        extra=extra,
    )


def cluster_key(fact: dict[str, Any]) -> str:
    """Same nameable fact identity for conflict clustering."""
    key = str(fact.get("key") or "")
    entity = _entity_key(fact)
    if key == "presence_on_date":
        on_date = str(fact.get("on_date") or "")[:10]
        person = _norm_name(str(fact.get("person") or "")) or "_"
        return f"{key}::{person}|{on_date}"
    if key == "service_attempt":
        on_date = str(fact.get("on_date") or "")[:10]
        server = _norm_name(str(fact.get("person") or "")) or "server"
        return f"{key}::{server}|{on_date}"
    if key == "balance_on_date":
        on_date = str(fact.get("on_date") or "")[:10]
        return f"{key}::{on_date or 'loan'}"
    if key == "loan_payment_date":
        return "loan_payment_date::loan"
    if key == "stated_interest_rate":
        return "stated_interest_rate::loan"
    if key == "execution_venue":
        on_date = str(fact.get("on_date") or "")[:10]
        server = _norm_name(str(fact.get("person") or "")) or "server"
        return f"{key}::{server}|{on_date}"
    if key == "employer_of_record":
        person = _norm_name(str(fact.get("person") or "")) or "_"
        return f"{key}::{person}"
    if key == "stated_principal":
        return "stated_principal::loan"
    if key == "sale_grantee":
        return "sale_grantee::sale"
    if entity and entity != "_":
        return f"{key}::{entity}"
    return key


def _entity_key(fact: dict[str, Any]) -> str:
    if fact.get("key") == "presence_on_date":
        person = _norm_name(str(fact.get("person") or ""))
        on_date = str(fact.get("on_date") or "")[:10]
        if person and on_date:
            return f"{person}|{on_date}"
        if person:
            return person
        return f"_|{on_date}" if on_date else "_"
    person = _norm_name(str(fact.get("person") or ""))
    employer = _norm_name(str(fact.get("employer") or ""))
    if person and employer:
        return f"{person}|{employer}"
    if person:
        return person
    if employer:
        return f"|{employer}"
    return "_"


def entity_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when both facts can describe the same person/employer (soft match).

    Person-only and person|employer records for the same person merge; conflicting
    employers or persons do not. Unscoped facts only merge with other unscoped facts.
    """
    if left.get("key") == "presence_on_date" or right.get("key") == "presence_on_date":
        if left.get("key") != right.get("key"):
            return False
        pa = _norm_name(str(left.get("person") or ""))
        pb = _norm_name(str(right.get("person") or ""))
        da = str(left.get("on_date") or "")[:10]
        db = str(right.get("on_date") or "")[:10]
        if not pa or not pb or pa != pb:
            return False
        return bool(da and db and da == db)
    if left.get("key") == "service_attempt" and right.get("key") == "service_attempt":
        da = str(left.get("on_date") or "")[:10]
        db = str(right.get("on_date") or "")[:10]
        if not da or da != db:
            return False
        pa = _norm_name(str(left.get("person") or ""))
        pb = _norm_name(str(right.get("person") or ""))
        # Same server when both named; allow unscoped to merge with named on same date
        if pa and pb and pa != pb:
            return False
        return True
    if left.get("key") == "balance_on_date" and right.get("key") == "balance_on_date":
        return str(left.get("on_date") or "")[:10] == str(right.get("on_date") or "")[:10]
    if left.get("key") == "loan_payment_date" and right.get("key") == "loan_payment_date":
        return True
    if left.get("key") == "stated_interest_rate" and right.get("key") == "stated_interest_rate":
        return True
    if left.get("key") == "execution_venue" and right.get("key") == "execution_venue":
        da = str(left.get("on_date") or "")[:10]
        db = str(right.get("on_date") or "")[:10]
        if not da or da != db:
            return False
        pa = _norm_name(str(left.get("person") or ""))
        pb = _norm_name(str(right.get("person") or ""))
        if pa and pb and pa != pb:
            return False
        return True
    if left.get("key") == "employer_of_record" and right.get("key") == "employer_of_record":
        pa = _norm_name(str(left.get("person") or ""))
        pb = _norm_name(str(right.get("person") or ""))
        if pa and pb:
            return pa == pb
        return False
    if left.get("key") == "stated_principal" and right.get("key") == "stated_principal":
        return True
    if left.get("key") == "sale_grantee" and right.get("key") == "sale_grantee":
        return True
    pa = _norm_name(str(left.get("person") or ""))
    pb = _norm_name(str(right.get("person") or ""))
    ea = _norm_name(str(left.get("employer") or ""))
    eb = _norm_name(str(right.get("employer") or ""))
    if pa and pb and pa != pb:
        return False
    if ea and eb and ea != eb:
        return False
    if pa and pb and pa == pb:
        return True
    if ea and eb and ea == eb:
        return True
    if not pa and not pb and not ea and not eb:
        return True
    # One side fully unscoped — do not glue onto a named entity (too noisy).
    return False


def same_nameable_fact(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Same attribute (or hire-vs-W2 cross-attribute) for a compatible entity."""
    keys = {left.get("key"), right.get("key")}
    if left.get("key") == right.get("key"):
        return entity_compatible(left, right)
    if keys == {"employment_start", "employment_tax_year"}:
        return entity_compatible(left, right)
    return False


def facts_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return False when two grounded facts for the same cluster cannot both be true."""
    if not same_nameable_fact(left, right):
        # Strict cluster_key equality still used for non-graph / exact keys
        if cluster_key(left) != cluster_key(right):
            return True
        if left.get("key") != right.get("key"):
            return True
    key = left.get("key")
    if key != right.get("key"):
        # Cross-attribute documentary rule: W-2 tax year vs later hire date
        if {left.get("key"), right.get("key")} == {"employment_start", "employment_tax_year"}:
            return not _hire_after_w2_year(left, right)
        return True
    if key == "employment_start":
        return _same_date_value(left, right)
    if key == "date_of_birth":
        # Any day/year mismatch is identity BS — not soft same-month.
        return str(left.get("value")) == str(right.get("value"))
    if key == "employment_tax_year":
        return str(left.get("value")) == str(right.get("value"))
    if key == "residence_address":
        return _addresses_compatible(left.get("value"), right.get("value"))
    if key == "annual_income":
        return _income_compatible(left, right)
    if key == "presence_on_date":
        return _presence_compatible(left, right)
    if key == "service_attempt":
        return _service_attempt_compatible(left, right)
    if key == "balance_on_date":
        return _balance_on_date_compatible(left, right)
    if key == "loan_payment_date":
        return _payment_dates_compatible(left, right)
    if key == "stated_interest_rate":
        return _rate_compatible(left, right)
    if key == "execution_venue":
        return _venue_compatible(left, right)
    if key == "stated_principal":
        return _principal_compatible(left, right)
    if key == "sale_grantee":
        return _norm_name(str(left.get("value") or "")) == _norm_name(str(right.get("value") or ""))
    if key == "employer_of_record":
        if _norm_name(str(left.get("value") or "")) == _norm_name(str(right.get("value") or "")):
            return True
        sources = {str(left.get("employer_source") or ""), str(right.get("employer_source") or "")}
        # P0#5: only interrupt when service narrative collides with an employment record.
        # Two employment extracts with different employers is a near-miss / multi-job — suppress.
        if "service_narrative" in sources and (
            "employment_record" in sources or sources - {"service_narrative", ""}
        ):
            return False
        if sources == {"service_narrative"}:
            return False
        return True
    return str(left.get("value")) == str(right.get("value"))


def _hire_after_w2_year(a: dict[str, Any], b: dict[str, Any]) -> bool:
    start = a if a.get("key") == "employment_start" else b
    tax = b if a.get("key") == "employment_start" else a
    try:
        year = int(tax.get("value"))
    except (TypeError, ValueError):
        return False
    start_year = start.get("year")
    if start_year is None:
        try:
            start_year = int(str(start.get("value"))[:4])
        except (TypeError, ValueError):
            return False
    # A W-2 for year Y asserts wages that year; hire date after Y cannot both be true
    # for continuous employment at that employer.
    return int(start_year) > year


def _same_date_value(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("value") == b.get("value"):
        return True
    # Same month-key with different day is soft-compatible for non-deadline dates
    if a.get("month_key") and a.get("month_key") == b.get("month_key"):
        return True
    return False


def _addresses_compatible(left: Any, right: Any) -> bool:
    a = _norm_address(str(left or ""))
    b = _norm_address(str(right or ""))
    if not a or not b:
        return True
    if a == b:
        return True
    # Same street number + street token → compatible even if city OCR differs slightly
    a_parts = a.split()
    b_parts = b.split()
    if len(a_parts) >= 2 and len(b_parts) >= 2 and a_parts[0] == b_parts[0] and a_parts[1] == b_parts[1]:
        # Different city/state after the street → conflict
        if len(a_parts) >= 3 and len(b_parts) >= 3 and a_parts[2:] != b_parts[2:]:
            # Queens vs San Francisco style — conflict when remaining tokens disjoint
            return bool(set(a_parts[2:]) & set(b_parts[2:]))
        return True
    return False


def _income_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        a = float(left.get("value"))
        b = float(right.get("value"))
    except (TypeError, ValueError):
        return True
    delta = abs(a - b)
    baseline = max(a, b, 1.0)
    meta = GRAPH_ATTRIBUTES.get("annual_income") or {}
    return not (delta > float(meta.get("abs") or 8000) or (delta / baseline) > float(meta.get("pct") or 0.20))


def _presence_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """False when same person+date cannot be both personally served locally and remote/departed."""
    ra = str(left.get("presence_region") or "")
    rb = str(right.get("presence_region") or "")
    sa = str(left.get("presence_status") or "")
    sb = str(right.get("presence_status") or "")
    if {sa, sb} & {"served_personally"} and {sa, sb} & {
        "departed_flight",
        "boarding_pass",
        "trip_away",
        "office_appointment",
    }:
        return False
    if not ra or not rb or ra == rb:
        if {sa, sb} == {"served_personally", "departed_flight"}:
            return False
        return True
    pair = frozenset({ra, rb})
    if pair == frozenset({"departed", "remote"}):
        return True  # flight + Chicago spend agree
    if pair in {
        frozenset({"local_serve", "remote"}),
        frozenset({"local_serve", "departed"}),
        frozenset({"local_serve", "local_other"}),
    }:
        return False
    return True


def _service_attempt_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """False when AOS personal service collides with same-day log / substitute / other place."""
    ma = str(left.get("service_method") or "")
    mb = str(right.get("service_method") or "")
    pa = _norm_address(str(left.get("service_place") or ""))
    pb = _norm_address(str(right.get("service_place") or ""))
    ta = str(left.get("service_time") or "")
    tb = str(right.get("service_time") or "")
    methods = {ma, mb}
    # Personal vs nail-mail or substitute on same date is AGAINST
    if methods == {"personal", "nail_mail"} or methods == {"personal", "substitute"}:
        return False
    if "personal" in methods and "substitute" in methods:
        return False
    # Same place, different clock times far apart for claimed personal service vs log
    if pa and pb and pa == pb and ma == mb == "personal":
        mins = _minutes_apart(ta, tb)
        if mins is not None and mins >= 60:
            return False  # e.g. AOS 2:30 vs log 9:47 same address
    # Incompatible places near the claimed time (incl. 3-minute borough hop)
    if pa and pb and pa != pb:
        mins = _minutes_apart(ta, tb)
        if mins is not None and mins <= 45:
            return False
        if "personal" in methods and mins is not None and mins <= 120:
            return False
    return True


def _balance_on_date_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        a = float(left.get("value"))
        b = float(right.get("value"))
    except (TypeError, ValueError):
        return True
    if str(left.get("on_date") or "")[:10] != str(right.get("on_date") or "")[:10]:
        return True
    delta = abs(a - b)
    baseline = max(a, b, 1.0)
    meta = GRAPH_ATTRIBUTES.get("balance_on_date") or {}
    return not (delta > float(meta.get("abs") or 1000) or (delta / baseline) > float(meta.get("pct") or 0.005))


def _payment_dates_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """False when payment roles disagree on the date of the same installment event."""
    role_a = str(left.get("payment_role") or "")
    role_b = str(right.get("payment_role") or "")
    if role_a == role_b:
        return True
    try:
        from datetime import datetime

        da = datetime.strptime(str(left.get("value"))[:10], "%Y-%m-%d")
        db = datetime.strptime(str(right.get("value"))[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return True
    days = abs((da - db).days)
    # Same calendar month / payment cycle cluster
    month_a = str(left.get("payment_month") or str(left.get("value"))[:7])
    month_b = str(right.get("payment_month") or str(right.get("value"))[:7])
    same_cycle = month_a == month_b or days <= 14

    bankish = {"bank_debit", "servicer_credit"}
    claimish = {"plaintiff_due", "defendant_paid", "payment_claim", "servicer_credit", "bank_debit"}
    if role_a in claimish and role_b in claimish and role_a != role_b:
        if {role_a, role_b} <= bankish or same_cycle:
            amt_a = left.get("payment_amount")
            amt_b = right.get("payment_amount")
            if amt_a is not None and amt_b is not None:
                try:
                    if abs(float(amt_a) - float(amt_b)) > max(
                        50.0, 0.05 * max(float(amt_a), float(amt_b))
                    ):
                        return True
                except (TypeError, ValueError):
                    pass
            if same_cycle:
                return days <= 1  # Feb 1 vs Jan 30 vs Jan 29 → conflict
    # Legacy servicer vs bank pair
    if {role_a, role_b} == {"servicer_credit", "bank_debit"}:
        if same_cycle:
            return days <= 3
    return True


def _rate_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """False when note/contract rate differs from sought/judgment rate."""
    try:
        a = float(left.get("value"))
        b = float(right.get("value"))
    except (TypeError, ValueError):
        return True
    role_a = str(left.get("rate_role") or "")
    role_b = str(right.get("rate_role") or "")
    if role_a == role_b:
        return abs(a - b) <= 0.05
    if {role_a, role_b} == {"note", "sought"}:
        return abs(a - b) <= float((GRAPH_ATTRIBUTES.get("stated_interest_rate") or {}).get("abs") or 0.10)
    return True


def _venue_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _norm_name(str(left.get("value") or "")) == _norm_name(str(right.get("value") or ""))


def _principal_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """False when judgment principal is materially lower than note principal with arrears and no payments."""
    try:
        a = float(left.get("value"))
        b = float(right.get("value"))
    except (TypeError, ValueError):
        return True
    role_a = str(left.get("principal_role") or "")
    role_b = str(right.get("principal_role") or "")
    if role_a == role_b:
        # Same role — ordinary money compare with thresholds
        delta = abs(a - b)
        baseline = max(a, b, 1.0)
        meta = GRAPH_ATTRIBUTES.get("stated_principal") or {}
        return not (delta > float(meta.get("abs") or 1000) or (delta / baseline) > float(meta.get("pct") or 0.005))
    # note vs judgment: only interrupt on a *decline* into judgment with arrears context
    if {role_a, role_b} != {"note", "judgment"}:
        return True
    note_v = a if role_a == "note" else b
    judg_v = b if role_a == "note" else a
    if judg_v >= note_v:
        return True  # increase / same is ordinary loan movement — suppress
    delta = note_v - judg_v
    baseline = max(note_v, 1.0)
    meta = GRAPH_ATTRIBUTES.get("stated_principal") or {}
    material = delta > float(meta.get("abs") or 1000) or (delta / baseline) > float(meta.get("pct") or 0.005)
    arrears = bool(left.get("arrears_context") or right.get("arrears_context"))
    if material and arrears:
        return False
    return True


def _parse_money(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _norm_address(value: str) -> str:
    n = re.sub(r"[#,]", " ", value or "")
    n = re.sub(r"\s+", " ", n).strip().lower()
    n = re.sub(
        r"\b(street|st|avenue|ave|boulevard|blvd|road|rd|drive|dr|lane|ln|court|ct|"
        r"place|pl|apartment|apt|unit|suite|ste|continuously|since)\b\.?",
        "",
        n,
    )
    n = re.sub(r"\s+", " ", n).strip()
    # Drop trailing years that rode along ("since 2018")
    n = re.sub(r"\b(19|20)\d{2}\b", "", n).strip()
    return re.sub(r"\s+", " ", n).strip()


def _graph_record(
    key: str,
    value: Any,
    display: str,
    quote: str,
    *,
    person: str | None = None,
    employer: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = GRAPH_ATTRIBUTES[key]
    rec = {
        "key": key,
        "label": meta["label"],
        "kind": meta["kind"],
        "value": value,
        "display": display,
        "quote": re.sub(r"\s+", " ", quote).strip()[:220],
        "documentary": True,
        "graph": True,
        "person": person,
        "employer": employer,
        "entity_key": None,
    }
    if extra:
        rec.update({k: v for k, v in extra.items() if k not in rec})
    rec["entity_key"] = _entity_key(rec)
    return rec


def _nearest_person(window: str) -> str | None:
    def _ok(name: str) -> bool:
        parts = name.split()
        if len(parts) < 2:
            return False
        return not any(p.lower().strip(".") in PERSON_STOP for p in parts)

    labeled: list[str] = []
    for match in EMPLOYEE_LABEL_RE.finditer(window or ""):
        labeled.append(match.group(1).strip())
    for match in re.finditer(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3})\s*:",
        window or "",
    ):
        labeled.append(match.group(1).strip())
    for match in PERSON_NEAR_RE.finditer(window or ""):
        labeled.append(match.group(1).strip())

    good_labeled = [c for c in labeled if _ok(c)]
    if good_labeled:
        return good_labeled[-1]

    # Fallback Title Case pairs — last resort only
    loose = re.findall(r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b", window or "")
    good_loose = [c for c in loose if _ok(c)]
    if good_loose:
        return good_loose[0]
    return None


def _employer_ok(name: str) -> bool:
    n = (name or "").strip().rstrip(".,")
    if len(n) < 3 or len(n) > 80:
        return False
    if EMPLOYER_NOISE.search(n):
        return False
    # Reject mid-word window chops like "oyed by TechCore Solutions"
    if n[:1].islower():
        return False
    if re.search(r"(?i)\b(has|been|was|were|since|employee|borrower|defendant)\b", n):
        return False
    return True


def _clean_employer(name: str) -> str:
    n = re.sub(r"[\r\n]+", " ", name or "")
    n = re.sub(r"\s+", " ", n).strip().rstrip(".,;")
    # Cut trailing person / since clauses that rode along
    n = re.split(
        r"(?i)\s+since\b|\s+from\b|\s+located\b|\s+has\b|\s+employee\b",
        n,
    )[0].strip()
    return n


def _nearest_employer(window: str) -> str | None:
    label = EMPLOYER_LABEL_RE.search(window or "")
    if label:
        cand = _clean_employer(label.group(1))
        if _employer_ok(cand):
            return cand
    match = EMPLOYER_NEAR_RE.search(window or "")
    if match:
        cand = _clean_employer(match.group("preposition") or match.group("title") or "")
        if _employer_ok(cand):
            return cand
    loose = re.search(
        r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,3}\s+"
        r"(?:Solutions|Systems|Corporation|Company|Center|Hospital|Inc|LLC))\b",
        window or "",
    )
    if loose:
        cand = _clean_employer(loose.group(1))
        if _employer_ok(cand):
            return cand
    return None


def _norm_name(value: str) -> str:
    n = re.sub(r"[.,]", "", value or "")
    n = re.sub(r"\s+", " ", n).strip().lower()
    n = re.sub(
        r"\b(inc|llc|llp|corp|corporation|company|co|the)\b",
        "",
        n,
    )
    return re.sub(r"\s+", " ", n).strip()
