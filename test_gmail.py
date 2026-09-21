"""Phase 1b checks — the email rules, offline.

    python test_gmail.py          # or: pytest test_gmail.py

Every check here imports capabilities.gmail_parse and nothing else: no network,
no Supabase, no environment. That is the point. These rules are the whole
classification surface of the capability, and the four rejection templates
below are the ones that actually arrived — they get asserted on every run
rather than the first time a real one is misread.

All fixtures are anonymised. Acme and Initech are not real employers.
"""

import os
import re
import subprocess
import sys

from capabilities import gmail_parse as gp

ROOT = os.path.dirname(os.path.abspath(__file__))


def _status(subject, body, sender="Acme Careers <no-reply@acme.com>"):
    out = gp.parse_application(sender, subject, body)
    assert out, f"did not classify at all: {subject!r}"
    return out["status"]


# --- Lane B: the four rejection templates that actually arrived -------------

def test_rejection_unfortunately_move_forward():
    assert _status(
        "Your application to Acme",
        "Hi Sam,\n\nUnfortunately, we've decided to move forward with other "
        "candidates for this role.\n\nBest,\nThe Acme Team") == "rejected"


def test_rejection_after_careful_consideration():
    assert _status(
        "Update on your application",
        "After careful consideration, we've decided to move forward with "
        "other candidates whose experience more closely aligns.") == "rejected"


def test_rejection_pursue_other_candidates():
    assert _status(
        "Initech - Application Status",
        "Thank you for your interest in Initech. However, we have chosen to "
        "pursue other candidates who more closely match our employment "
        "needs.") == "rejected"


def test_rejection_proceed_with_other_applicants():
    """The one that carries praise. The praise is not what classifies it."""
    assert _status(
        "Regarding your application",
        "While your skills and background are impressive, we have decided to "
        "proceed with other applicants who more closely fit our needs at this "
        "time.") == "rejected"


# --- Lane B: the boundaries -------------------------------------------------

def test_receipt_with_no_rejection_is_applied():
    assert _status(
        "Thank you for applying at Acme",
        "We've received your application and will review it shortly.") == "applied"


def test_praise_alone_is_not_a_rejection():
    """The guard the whole REJECTION_PATTERNS list is shaped around: praise
    appears in rejections and in hold-tight emails alike, so on its own it
    means nothing."""
    assert _status(
        "Your application at Acme",
        "Thanks for applying! Your skills and background are impressive and "
        "the team is excited to review your profile. We'll be in touch.") == "applied"


def test_rejection_in_the_body_under_a_neutral_subject():
    """The common real shape. A subject-only scan calls this one 'applied'."""
    assert _status(
        "Acme Careers",
        "Hello,\n\nThanks for your interest in the Backend Engineer opening.\n"
        "We will not be moving forward with your application.\n\n"
        "We wish you the best.") == "rejected"


def test_receipt_and_rejection_together_is_rejected():
    assert _status(
        "Application received for Data Analyst",
        "We've received your application. After review, we have decided to "
        "proceed with other applicants.") == "rejected"


def test_rejection_with_no_receipt_pattern_at_all():
    """Most real rejections match no receipt phrase. The rule is: any
    rejection phrase wins, receipt pattern or not."""
    assert _status(
        "Initech",
        "We appreciate the time you spent with us. Unfortunately we have "
        "decided not to move forward.") == "rejected"


def test_unrelated_email_is_not_an_application():
    assert gp.parse_application(
        "Mum <mum@example.com>", "dinner sunday?",
        "are you free on sunday") is None


# --- Lane B: extraction -----------------------------------------------------

def test_company_comes_from_the_subject_when_it_is_there():
    out = gp.parse_application(
        "no-reply@us.greenhouse-mail.io", "Thank you for applying to Initech",
        "We got it.")
    assert out["company"] == "initech", out


