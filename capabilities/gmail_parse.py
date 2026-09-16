"""Email rules — every pattern and every parse, with nothing underneath them.

Pure functions. No network, no database, no environment. That is not tidiness:
it is what lets test_gmail.py assert the whole classification surface offline,
which is the only way these rules ever get exercised on the four rejection
templates that matter before a real one arrives.

Two lanes, and the ordering between them is the router's whole job:

    Lane A  card BILL PAYMENTS   -> a settlement, kind='cc_payment'
    Lane B  job applications     -> receipt vs rejection

Lane A is NOT expense capture. A bank's "you spent $42 at X" swipe alert is a
different thing, still blocked on E1 in PLAN-EXPENSES.md, and nothing here
touches it.

No model is called from this file, or from anything that imports it.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

# --- normalisation ----------------------------------------------------------
#
# Every pattern below matches against _norm() output, which is the entire
# reason the patterns are readable. Casing, curly quotes, non-breaking spaces
# and ragged line wrapping are handled once, here, instead of being smeared
# across forty regexes as [\s ]+ and ['’].

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"',
                         "”": '"', " ": " ", "–": "-",
                         "—": "-"})


def norm(text: str | None) -> str:
    """Lowercase, straighten quotes, collapse whitespace. Idempotent."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.translate(_QUOTES).lower()).strip()


def _any(patterns, *texts: str) -> bool:
    return any(p.search(t) for p in patterns for t in texts if t)


def _compile(*sources: str) -> tuple:
    return tuple(re.compile(s) for s in sources)


# --- Lane A · payment confirmations -----------------------------------------
#
# A synonym set, not one exact string. These are matched against the SUBJECT
# only: a payment confirmation announces itself in the subject line, and
# scanning bodies for them would catch every "thank you for your payment"
# footer on an unrelated receipt.

PAYMENT_SUBJECTS = _compile(
    r"we(?:'ve| have)? received your payment",
    r"thank you for (?:your |the )?payment",
    r"payment (?:has been |was )?received",
    r"(?:your )?payment confirmation",
    r"payment (?:posted|processed|successful|complete)",
    r"your payment to .+ (?:was|has been) (?:received|processed)",
)

# $1,234.56 or USD 1234.56. The cents are REQUIRED, and that is what keeps this
# off account numbers, reference ids and dates — all of which are bare digits.
_AMOUNT = re.compile(r"(?:\$|usd\s*)\s*([\d,]+\.\d{2})")

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")
_MON = "|".join(_MONTHS)

_DATES = (
    (re.compile(rf"\b({_MON})[a-z]* (\d{{1,2}}),? (\d{{4}})"), "mdy"),
    (re.compile(rf"\b(\d{{1,2}}) ({_MON})[a-z]* (\d{{4}})"), "dmy"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "slash"),
)


def _find_date(text: str) -> date | None:
    for pattern, shape in _DATES:
        m = pattern.search(text)
        if not m:
            continue
        try:
            if shape == "mdy":
                return date(int(m[3]), _MONTHS.index(m[1]) + 1, int(m[2]))
            if shape == "dmy":
                return date(int(m[3]), _MONTHS.index(m[2]) + 1, int(m[1]))
            return date(int(m[3]), int(m[1]), int(m[2]))   # US m/d/y
        except ValueError:
            continue   # 31/02/2026 and friends — try the next shape
    return None


def match_card(haystack: str, cards: dict) -> tuple[str | None, str | None]:
    """Resolve a bank/card label to a card nickname via users.cards (E8).

    Returns (card_key, matched_label). Longest match string first, because
    'Chase' and 'Chase Freedom' are both in the config and the shorter one
    would otherwise claim every Freedom email for the debit account.

    A miss returns (None, None) rather than a guess. A wrong card silently
    corrupts E9's reconciliation, which is the one outside check this system
    has; a null card is merely incomplete, and visibly so.
    """
    candidates = sorted(
        ((label, key) for key, cfg in (cards or {}).items()
         for label in (cfg.get("match") or [])),
        key=lambda pair: len(pair[0]), reverse=True)
    for label, key in candidates:
        if norm(label) and norm(label) in haystack:
            return key, label
    return None, None


def is_payment(subject: str) -> bool:
    return _any(PAYMENT_SUBJECTS, norm(subject))


