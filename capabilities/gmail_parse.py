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


# --- Lane C · card spend alerts ---------------------------------------------
#
# The "you spent $42 at AMZN" side. PLAN-EXPENSES.md E1 asked whether
# extraction runs on arrival or is batched at digest time, and framed it as a
# model call per alert traded against holding raw alert text in the database
# overnight — which evaluation_plan.md §4 forbids.
#
# Rules on arrival dissolve the trade: nothing raw is ever stored and nothing
# is spent. E1 is settled the same way Lane A was.
#
# These emails are the ONLY place an amount enters the system from a source
# that actually saw the transaction, so the subject family stays tight. A miss
# is a row you add by hand; a false positive is a phantom charge in your month.

SPEND_SUBJECTS = _compile(
    r"transaction alert",
    r"you (?:made|had) a (?:transaction|purchase|charge)",
    r"your (?:card|account) was (?:used|charged)",
    r"(?:a |large |new )?(?:purchase|charge|transaction) (?:of|was|alert|notification)",
    r"you spent",
    r"(?:debit|credit) card (?:transaction|purchase)",
    r"alert:.*(?:transaction|purchase|charge|spent)",
)

# Merchant strings are garbage — 'POS 4321 AMZN MKTP IN' is what a bank sends.
# That is fine and deliberate: E6 means the user supplies the meaning anyway,
# so this captures the raw string rather than guessing at a cleaned-up name.
#
# The terminator is a lookahead over the words a bank template puts AFTER the
# merchant — "at BLUE BOTTLE COFFEE was made on..." must not capture the verb.
_STOP = r"(?=\s+(?:on|for|using|with|was|were|is|has|have|from|in)\b|[.,;!]|$)"
_MERCHANT = _compile(
    rf"\bat ([a-z0-9&*#'.\-/ ]{{2,40}}?){_STOP}",
    rf"\bto ([a-z0-9&*#'.\-/ ]{{2,40}}?){_STOP}",
    rf"merchant:? ([a-z0-9&*#'.\-/ ]{{2,40}}?){_STOP}",
)


def is_spend(subject: str) -> bool:
    return _any(SPEND_SUBJECTS, norm(subject))


def parse_spend(sender: str, subject: str, body: str,
                cards: dict) -> dict | None:
    """Amount, merchant, date and card out of a bank swipe alert.

    Deliberately extracts NO category. Per E6 the meaning comes from the user
    at the digest, and a guessed category on a pending row would look like an
    answer rather than a question.
    """
    subject_n, body_n = norm(subject), norm(body)
    m = _AMOUNT.search(subject_n) or _AMOUNT.search(body_n)
    if not m:
        return None
    try:
        amount = Decimal(m[1].replace(",", ""))
    except InvalidOperation:
        return None

    merchant = None
    for pattern in _MERCHANT:
        hit = pattern.search(subject_n) or pattern.search(body_n)
        if hit and (name := _clean(hit[1], max_words=6)):
            merchant = name
            break

    card, label = match_card(f"{norm(sender)} {subject_n} {body_n}", cards)
    return {
        "amount": amount,
        "merchant": merchant,
        "spent_on": _find_date(subject_n) or _find_date(body_n),
        "card": card,
        "bank_label": label,
    }


# --- Lane B · application receipts and rejections ---------------------------

RECEIPT_PATTERNS = _compile(
    r"thank(?:s| you) for applying",
    r"thank(?:s| you) for your application",
    r"we(?:'ve| have)? rec(?:ei|ie)ved your application",
    r"your application (?:has been |was )?rec(?:ei|ie)ved",
    r"application rec(?:ei|ie)ved",
    r"(?:update|status) (?:on|regarding|of) your application",
    r"your application (?:status|update)",
    # Real subjects that carry no "thank you" at all.
    r"application (?:update|status)\b",
    r"recent submission",
    r"you(?:'ve| have) (?:applied|submitted)",
    r"your candidacy",
)