def test_company_falls_back_to_the_sender_display_name():
    """The ATS domain identifies greenhouse, not the employer — so the display
    name is the only thing left that names who this is from."""
    out = gp.parse_application(
        "Acme Recruiting <no-reply@greenhouse.io>", "Application update",
        "We have decided to proceed with other applicants.")
    assert out["company"] == "acme", out


def test_role_comes_from_for_not_from_at():
    """'for <title>' is a role; 'at <company>' is a company. The templates are
    consistent about this and nothing else recovers the difference."""
    out = gp.parse_application(
        "Acme <no-reply@acme.com>", "Application received for Data Analyst",
        "Thanks for applying.")
    assert out["role"] == "data analyst", out
    assert out["status"] == "applied"


def test_company_key_collapses_legal_suffixes():
    assert gp.company_key("Acme, Inc.") == gp.company_key("ACME Inc") == "acme"
    # Spaces go too: an ATS slug and a human-written name must collapse.
    assert gp.company_key("Acme  Labs") == "acmelabs"
    assert gp.company_key("CVS Health") == gp.company_key("cvshealth")


def test_split_sender():
    assert gp.split_sender("Acme Careers <no-reply@acme.com>") == (
        "Acme Careers", "acme.com")
    assert gp.split_sender("no-reply@acme.com")[1] == "acme.com"


# --- Lane A: payment confirmations ------------------------------------------

CARDS = {
    "amex_everyday": {"type": "credit", "match": ["American Express", "AMEX"]},
    "chase_freedom": {"type": "credit", "match": ["Chase Freedom"]},
    "chase_debit": {"type": "debit", "match": ["Chase"]},
}


def test_payment_subject_family():
    for subject in ("We've received your payment",
                    "We’ve received your payment",     # curly apostrophe
                    "WE HAVE RECEIVED YOUR PAYMENT.",
                    "Thank you for the payment",
                    "Thank you for your payment!",
                    "  Payment   Received  ",
                    "Your payment confirmation"):
        assert gp.is_payment(subject), subject


def test_non_payment_subjects_do_not_match():
    for subject in ("Your payment is due", "Minimum payment reminder",
                    "Set up automatic payments"):
        assert not gp.is_payment(subject), subject


def test_payment_amount_and_date():
    out = gp.parse_payment(
        "American Express <no-reply@americanexpress.com>",
        "Thank you for the payment",
        "We received your payment of $1,234.56 on March 4, 2026. "
        "Account ending 1005.", CARDS)
    assert str(out["amount"]) == "1234.56", out
    assert out["paid_on"].isoformat() == "2026-03-04", out
    assert out["card"] == "amex_everyday", out


def test_payment_date_falls_back_to_none_not_today():
    """No date in the email means None, so the caller uses Gmail's own receipt
    time. now() would put the payment on the day the poll happened to run."""
    out = gp.parse_payment("x@y.com", "Payment received",
                           "Your payment of $50.00 has posted.", CARDS)
    assert out["paid_on"] is None and str(out["amount"]) == "50.00"


def test_amount_requires_cents_so_digits_are_not_amounts():
    """Reference ids and account digits are bare numbers. Without the cents
    requirement, 'reference 4821' becomes $4821."""
    assert gp.parse_payment("x@y.com", "Payment received",
                            "Confirmation 4821 for account 12345678.",
                            CARDS) is None


def test_longest_card_match_wins():
    """'Chase' and 'Chase Freedom' are both in the config. The short one must
    not claim the Freedom email for the debit account."""
    out = gp.parse_payment("Chase <no-reply@chase.com>",
                           "Thank you for your payment",
                           "Your Chase Freedom payment of $210.00 posted.",
                           CARDS)
    assert out["card"] == "chase_freedom", out


def test_unknown_bank_gives_no_card_rather_than_a_guess():
    out = gp.parse_payment("Citi <no-reply@citi.com>", "Payment received",
                           "We received your payment of $75.00.", CARDS)
    assert out["card"] is None and out["amount"]


# --- the router -------------------------------------------------------------