def parse_payment(sender: str, subject: str, body: str,
                  cards: dict) -> dict | None:
    """Pull amount, date and card out of a payment confirmation.

    Returns None when no amount is found — the caller records that as
    `unparsed_payment` and moves on. It does NOT fall back to a model: an
    unreadable payment email is a pattern to add, not a token to spend.
    """
    subject_n, body_n = norm(subject), norm(body)
    m = _AMOUNT.search(subject_n) or _AMOUNT.search(body_n)
    if not m:
        return None
    try:
        amount = Decimal(m[1].replace(",", ""))
    except InvalidOperation:
        return None

    card, label = match_card(f"{norm(sender)} {subject_n} {body_n}", cards)
    return {
        "amount": amount,
        # None means "the email did not say"; the caller falls back to Gmail's
        # internalDate. Never now() — PLAN-EXPENSES.md §4.
        "paid_on": _find_date(subject_n) or _find_date(body_n),
        "card": card,
        "bank_label": label,
    }


# --- Lane B · application receipts and rejections ---------------------------

RECEIPT_PATTERNS = _compile(
    r"thank(?:s| you) for applying",
    r"thank(?:s| you) for your (?:interest|application)",
    r"we(?:'ve| have)? rec(?:ei|ie)ved your application",
    r"your application (?:has been |was )?rec(?:ei|ie)ved",
    r"application rec(?:ei|ie)ved",
    r"we appreciate your interest",
    r"(?:update|status) on your application",
)

# The three families from the real templates, plus their cousins. Order is
# irrelevant — any hit is a rejection.
#
# NOTHING in this list is praise. "your skills and background are impressive"
# is not here and must never be added: it appears in rejections and in
# hold-tight emails alike, and on its own it means nothing. The rejection is
# carried by the proceed/pursue/move-forward clause, so that is what is
# matched. test_gmail.py asserts this list stays praise-free.
REJECTION_PATTERNS = _compile(
    # family 1 — move forward with other candidates
    r"mov(?:e|ing) forward with (?:other|another) candidate",
    # family 2 — pursue other candidates
    r"pursue other candidates",
    r"more closely match(?:es)? our employment needs",
    # family 3 — proceed with other applicants
    r"proceed with other (?:applicants|candidates)",
    r"more closely fit(?:s)? our needs",
    # cousins — same intent, different template
    r"not (?:be )?mov(?:e|ing) forward with your (?:application|candidacy)",
    r"we will not be moving forward",
    r"decided not to (?:move|proceed|continue)",
    r"will not be proceeding with your (?:application|candidacy)",
    r"not (?:be )?selected for (?:this|the) (?:role|position)",
    r"(?:have|has) filled (?:this|the) position",
)

# Sender domains that identify an applicant-tracking system or a mail provider
# rather than the employer, so the domain is useless as a company name.
ATS_DOMAINS = frozenset({
    "greenhouse", "greenhouse-mail", "lever", "hire", "ashbyhq", "workday",
    "myworkday", "myworkdayjobs", "icims", "smartrecruiters", "taleo",
    "jobvite", "workable", "breezy", "recruitee", "bamboohr", "rippling",
    "successfactors", "oraclecloud", "paylocity", "dayforcehcm",
    "gmail", "googlemail", "outlook", "hotmail", "yahoo", "sendgrid",
    "mailgun", "amazonses", "notifications",
})

# Words that are part of a mailbox's persona, not the employer's name.
_DISPLAY_NOISE = re.compile(
    r"\b(careers?|recruit(?:ing|ment)?|talent|hiring|jobs?|hr|people|"
    r"no[- ]?reply|do[- ]?not[- ]?reply|team|notifications?|via)\b")

_LEGAL_SUFFIX = re.compile(
    r"\b(inc|llc|l\.?l\.?c|ltd|limited|corp|corporation|co|company|plc|"
    r"gmbh|bv|ag|sa|pvt|pte)\b")

# Company sits after "at"/"to"/"with". Role sits after "for" — a distinction
# the templates are consistent about ("Application received for Data Analyst"
# vs "Thank you for applying at Acme") and that nothing else recovers.
_COMPANY_FROM_SUBJECT = _compile(
    r"appl(?:ying|ication|ied) (?:to|at|with) (.+)",
    r"your application (?:to|at|with) (.+)",
    r"(?:update|status) on your application (?:to|at|with) (.+)",
    r"thank(?:s| you) for your interest in (.+)",
)
_ROLE_PATTERNS = _compile(
    r"appl(?:ication|ied|ying) (?:received )?for (?:the )?(.+)",
    r"your application for (?:the )?(.+)",
    r"for the (.+?) (?:role|position|opening)",
    r"the (.+?) (?:role|position) at ",
)