# "Thanks for your interest in Apple." is a marketing email. The phrase is too
# weak to stand alone, so it counts only when the mail is actually about an
# application — a guard, not a pattern, because the phrase itself is genuine
# in both contexts and only the surrounding words separate them.
_WEAK_RECEIPT = _compile(
    r"thank(?:s| you) for your interest",
    r"we appreciate your interest",
)
_APPLICATION_CONTEXT = re.compile(
    r"\b(appl(?:y|ied|ying|ication)|candidacy|candidate|r[eé]sum[eé]|cv|"
    r"recruit|hiring|position|job req|requisition)\b")

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
    "successfactors", "oraclecloud", "paylocity", "dayforcehcm", "brassring",
    "paycomonline", "tsenta", "jobright", "indeed", "glassdoor", "wellfound",
    "linkedin", "ziprecruiter", "lensa", "myworkdaysite",
    "gmail", "googlemail", "outlook", "hotmail", "yahoo", "sendgrid",
    "mailgun", "amazonses", "notifications",
})

# Subdomains that name the FUNCTION, not the company: careers.acme.com and
# recruiting.acme.com are both Acme.
_DOMAIN_NOISE = frozenset({
    "mail", "email", "e", "us", "www", "careers", "career", "recruiting",
    "recruitment", "talent", "jobs", "hire", "hiring", "apply", "notification",
    "notifications", "no-reply", "noreply", "smtp", "mailer", "reply",
})

# Words that are part of a mailbox's persona, not the employer's name.
_DISPLAY_NOISE = re.compile(
    r"\b(careers?|recruit(?:ing|ment|er)?|talent|acquisition|hiring|jobs?|hr|"
    r"people|culture|colleague|zone|inbox|system|message|"
    r"no[- ]?reply|do[- ]?not[- ]?reply|team|notifications?|via|workday|"
    r"icims|greenhouse|lever|ashby|brassring|paycom)\b")

# Local parts that name the robot, not the company.
_ROBOT = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|auto[-_.]?reply|reply|donotreply|"
    r"system(?:message)?|notification|notifications|mailer|info|hello|hi|"
    r"inbox|mail|email|contact|updates?|alerts?|messages?|"
    r"careers?|jobs?|recruiting|recruitment|talent|hr|team|support|admin)$")

# A capture that looks like a job title is a job title, not an employer. Real
# subjects say "Thank you for applying to AI Engineer III - 26013067", where
# the thing after "to" is the ROLE — so the company must come from elsewhere.
_LOOKS_LIKE_ROLE = re.compile(
    r"\b(engineer|scientist|analyst|manager|developer|intern(?:ship)?|"
    r"specialist|consultant|designer|architect|associate|director|"
    r"administrator|coordinator|technician|researcher|lead|senior|junior|"
    r"staff|principal|full[- ]?stack|front[- ]?end|back[- ]?end)\b|\d{4,}")

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
    # Real templates seen in the wild, where the company leads the subject.
    r"^(.+?) (?:job )?application (?:update|status|received)",
    r"^(.+?) - (?:your )?application",
    r"follow(?:ing)? up from (.+)",
    r"^(?:update|follow up) from (.+)",
    r"your (.+?) application for ",
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
    """The matching key. Every spelling of one employer must collapse to one.

    Spaces are removed, not just collapsed, and that is the important part:
    the same company reaches us as 'CVS Health' in a subject line and as
    'cvshealth@myworkday.com' in a sender. Keeping the space would file those
    as two employers and the rejection would never find the receipt.

    Only legal suffixes are stripped — not 'Labs', 'Group' or 'Technologies',
    which distinguish genuinely different companies more often than they are
    noise.
    """
    text = re.sub(r"[^a-z0-9 ]", " ", norm(name))
    text = _LEGAL_SUFFIX.sub(" ", text)
    return re.sub(r"\s+", "", text)