def test_payment_wins_over_application():
    """Order matters: a card issuer's 'thank you for your payment' must never
    reach the application lane."""
    assert gp.route("no-reply@amex.com", "Thank you for your payment",
                    "We appreciate your interest. Payment of $10.00 received."
                    ) == "payment"


def test_router_ignores_ordinary_mail():
    assert gp.route("friend@example.com", "lunch", "you free thursday?") is None


def test_rejection_routes_even_from_a_human_sender():
    """no-reply is a hint, not a gate. Rejections arrive from careers@ and from
    named recruiters, and evaluation_plan.md §9 says optimise recall."""
    assert gp.route("Dana Reyes <dana@acme.com>", "Following up",
                    "We've decided to move forward with other candidates."
                    ) == "application"


# --- real-mailbox regressions ------------------------------------------------
#
# Every case below is a sender/subject pair that actually arrived, found by
# scanning two live mailboxes. They are here because the first implementation
# got most of them wrong: "applying to AI Engineer III" filed under the job
# title, "Talent Acquisition (Do Not Reply)" filed under "acquisition", and
# an ATS address whose local part named the employer thrown away for the
# vendor's domain.

REAL_COMPANIES = [
    # (sender, subject, expected company_key)
    ("Amex Careers <careers@recruitment.americanexpress.com>",
     "Thank you for applying to AI Engineer III - 26013067",
     "americanexpress"),
    ("CapitalOneHRWorkday <capitalone@myworkday.com>",
     "Thank you for your interest", "capitalone"),
    ("Colleague Zone <cvshealth@myworkday.com>",
     "Thank you for your interest in CVS Health", "cvshealth"),
    ("Guidehouse Workday Notification <guidehouse@myworkday.com>",
     "Your Guidehouse Application for 42467 Data Scientist", "guidehouse"),
    ('"Somatus, Inc. @ icims" <somatus+autoreply@talent.icims.com>',
     "Thank You For Your Application", "somatus"),
    ('"PlanSource @ icims" <plansource+autoreply@talent.icims.com>',
     "Thank you for your application", "plansource"),
    ("Do Not Reply <somatus+email+4yr2-c5eac0@talent.icims.com>",
     "Somatus Application Update", "somatus"),
    ("Mercor Hiring Team <no-reply@ashbyhq.com>",
     "Thanks for applying to Mercor!", "mercor"),
    ("Baseten Hiring Team <no-reply@ashbyhq.com>",
     "Following up from Baseten", "baseten"),
    ("Charta Health Hiring Team <no-reply@ashbyhq.com>",
     "Charta Health Application Update", "chartahealth"),
    ("Distyl Recruiting - No Reply <no-reply-ashby@distyl.ai>",
     "Distyl: Update on Your Application", "distyl"),
    ("systemmessage@paycomonline.com",
     "Your Application With Sunlight Financial", "sunlightfinancial"),
    ('"Harvest Group People & Culture" <recruiting@harvestgroup.com>',
     "Harvest Group | Data Analyst, Amazon", "harvestgroup"),
    ('"Talent Acquisition (Do Not Reply)" <careers@recruiting.uhg.com>',
     "Associate AI/ML Engineer-2372389 Opening at UnitedHealth Group", "uhg"),
    ("Cognizant Talent Acquisition Group <TalentAcquisitionGroup@cognizant.com>",
     "We've received your application!", "cognizant"),
    ("Oden Technologies Hiring Team <no-reply@ashbyhq.com>",
     "Thank you for your Application to Oden Technologies", "odentechnologies"),
]


def test_real_sender_subject_pairs_resolve_to_the_employer():
    bad = []
    for sender, subject, expected in REAL_COMPANIES:
        out = gp.parse_application(sender, subject, "Thanks for applying.")
        got = out["company_key"] if out else None
        if got != expected:
            bad.append(f"{subject[:42]!r} -> {got!r}, wanted {expected!r}")
    assert not bad, "company extraction regressed:\n  " + "\n  ".join(bad)