def split_sender(sender: str) -> tuple[str, str]:
    """('Acme Careers <no-reply@acme.com>') -> ('Acme Careers', 'acme.com').

    A bare address has no display name, and must not be mistaken for one.
    """
    text = (sender or "").strip()
    m = re.search(r"<([^<>]+)>", text)
    address = m[1] if m else text
    display = text[:m.start()].strip().strip("'\"") if m else ""
    return display, address.rpartition("@")[2].strip().lower()


def _clean(raw: str, max_words: int = 6) -> str | None:
    """Trim a capture group down to something that could be a name.

    Subjects carry tails — ' | Acme', ' - Req #4821', ' (Remote)'. Cut at the
    first separator, drop trailing punctuation, and refuse anything long
    enough to be a sentence: a capture of eight words means the regex caught
    prose, and a wrong company is worse than no company.
    """
    text = re.split(r"\s[|·\-–—]\s|[|!?]|\s*\(", raw, maxsplit=1)[0]
    text = text.strip(" .,:;-–—'\"")
    text = re.sub(r"\s+", " ", text)
    if not text or len(text) > 80 or len(text.split()) > max_words:
        return None
    return text


def company_key(name: str) -> str:
    """The matching key. 'Acme, Inc.' and 'ACME Inc' must collapse to one.

    Only legal suffixes are stripped — not 'Labs', 'Group' or 'Technologies',
    which distinguish genuinely different companies more often than they are
    noise.
    """
    text = re.sub(r"[^a-z0-9 ]", " ", norm(name))
    text = _LEGAL_SUFFIX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_company(sender: str, subject: str, body: str) -> str | None:
    """A ladder, not one brittle regex. Subject, then the sender's display
    name, then its domain — each a weaker signal than the last."""
    subject_n = norm(subject)
    for pattern in _COMPANY_FROM_SUBJECT:
        m = pattern.search(subject_n) or pattern.search(norm(body)[:400])
        if m and (name := _clean(m[1])):
            return name

    display, domain = split_sender(sender)
    if display:
        stripped = _clean(_DISPLAY_NOISE.sub(" ", norm(display)), max_words=5)
        if stripped:
            return stripped

    label = domain.split(".")[0] if domain else ""
    # Strip a mail subdomain so 'mail.acme.com' and 'us.greenhouse.io' both
    # land on the part that identifies who sent it.
    parts = [p for p in domain.split(".") if p not in ("mail", "email", "us", "www")]
    label = parts[0] if parts else label
    return label if label and label not in ATS_DOMAINS else None


def _find_role(subject: str, body: str) -> str | None:
    for pattern in _ROLE_PATTERNS:
        m = pattern.search(norm(subject)) or pattern.search(norm(body)[:600])
        if m and (role := _clean(m[1], max_words=7)):
            return re.sub(r"\s+(role|position|opening)$", "", role) or None
    return None


def is_application(subject: str, body: str) -> bool:
    text_s, text_b = norm(subject), norm(body)
    return (_any(RECEIPT_PATTERNS, text_s, text_b)
            or _any(REJECTION_PATTERNS, text_s, text_b))


def parse_application(sender: str, subject: str, body: str) -> dict | None:
    """Classify an application email and pull out who it is from.

    Three cases, and the third is the one a receipt-versus-rejection framing
    misses: most real rejections match NO receipt pattern at all, because the
    subject is "Update on your application". So a rejection phrase wins
    outright, whether or not a receipt pattern also fired.

    Rejection phrasing frequently lives in the body under a neutral subject,
    which is why both are always scanned.

    Returns None when no company could be extracted — the caller records
    `unparsed_application` rather than writing a row it cannot identify.
    """
    text_s, text_b = norm(subject), norm(body)
    if _any(REJECTION_PATTERNS, text_s, text_b):
        status = "rejected"
    elif _any(RECEIPT_PATTERNS, text_s, text_b):
        status = "applied"
    else:
        return None

    company = _find_company(sender, subject, body)
    if not company:
        return None
    return {
        "status": status,
        "company": company,
        "company_key": company_key(company),
        "role": _find_role(subject, body),
    }


# --- the router -------------------------------------------------------------

def route(sender: str, subject: str, body: str) -> str | None:
    """Which lane, if any. Order matters and is the point of the function.

    Payment first: a card issuer's "thank you for your payment" must never be
    read as anything else. Then applications — and note that the sender is NOT
    a gate here. Rejections arrive from careers@, from named recruiters and
    from no-reply alike; requiring a noreply-ish sender would drop real ones,
    and evaluation_plan.md §9 says to optimise recall. A false positive is one
    visible wrong row; a missed rejection is invisible.
    """
    if is_payment(subject):
        return "payment"
    if is_application(subject, body):
        return "application"
    return None