def _ats_local_part(sender: str) -> str | None:
    """An ATS domain names the vendor, but its LOCAL part usually names the
    employer: capitalone@myworkday.com, somatus+autoreply@talent.icims.com.
    That is the highest-confidence signal available on those emails, because
    the vendor generated it from the employer's own account.
    """
    _, domain = split_sender(sender)
    if not any(part in ATS_DOMAINS for part in domain.split(".")):
        return None
    local = (sender.rpartition("<")[2] or sender).partition("@")[0].strip("\"'<> ")
    local = re.split(r"[+]", local)[0].strip("-._")
    return None if not local or _ROBOT.match(local.lower()) else local


def _find_company(sender: str, subject: str, body: str) -> str | None:
    """A ladder, not one brittle regex, and the order is what real mail taught.

    Subject first — but a subject capture that reads as a job title is thrown
    away, because "applying to AI Engineer III" puts the ROLE after "to" and
    taking it would file the application under a job title forever.
    """
    subject_n = norm(subject)
    for pattern in _COMPANY_FROM_SUBJECT:
        m = pattern.search(subject_n) or pattern.search(norm(body)[:400])
        if m and (name := _clean(m[1])) and not _LOOKS_LIKE_ROLE.search(name):
            return name

    if local := _ats_local_part(sender):
        return local

    display, domain = split_sender(sender)

    # The domain outranks the display name, which is the opposite of the
    # obvious order and is what the real mail argued for. A company's own
    # domain is written once and never decorated; its display name collects
    # departments — "Cognizant Talent Acquisition Group" is Cognizant, but
    # "Harvest Group People & Culture" is Harvest Group, and no amount of
    # word-stripping tells those two apart. careers.acme.com is just Acme.
    parts = [p for p in domain.split(".") if p not in _DOMAIN_NOISE]
    label = parts[0] if parts else ""
    if label and label not in ATS_DOMAINS:
        return label

    if display:
        # '@ icims', 'Workday Notification', 'People & Culture' — vendor and
        # department furniture bolted onto the employer's name.
        cleaned = re.sub(r"@\s*\w+\s*$", " ", norm(display))
        stripped = _clean(_DISPLAY_NOISE.sub(" ", cleaned).replace("&", " "),
                          max_words=5)
        if stripped:
            return stripped
    return None


def _find_role(subject: str, body: str) -> str | None:
    for pattern in _ROLE_PATTERNS:
        m = pattern.search(norm(subject)) or pattern.search(norm(body)[:600])
        if m and (role := _clean(m[1], max_words=7)):
            # Captures run on past the title: "data scientist role at mercor",
            # "machine learning engineer opportunity at somatus". Cut at the
            # employer and drop the trailing noun.
            role = re.split(r"\s+(?:at|with|for)\s+", role)[0]
            role = re.sub(r"\s+(role|position|opening|opportunity|req\w*)$",
                          "", role).strip(" -–—,.")
            return role or None
    return None


def _is_receipt(text_s: str, text_b: str) -> bool:
    if _any(RECEIPT_PATTERNS, text_s, text_b):
        return True
    return (_any(_WEAK_RECEIPT, text_s, text_b)
            and bool(_APPLICATION_CONTEXT.search(f"{text_s} {text_b[:2000]}")))


def is_application(subject: str, body: str) -> bool:
    text_s, text_b = norm(subject), norm(body)
    return (_is_receipt(text_s, text_b)
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
    elif _is_receipt(text_s, text_b):
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

    Payment is checked FIRST and this is the single most important line in the
    file (E7). A card issuer's "thank you for your payment" is a settlement of
    swipes already counted; read as a spend it double-counts the entire month,
    silently. Both families mention cards and amounts, so the specific one wins.

    Then spend, then applications — and note the sender is NOT a gate for the
    last one. Rejections arrive from careers@, from named recruiters and from
    no-reply alike; requiring a noreply-ish sender would drop real ones, and
    evaluation_plan.md §9 says to optimise recall. A false positive there is
    one visible wrong row; a missed rejection is invisible.
    """
    if is_payment(subject):
        return "payment"
    if is_spend(subject):
        return "spend"
    if is_application(subject, body):
        return "application"
    return None