def test_one_employer_reaching_us_two_ways_gets_one_key():
    """The duplicate-row bug: an ATS slug and a human-written name are the
    same employer and must produce the same key, or a rejection never finds
    the receipt it is meant to close."""
    from_ats = gp.parse_application("Colleague Zone <cvshealth@myworkday.com>",
                                    "Application update", "we have decided to "
                                    "proceed with other applicants.")
    from_subject = gp.parse_application("no-reply@example.com",
                                        "Thank you for applying to CVS Health",
                                        "Thanks!")
    assert from_ats["company_key"] == from_subject["company_key"] == "cvshealth"


def test_a_job_title_is_never_taken_as_the_employer():
    out = gp.parse_application(
        "Amex Careers <careers@recruitment.americanexpress.com>",
        "Thank you for applying to AI Engineer III - 26013067", "Thanks.")
    assert "engineer" not in out["company_key"], out
    assert not out["company_key"].isdigit()


def test_marketing_thanks_for_your_interest_is_not_an_application():
    """'Thanks for your interest in Apple.' is an ad. The phrase is genuine in
    both contexts, so only the surrounding words separate them."""
    assert gp.route("Apple <news@email.apple.com>",
                    "Thanks for your interest in Apple.",
                    "Discover the new iPhone lineup and Apple Watch. "
                    "Shop now and save on your next purchase.") is None


def test_the_same_phrase_with_application_context_still_counts():
    out = gp.parse_application("Colleague Zone <cvshealth@myworkday.com>",
                               "Thank you for your interest in CVS Health",
                               "We received your application for the Data "
                               "Analyst position and will review your resume.")
    assert out and out["status"] == "applied", out


REAL_IGNORED = [
    # Bank and retail marketing that must never reach a money lane. All of
    # these were sitting in the live mailboxes next to the real thing.
    ("Chase <no-reply@mcmap.chase.com>",
     "Rushi, Earn a $200 bonus plus unlimited 1.5% cash back"),
    ("Chase <no-reply@mcmap.chase.com>",
     "Rushi, earn $1000 Bonus Cash Back with Chase Ink Business Unlimited"),
    ("Apple <no_reply@insideapple.apple.com>",
     "Pay your Apple Card bill easily. And save on interest when you do."),
    ("Kohl's <sale@s.kohls.com>",
     "Labor Day Sale $5 Kohl's Cash for every $25 spent"),
    ("Kohl's <sale@s.kohls.com>",
     "$10 off your $25 purchase, $5 Kohl's Cash for every $25 spent"),
    ("Hertz <marketing@emails.hertz.com>",
     "Up to $750 off car purchases OR 20% off rentals"),
    ("Glassdoor Jobs <noreply@glassdoor.com>",
     "Senior Data Scientist at DOWC and 4 more jobs in New York"),
    ("Jobright Job Alert <noreply@jobright.ai>",
     "NVIDIA just posted a 77% match Research Scientist role"),
    ("Walmart <noreply@walmart.com>",
     "Reminder: Complete Your Application to Be Considered"),
    ("UnitedHealth Group Careers <careers@uhgtalentcommunity.com>",
     "Welcome to our Talent Community."),
]


def test_real_marketing_mail_stays_ignored():
    """Precision, measured on the noise that actually shares the mailbox.
    'Cash back', 'every $25 spent' and 'pay your bill' are the traps."""
    wrong = [(s, subj, gp.route(s, subj, "Shop now. Terms apply. Offer ends soon."))
             for s, subj in REAL_IGNORED
             if gp.route(s, subj, "Shop now. Terms apply. Offer ends soon.")]
    assert not wrong, f"false positives on marketing: {wrong}"


def test_roles_do_not_swallow_the_employer():
    out = gp.parse_application("Mercor Hiring Team <no-reply@ashbyhq.com>",
                               "Thanks for applying to Mercor!",
                               "We received your application for the Data "
                               "Scientist role at Mercor.")
    assert out["role"] == "data scientist", out


# --- the write paths, against an in-memory stand-in --------------------------
#
# Small enough to read, and it enforces the one constraint that matters:
# expenses(user_id, source_ref) is unique. Everything else about the real
# client is irrelevant to the decisions being checked here.

class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self.store, self.table, self.filters = store, table, []
        self.op = self.payload = None
        self.sort = None

    def select(self, *_a):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def order(self, field, desc=False):
        self.sort = (field, desc)
        return self

    def execute(self):
        rows = self.store.setdefault(self.table, [])
        if self.op == "insert":
            if self.table == "expenses" and any(
                    r["source_ref"] == self.payload.get("source_ref")
                    for r in rows):
                raise Exception('duplicate key value ... (23505)')
            row = {"id": f"{self.table}-{len(rows) + 1}",
                   "created_at": f"{len(rows):04d}", **self.payload}
            rows.append(row)
            return _Result([row])
        hits = [r for r in rows
                if all(r.get(k) == v for k, v in self.filters)]
        if self.op == "update":
            for row in hits:
                row.update(self.payload)
        elif self.sort:
            hits.sort(key=lambda r: r.get(self.sort[0]) or "",
                      reverse=self.sort[1])
        return _Result(hits)


class _Fake:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(self.store, name)


def _with_fake_db(fn):
    """Run fn(store) with core.db.sb() swapped for the stand-in."""
    from core import db as core_db
    store, real = {}, core_db.sb
    core_db.sb = lambda: _Fake(store)
    try:
        fn(store)
    finally:
        core_db.sb = real


USER = {"id": "u1", "cards": CARDS}


def _msg(subject, body, sender="Acme Careers <no-reply@acme.com>", mid="m1"):
    from datetime import datetime, timezone
    return {"id": mid, "sender": sender, "subject": subject, "body": body,
            "at": datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)}


def test_the_same_payment_email_never_writes_two_rows():
    """The acceptance criterion: exactly one cc_payment row per gmail id, even
    if the same message comes back through the poll."""
    from capabilities import gmail

    def check(store):
        msg = _msg("Thank you for the payment",
                   "We received your payment of $1,234.56 on March 4, 2026.",
                   sender="American Express <no-reply@americanexpress.com>")
        assert gmail._write_payment(USER, msg)[0] == "payment"
        assert gmail._write_payment(USER, msg)[0] == "payment"   # 23505, absorbed
        rows = store["expenses"]
        assert len(rows) == 1, rows
        assert rows[0]["kind"] == "cc_payment" and rows[0]["category"] is None
        assert rows[0]["card"] == "amex_everyday"
        assert rows[0]["spent_at"].startswith("2026-03-04")

    _with_fake_db(check)


def test_a_rejection_closes_the_application_its_receipt_opened():
    """One company, two emails, one row — and it ends up rejected. Matching
    ignores role precisely so this works when the strings differ."""
    from capabilities import gmail

    def check(store):
        gmail._upsert_application(USER, _msg(
            "Thank you for applying to Acme",
            "We've received your application for the Backend Engineer role."))
        outcome, _ = gmail._upsert_application(USER, _msg(
            "Update on your application",
            "We've decided to move forward with other candidates.", mid="m2"))
        assert outcome == "application_rejected"
        rows = store["applications"]
        assert len(rows) == 1, rows
        assert rows[0]["status"] == "rejected"
        assert rows[0]["role"] == "backend engineer"   # not blanked
        assert rows[0]["source_ref"] == "m2"

    _with_fake_db(check)


def test_an_orphan_rejection_still_becomes_a_visible_row():
    """No receipt ever arrived. The company comes off the subject or sender,
    the role stays empty, and the row exists rather than the signal vanishing."""
    from capabilities import gmail

    def check(store):
        outcome, _ = gmail._upsert_application(USER, _msg(
            "Initech", "Unfortunately we have decided not to move forward.",
            sender="Initech Recruiting <no-reply@greenhouse.io>"))
        assert outcome == "application_rejected"
        rows = store["applications"]
        assert len(rows) == 1 and rows[0]["status"] == "rejected"
        assert rows[0]["company_key"] == "initech" and rows[0]["role"] is None

    _with_fake_db(check)


def test_a_repeated_receipt_does_not_open_a_second_application():
    from capabilities import gmail

    def check(store):
        msg = _msg("Thank you for applying to Acme", "Got your application.")
        gmail._upsert_application(USER, msg)
        gmail._upsert_application(USER, dict(msg, id="m2"))
        assert len(store["applications"]) == 1

    _with_fake_db(check)


def test_an_unreadable_payment_writes_nothing_and_says_so():
    from capabilities import gmail

    def check(store):
        outcome, detail = gmail._write_payment(USER, _msg(
            "Payment received", "Your payment has posted. Reference 4821."))
        assert outcome == "unparsed_payment" and detail == {}
        assert "expenses" not in store

    _with_fake_db(check)


# --- structural: the rules that must stay true ------------------------------

def test_rejection_patterns_contain_no_praise():
    """The praise-only guard is an absence, not a function. Anything matching
    a compliment in this list would classify hold-tight emails as rejections."""
    praise = re.compile(r"impressive|strong (?:background|candidate)|"
                        r"enjoyed|great (?:profile|fit)|appreciate your",
                        re.I)
    for pattern in gp.REJECTION_PATTERNS:
        assert not praise.search(pattern.pattern), \
            f"praise phrase in REJECTION_PATTERNS: {pattern.pattern}"


def test_no_email_body_reaches_the_database():
    """evaluation_plan.md §4 is a trust boundary. The body is decoded, handed
    to the parsers and dropped; the message id is what gets stored."""
    src = open(os.path.join(ROOT, "capabilities", "gmail.py"),
               encoding="utf-8").read()
    writes = re.findall(r"\.insert\(\{(.*?)\}\)\.execute", src, re.S)
    writes += re.findall(r"\.update\(\{(.*?)\}\)", src, re.S)
    assert writes, "found no DB writes to check — did the file move?"
    for block in writes:
        for leak in ('msg["body"]', "body=", '"body"', '"snippet"'):
            assert leak not in block, f"body reaching a DB write: {block[:120]}"

    detail = re.search(r"def record_email_event.*?\n\n\n",
                       open(os.path.join(ROOT, "core", "db.py"),
                            encoding="utf-8").read(), re.S)
    assert detail and '"body"' not in detail.group(0)


def test_poll_makes_no_model_call():
    """The whole economic argument for rules over extraction. A model import in
    this path means every email costs tokens."""
    for name in ("gmail.py", "gmail_parse.py"):
        src = open(os.path.join(ROOT, "capabilities", name),
                   encoding="utf-8").read()
        for banned in ("core.turn", "core import turn", "handle_turn",
                       "models.call", "generativelanguage"):
            assert banned not in src, f"{name} reaches for a model: {banned}"


def test_gmail_adds_no_dependency():
    reqs = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()
    assert "google-api" not in reqs and "google-auth" not in reqs, \
        "the Gmail SDK got added — the point was raw REST over httpx"


def test_poll_runs_outside_the_checkin_hour():
    """The trap: the check-in loop `continue`s on checkin_hour, so a poll
    inside that loop body would run one hour a day."""
    tick = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    tick = tick.split("async def tick", 1)[1]
    assert "gmail.poll" in tick, "the gmail poll is not wired into /cron/tick"
    guard = tick.index('!= user["checkin_hour"]')
    assert tick.index("gmail.poll") < guard, \
        "gmail.poll sits below the checkin_hour guard — it will not run hourly"


def test_migrations_are_present():
    for name in ("003_expenses.sql", "004_email.sql"):
        assert os.path.exists(os.path.join(ROOT, "migrations", name)), name
    sql = open(os.path.join(ROOT, "migrations", "004_email.sql"),
               encoding="utf-8").read()
    assert "unique index if not exists email_events_dedupe_idx" in sql, \
        "the idempotency guarantee is gone"


# --- runner -----------------------------------------------------------------

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
