"""Phase 1 checks — recurring reminders, expenses, and the digest. Offline.

    python test_phase1.py          # or: pytest test_phase1.py

Everything here runs against an in-memory stand-in for Supabase (_FakeDB), so
no network, no keys, no real rows. What it does NOT stand in for is the CHECK
constraints in migrations/003_expenses.sql — those are asserted by reading the
SQL, at the bottom of this file.

The arithmetic is the part that will be wrong silently (PLAN-EXPENSES.md §4),
so it gets the most assertions: split, settle, and the kind filter that keeps a
card bill payment out of your spending totals.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))


# --- the stand-in -----------------------------------------------------------

# Column defaults, per table, as the migrations declare them. These matter:
# tasks.status defaults to 'todo' and everything that finds an open task
# filters on it, so a stand-in that defaulted it differently would make
# completion silently match nothing.
DEFAULTS = {
    "tasks": {"status": "todo", "slip_count": 0, "recur": None, "meta": {},
              "planned_on": None, "due_on": None, "deadline_hard": False,
              "estimate_min": 25, "priority_level": "normal", "category": None,
              "notes": None, "source": "chat", "completed_at": None},
    "expenses": {"status": "confirmed", "owed_amount": 0, "headcount": 1,
                 "card": None, "category": None, "merchant": None,
                 "notes": None, "source_ref": None, "kind": "spend",
                 "meta": {}},
    "applications": {"status": "applied", "role": None, "source": "gmail",
                     "source_ref": None, "last_email_at": None},
    "users": {}, "messages": {"meta": {}}, "email_events": {"detail": {}},
}


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Enough of the supabase-py chain for the calls these modules actually
    make. Filters are applied in order; unknown ones would silently match
    everything, so each is implemented rather than ignored."""

    def __init__(self, store, table):
        self.store, self.table = store, table
        self.ops, self.op, self.payload, self.sort = [], None, None, None

    def select(self, *_a):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, k, v):
        self.ops.append(lambda r: str(r.get(k)) == str(v))
        return self

    def neq(self, k, v):
        self.ops.append(lambda r: str(r.get(k)) != str(v))
        return self

    def in_(self, k, vs):
        self.ops.append(lambda r: r.get(k) in vs)
        return self

    def is_(self, k, _null):
        self.ops.append(lambda r: r.get(k) is None)
        return self

    def gt(self, k, v):
        self.ops.append(lambda r: float(r.get(k) or 0) > float(v))
        return self

    def gte(self, k, v):
        self.ops.append(lambda r: str(r.get(k) or "") >= str(v))
        return self

    def lt(self, k, v):
        self.ops.append(lambda r: str(r.get(k) or "") < str(v))
        return self

    def lte(self, k, v):
        self.ops.append(lambda r: str(r.get(k) or "") <= str(v))
        return self

    def order(self, field, desc=False):
        self.sort = (field, desc)
        return self

    def limit(self, _n):
        return self

    def execute(self):
        rows = self.store.setdefault(self.table, [])
        if self.op == "insert":
            # The one database guarantee the stand-in must reproduce, because
            # a test that cannot see a duplicate cannot prove there isn't one.
            if self.table == "expenses" and self.payload.get("source_ref") and any(
                    r.get("source_ref") == self.payload["source_ref"] for r in rows):
                raise Exception("duplicate key value ... (23505)")
            row = {**DEFAULTS.get(self.table, {}), **self.payload,
                   "id": f"{self.table[:3]}-{len(rows) + 1}",
                   "created_at": datetime.now(timezone.utc).isoformat()}
            rows.append(row)
            return _Result([row])
        hits = [r for r in rows if all(op(r) for op in self.ops)]
        if self.op == "update":
            for r in hits:
                r.update(self.payload)
        elif self.op == "delete":
            for r in hits:
                rows.remove(r)
        elif self.sort:
            hits.sort(key=lambda r: str(r.get(self.sort[0]) or ""),
                      reverse=self.sort[1])
        return _Result(hits)


class _FakeDB:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(self.store, name)


CARDS = {
    "amex_everyday": {"type": "credit", "match": ["American Express", "AMEX"],
                      "closes": None},
    "chase_freedom": {"type": "credit", "match": ["Chase Freedom"], "closes": None},
    "chase_debit": {"type": "debit", "match": ["Chase"], "closes": None},
    "cash": {"type": "cash", "match": [], "closes": None},
}
USER = {"id": "u1", "timezone": "UTC", "cards": CARDS, "checkin_hour": 11,
        "digest_hour": 21, "weekly_budget_min": 720, "slip_threshold": 3,
        "last_nudge_on": None, "channel_user_id": "c1"}


def _run(fn):
    """Run fn(store) with core.db.sb() and the per-turn context swapped out."""
    from core import ctx, db as core_db
    store, real = {}, core_db.sb
    core_db.sb = lambda: _FakeDB(store)
    ctx.begin(dict(USER))
    try:
        fn(store)
    finally:
        core_db.sb = real


# --- recurring reminders ----------------------------------------------------

def test_recur_rules_validate():
    from capabilities import tasks
    for good in ("daily", "weekly:mon", "weekly:sun", "monthly:1",
                 "monthly:31", "monthly:last", "yearly:03-15"):
        assert tasks.valid_recur(good), good
    for bad in ("", None, "monthly", "weekly:funday", "monthly:0",
                "monthly:32", "fortnightly", "yearly:3-15", "daily:1"):
        assert not tasks.valid_recur(bad), bad


def test_next_occurrence_monthly():
    from capabilities.tasks import next_occurrence as nxt
    assert nxt("monthly:15", date(2026, 9, 15)) == date(2026, 10, 15)
    # Completed early: the 15th of the SAME month is still ahead.
    assert nxt("monthly:15", date(2026, 9, 3)) == date(2026, 9, 15)
    # Year boundary.
    assert nxt("monthly:1", date(2026, 12, 1)) == date(2027, 1, 1)


def test_monthly_31_clamps_instead_of_skipping_february():
    """A bill reminder that silently skips a month is the expensive kind of
    wrong. The 31st of February is the 28th."""
    from capabilities.tasks import next_occurrence as nxt
    assert nxt("monthly:31", date(2026, 1, 31)) == date(2026, 2, 28)
    assert nxt("monthly:31", date(2028, 1, 31)) == date(2028, 2, 29)   # leap
    assert nxt("monthly:last", date(2026, 1, 31)) == date(2026, 2, 28)


def test_next_occurrence_weekly_and_yearly():
    from capabilities.tasks import next_occurrence as nxt
    assert nxt("weekly:mon", date(2026, 9, 17)) == date(2026, 9, 21)   # Thu -> Mon
    assert nxt("weekly:thu", date(2026, 9, 17)) == date(2026, 9, 24)   # never today
    assert nxt("daily", date(2026, 9, 17)) == date(2026, 9, 18)
    assert nxt("yearly:03-15", date(2026, 9, 17)) == date(2027, 3, 15)
    assert nxt("yearly:12-25", date(2026, 9, 17)) == date(2026, 12, 25)


def test_an_unusable_rule_makes_a_normal_task_not_an_error():
    from capabilities.tasks import next_occurrence as nxt
    assert nxt("every other tuesday", date(2026, 9, 17)) is None
    assert nxt(None, date(2026, 9, 17)) is None


def test_completing_a_reminder_rolls_it_and_leaves_a_record():
    """The shape of the whole feature: one live row that moves, plus a dated
    copy so there is a record you paid September's bill."""
    from capabilities import tasks

    def check(store):
        task = tasks.add_task("cancel the trial", planned_on="2026-09-15",
                              category="money", recur="monthly:15")
        assert "error" not in task, task
        out = tasks.complete(task["id"], USER["id"], "UTC")
        assert out["recurred_to"] == "2026-10-15", out

        rows = store["tasks"]
        assert len(rows) == 2, rows
        live = [r for r in rows if r["recur"]]
        done = [r for r in rows if r["status"] == "done"]
        assert len(live) == 1 and live[0]["planned_on"] == "2026-10-15"
        assert live[0]["status"] == "todo", "the reminder must stay open"
        assert len(done) == 1 and done[0]["planned_on"] == "2026-09-15"
        assert done[0]["recur"] is None, \
            "the history copy must never roll itself forward"

    _run(check)


def test_a_reminder_that_came_round_again_is_not_a_slipping_task():
    """slip_count resets on roll. Leaving it would make the assistant challenge
    you about a bill you pay on time every month."""
    from capabilities import tasks

    def check(store):
        task = tasks.add_task("pay the card bill", planned_on="2026-09-15",
                              recur="monthly:15")
        store["tasks"][0]["slip_count"] = 4
        store["tasks"][0]["meta"] = {"challenged": True, "slipped_on": "2026-09-16"}
        tasks.complete(task["id"], USER["id"], "UTC")
        live = [r for r in store["tasks"] if r["recur"]][0]
        assert live["slip_count"] == 0, live
        assert "challenged" not in live["meta"] and "slipped_on" not in live["meta"]

    _run(check)


def test_an_ordinary_task_still_just_closes():
    from capabilities import tasks

    def check(store):
        task = tasks.add_task("email the landlord", planned_on="2026-09-17")
        out = tasks.complete(task["id"], USER["id"], "UTC")
        assert out["status"] == "done" and "recurred_to" not in out
        assert len(store["tasks"]) == 1

    _run(check)


def test_completing_twice_is_a_no_op():
    """The Done button sends a different callback id each tap, so the message
    dedupe never sees it. in_(OPEN) inside complete() is what catches it."""
    from capabilities import tasks

    def check(store):
        task = tasks.add_task("gym", planned_on="2026-09-17")
        assert tasks.complete(task["id"], USER["id"], "UTC")
        assert tasks.complete(task["id"], USER["id"], "UTC") is None

    _run(check)


def test_a_bad_recur_rule_is_refused_not_stored():
    """Storing a rule that never fires is worse than refusing: the user would
    believe the reminder exists and never hear from it again."""
    from capabilities import tasks

    def check(store):
        out = tasks.add_task("water plants", recur="every other tuesday")
        assert "error" in out, out
        assert not store.get("tasks"), "it got written anyway"

    _run(check)


# --- expenses: the arithmetic -----------------------------------------------

def test_the_split_example_from_the_plan():
    """PLAN-EXPENSES.md Step 2.3, verbatim: 56 between 4 on the amex."""
    from capabilities import expenses

    def check(store):
        out = expenses.log_expense(56, merchant="dinner", category="food",
                                   card="amex_everyday", headcount=4)
        assert out["amount"] == "56.00" and out["share_amount"] == "14.00"
        assert out["owed_amount"] == "42.00" and out["headcount"] == 4
        assert out["card"] == "amex_everyday"

    _run(check)


def test_being_paid_back_does_not_change_what_the_month_cost():
    """The whole reason there are three columns."""
    from capabilities import expenses

    def check(store):
        row = expenses.log_expense(56, merchant="dinner", category="food",
                                   headcount=4)
        out = expenses.settle_split(row["id"], 14)
        assert out["still_owed"] == "28.00"
        assert out["share_amount"] == "14.00", "your spend moved on a repayment"
        assert out["amount"] == "56.00"

    _run(check)


def test_settling_never_goes_negative():
    from capabilities import expenses

    def check(store):
        row = expenses.log_expense(20, category="food", headcount=2)
        out = expenses.settle_split(row["id"], 999)
        assert out["still_owed"] == "0.00"

    _run(check)


def test_odd_splits_round_without_losing_a_cent():
    from capabilities import expenses

    def check(store):
        out = expenses.log_expense(10, category="food", headcount=3)
        assert out["share_amount"] == "3.33" and out["owed_amount"] == "6.67"
        assert (float(out["share_amount"]) + float(out["owed_amount"])
                == float(out["amount"]))

    _run(check)


def test_updating_the_amount_recomputes_the_split():
    from capabilities import expenses

    def check(store):
        row = expenses.log_expense(56, category="food", headcount=4)
        out = expenses.update_expense(row["id"], amount=60)
        assert out["share_amount"] == "15.00" and out["owed_amount"] == "45.00"

    _run(check)


# --- expenses: E7, the field that can be wrong by a whole month -------------

def test_a_card_bill_payment_is_not_in_the_spending_total():
    """E7. Counted as a spend it would double the month, silently."""
    from capabilities import expenses

    def check(store):
        expenses.log_expense(40, merchant="groceries", category="groceries",
                             card="amex_everyday")
        expenses.log_expense(900, kind="cc_payment", card="amex_everyday")
        out = expenses.list_expenses("today")
        assert out["spent_total"] == "40.00", out
        assert len(out["card_payments_excluded_from_total"]) == 1
        assert out["count"] == 2, "the payment must still be stored and listed"

    _run(check)


def test_a_card_payment_never_gets_a_category():
    from capabilities import expenses

    def check(store):
        out = expenses.log_expense(900, kind="cc_payment", category="fees",
                                   card="amex_everyday")
        assert out["category"] is None, out
        moved = expenses.update_expense(out["id"], kind="cc_payment",
                                        category="shopping")
        assert moved["category"] is None, moved

    _run(check)


def test_only_a_credit_card_can_have_a_bill_payment():
    """A debit account has no bill. Letting this through corrupts E9 for it."""
    from capabilities import expenses

    def check(store):
        out = expenses.log_expense(500, kind="cc_payment", card="chase_debit")
        assert "error" in out, out
        assert not store.get("expenses")

    _run(check)


def test_transfers_are_stored_but_never_counted():
    from capabilities import expenses

    def check(store):
        expenses.log_expense(25, category="food")
        expenses.log_expense(1000, kind="transfer", merchant="to savings")
        out = expenses.list_expenses("today")
        assert out["spent_total"] == "25.00" and out["count"] == 2

    _run(check)


# --- expenses: E8, cards are config -----------------------------------------

def test_an_unknown_card_is_refused_not_stored():
    """card='amex' when the config says 'amex_everyday' makes reconciliation
    return a confident zero, which is worse than an error."""
    from capabilities import expenses

    def check(store):
        out = expenses.log_expense(10, category="food", card="amex")
        assert "error" in out and "amex_everyday" in out["known_cards"]
        assert not store.get("expenses")

    _run(check)


def test_an_unknown_category_is_refused():
    from capabilities import expenses

    def check(store):
        assert "error" in expenses.log_expense(10, category="coffee")

    _run(check)


# --- expenses: E2, one row per spend ----------------------------------------

def test_a_chat_entry_attaches_to_the_pending_row_the_email_made():
    """E2, chat direction. Two rows for one dinner is the failure this rule
    exists to prevent."""
    from capabilities import expenses
    from core import db

    def check(store):
        today = db.local_today(USER)
        store["expenses"] = [{
            "id": "exp-1", "user_id": "u1", "amount": "50.00",
            "share_amount": "50.00", "owed_amount": 0, "headcount": 1,
            "status": "pending", "kind": "spend", "category": None,
            "merchant": "AMZN MKTP", "card": "amex_everyday", "source": "email",
            "source_ref": "gm-1", "spent_at": f"{today}T14:00:00+00:00",
            "notes": None, "created_at": f"{today}T14:00:00+00:00"}]

        out = expenses.log_expense(50, merchant="headphones",
                                   category="shopping")
        assert out.get("attached_to_pending_row"), out
        assert len(store["expenses"]) == 1, "a second row was created"
        row = store["expenses"][0]
        assert row["status"] == "confirmed" and row["category"] == "shopping"
        assert row["card"] == "amex_everyday", "the bank's card was overwritten"

    _run(check)


def test_two_pending_rows_of_the_same_amount_attach_to_neither():
    """Guessing between two real $50 charges is worse than asking once."""
    from capabilities import expenses
    from core import db

    def check(store):
        today = db.local_today(USER)
        base = {"user_id": "u1", "amount": "50.00", "share_amount": "50.00",
                "owed_amount": 0, "headcount": 1, "status": "pending",
                "kind": "spend", "category": None, "card": "amex_everyday",
                "source": "email", "spent_at": f"{today}T14:00:00+00:00",
                "notes": None, "merchant": "?"}
        store["expenses"] = [{**base, "id": "exp-1", "source_ref": "gm-1"},
                             {**base, "id": "exp-2", "source_ref": "gm-2"}]
        out = expenses.log_expense(50, category="food")
        assert not out.get("attached_to_pending_row")
        assert len(store["expenses"]) == 3, "it should have made its own row"

    _run(check)


def test_an_email_is_discarded_when_you_said_it_first():
    """E2, email direction — and the discard must be recorded, because an
    email that vanishes without a record looks like one that never arrived."""
    from capabilities import expenses, gmail

    def check(store):
        expenses.log_expense(42, merchant="lunch", category="food",
                             card="amex_everyday")
        msg = {"id": "gm-9", "sender": "AMEX <no-reply@americanexpress.com>",
               "subject": "Transaction alert",
               "body": "A charge of $42.00 at CAFE was made on your card.",
               "at": datetime.now(timezone.utc)}
        outcome, detail = gmail._write_spend(USER, msg)
        assert outcome == "spend_discarded", (outcome, detail)
        assert detail["discarded_against"]
        assert len(store["expenses"]) == 1

    _run(check)


def test_a_spend_email_with_no_chat_row_lands_as_pending():
    from capabilities import gmail

    def check(store):
        msg = {"id": "gm-10", "sender": "AMEX <no-reply@americanexpress.com>",
               "subject": "Transaction alert",
               "body": "A charge of $50.00 at AMZN MKTP was made on your "
                       "American Express card on March 4, 2026.",
               "at": datetime.now(timezone.utc)}
        outcome, detail = gmail._write_spend(USER, msg)
        assert outcome == "spend", (outcome, detail)
        row = store["expenses"][0]
        assert row["status"] == "pending" and row["kind"] == "spend"
        assert row["amount"] == "50.00" and row["card"] == "amex_everyday"
        assert row["category"] is None, "a guessed category would read as an answer"
        assert row["spent_at"].startswith("2026-03-04")

    _run(check)


def test_the_same_spend_email_never_writes_two_rows():
    from capabilities import gmail

    def check(store):
        msg = {"id": "gm-11", "sender": "no-reply@americanexpress.com",
               "subject": "Transaction alert",
               "body": "A charge of $7.25 at CAFE was made.",
               "at": datetime.now(timezone.utc)}
        assert gmail._write_spend(USER, msg)[0] == "spend"
        assert gmail._write_spend(USER, msg)[0] == "spend"
        assert len(store["expenses"]) == 1

    _run(check)


# --- the router boundary that matters most ----------------------------------

def test_a_payment_confirmation_is_never_routed_as_a_spend():
    """E7 at the router. Both families mention cards and amounts; the specific
    one has to win or the month doubles."""
    from capabilities import gmail_parse as gp
    for subject in ("We've received your payment",
                    "Thank you for the payment",
                    "Your payment confirmation"):
        assert gp.route("no-reply@amex.com", subject,
                        "A payment of $900.00 was received.") == "payment", subject


def test_a_swipe_alert_routes_to_spend():
    from capabilities import gmail_parse as gp
    for subject in ("Transaction alert", "You made a purchase",
                    "Your card was used", "Large transaction alert",
                    "Alert: a purchase was made on your card"):
        assert gp.route("no-reply@amex.com", subject,
                        "A charge of $42.00 at CAFE.") == "spend", subject


def test_spend_extraction_pulls_the_merchant_and_leaves_category_alone():
    from capabilities import gmail_parse as gp
    out = gp.parse_spend("AMEX <no-reply@americanexpress.com>",
                         "Transaction alert",
                         "A charge of $42.00 at BLUE BOTTLE COFFEE was made "
                         "on March 4, 2026.", CARDS)
    assert str(out["amount"]) == "42.00"
    assert out["merchant"] == "blue bottle coffee", out
    assert out["card"] == "amex_everyday"
    assert "category" not in out, "extraction must not guess a category (E6)"


# --- the digest -------------------------------------------------------------

def test_the_digest_says_nothing_on_an_empty_day():
    """An assistant that messages you to report that nothing happened is one
    you mute, and then you miss the day it matters."""
    from capabilities import expenses
    _run(lambda store: (_ for _ in ()).throw(AssertionError("digest spoke"))
         if expenses.digest(USER) else None)


def test_the_digest_lists_the_day_and_asks_about_pending_rows():
    from capabilities import expenses
    from core import db

    def check(store):
        today = db.local_today(USER)
        expenses.log_expense(12.40, merchant="Blue Bottle", category="food",
                             card="amex_everyday")
        expenses.log_expense(900, kind="cc_payment", card="amex_everyday")
        store["expenses"].append({
            "id": "exp-p", "user_id": "u1", "amount": "50.00",
            "share_amount": "50.00", "owed_amount": 0, "headcount": 1,
            "status": "pending", "kind": "spend", "category": None,
            "merchant": "AMZN MKTP", "card": "amex_everyday", "source": "email",
            "source_ref": "gm-1", "spent_at": f"{today}T14:00:00+00:00",
            "notes": None, "created_at": f"{today}T14:00:00+00:00"})

        text = expenses.digest(USER)
        assert "Today: 12.40" in text, text
        assert "Blue Bottle" in text and "food" in text
        assert "card payment" in text, "the bill payment must still be shown"
        assert "900" not in text.split("\n")[0], "it must not be in the total"
        assert "what was this?" in text and "AMZN MKTP" in text
        assert "1 waiting on you" in text

    _run(check)


def test_the_digest_makes_no_model_call():
    """E3 — a model should not do subtraction on your money."""
    src = open(os.path.join(ROOT, "capabilities", "expenses.py"),
               encoding="utf-8").read()
    body = src.split("def digest(", 1)[1]
    for banned in ("handle_turn", "models.generate", "core.turn"):
        assert banned not in body, f"the digest reaches for a model: {banned}"


def test_the_digest_flags_rows_left_unanswered_for_days():
    from capabilities import expenses
    from core import db

    def check(store):
        old = db.local_today(USER) - timedelta(days=5)
        store["expenses"] = [{
            "id": "exp-old", "user_id": "u1", "amount": "9.99",
            "share_amount": "9.99", "owed_amount": 0, "headcount": 1,
            "status": "pending", "kind": "spend", "category": None,
            "merchant": "SOMETHING", "card": None, "source": "email",
            "source_ref": "gm-x", "spent_at": f"{old}T10:00:00+00:00",
            "notes": None, "created_at": f"{old}T10:00:00+00:00"}]
        text = expenses.digest(USER)
        assert "for 3 days or more" in text, text

    _run(check)


# --- analytics --------------------------------------------------------------

def test_card_summary_separates_spend_from_the_bill():
    from capabilities import expenses

    def check(store):
        expenses.log_expense(40, category="groceries", card="amex_everyday")
        expenses.log_expense(60, category="food", card="amex_everyday")
        expenses.log_expense(95, kind="cc_payment", card="amex_everyday")
        out = expenses.card_summary("amex_everyday")
        assert out["captured_spend"] == "100.00" and out["bill_payments"] == "95.00"
        assert out["delta"] == "-5.00" and out["transactions"] == 2
        assert "signal" in out["delta_note"]

    _run(check)


def test_card_summary_says_a_debit_card_has_no_bill():
    from capabilities import expenses

    def check(store):
        out = expenses.card_summary("chase_debit")
        assert "not a credit card" in out["note"], out

    _run(check)


def test_by_category_breakdown_excludes_settlements():
    from capabilities import expenses

    def check(store):
        expenses.log_expense(30, category="food")
        expenses.log_expense(20, category="food")
        expenses.log_expense(10, category="transport")
        expenses.log_expense(500, kind="cc_payment", card="amex_everyday")
        out = expenses.list_expenses("month")
        assert out["by_category"] == {"food": "50.00", "transport": "10.00"}, out
        assert out["spent_total"] == "60.00"

    _run(check)


def test_list_applications_reads_what_the_poll_wrote():
    from capabilities import expenses

    def check(store):
        store["applications"] = [
            {"id": "a1", "user_id": "u1", "company": "Acme", "role": "SWE",
             "status": "rejected", "last_email_at": "2026-09-10"},
            {"id": "a2", "user_id": "u1", "company": "Initech", "role": None,
             "status": "applied", "last_email_at": "2026-09-14"}]
        assert expenses.list_applications("all")["count"] == 2
        open_ones = expenses.list_applications("applied")
        assert open_ones["count"] == 1
        assert open_ones["applications"][0]["company"] == "Initech"

    _run(check)


# --- everything at once ------------------------------------------------------

MAILBOX = {
    "gm-spend": {
        "id": "gm-spend", "sender": "AMEX <no-reply@americanexpress.com>",
        "subject": "Transaction alert",
        "body": "A charge of $50.00 at AMZN MKTP was made on your American "
                "Express card."},
    "gm-bill": {
        "id": "gm-bill", "sender": "American Express <no-reply@americanexpress.com>",
        "subject": "Thank you for the payment",
        "body": "We received your payment of $900.00 for your American "
                "Express account."},
    "gm-reject": {
        "id": "gm-reject", "sender": "Acme Careers <no-reply@greenhouse.io>",
        "subject": "Update on your application",
        "body": "After careful consideration, we've decided to move forward "
                "with other candidates."},
    "gm-noise": {
        "id": "gm-noise", "sender": "Mum <mum@example.com>",
        "subject": "sunday", "body": "are you free?"},
}


def test_one_tick_one_day_all_three_capabilities():
    """The whole of Phase 1 in one pass, then the same pass again.

    A reminder that recurs, a mailbox that produces a spend, a card bill and a
    rejection, a chat entry that lands on the spend the bank reported, and a
    digest that quotes the day back without counting the bill. Then a second
    poll that must change nothing.
    """
    from capabilities import expenses, gmail, tasks
    from core import db

    def check(store):
        real_list, real_msg, real_acc = (gmail._list_ids, gmail._message,
                                         gmail.accounts)
        gmail.accounts = lambda: {"personal": "fake-token"}
        gmail._list_ids = lambda label: list(MAILBOX)
        gmail._message = lambda label, mid: {
            **MAILBOX[mid], "id": f"{label}:{mid}", "account": label,
            "at": datetime.now(timezone.utc)}
        try:
            # 1. A monthly reminder, completed today.
            bill = tasks.add_task("pay the amex bill", planned_on="2026-09-17",
                                  category="money", deadline_hard=True,
                                  recur="monthly:17")
            assert tasks.complete(bill["id"], USER["id"], "UTC")["recurred_to"] \
                == "2026-10-17"

            # 2. The poll. Four emails, three lanes, one ignored.
            assert gmail.poll(USER) == 4
            outcomes = {e["gmail_message_id"]: e["outcome"]
                        for e in store["email_events"]}
            assert outcomes == {"personal:gm-spend": "spend",
                                "personal:gm-bill": "payment",
                                "personal:gm-reject": "application_rejected",
                                "personal:gm-noise": "ignored"}, outcomes
            assert store["applications"][0]["status"] == "rejected"

            # 3. You say what the $50 was. It lands on the bank's row (E2).
            said = expenses.log_expense(50, merchant="headphones",
                                        category="shopping")
            assert said.get("attached_to_pending_row"), said
            assert len(store["expenses"]) == 2, \
                "the chat entry made a second row for one spend"

            # 4. The digest: the day, without the bill in the total (E7).
            text = expenses.digest(USER)
            assert "Today: 50.00" in text, text
            assert "card payment" in text and "900" in text
            assert "waiting on you" not in text, "nothing should still be pending"

            # 5. The same tick again. Nothing moves.
            before = (len(store["expenses"]), len(store["applications"]),
                      len(store["email_events"]))
            assert gmail.poll(USER) == 0, "it re-processed handled messages"
            after = (len(store["expenses"]), len(store["applications"]),
                     len(store["email_events"]))
            assert before == after, (before, after)

            # 6. And the reminder is still open, on its next date.
            live = [t for t in store["tasks"] if t["recur"]]
            assert len(live) == 1 and live[0]["planned_on"] == "2026-10-17"
            assert live[0]["status"] == "todo"
            assert len([t for t in store["tasks"] if t["status"] == "done"]) == 1

            # 7. A month's analytics, from the same rows.
            card = expenses.card_summary("amex_everyday")
            assert card["captured_spend"] == "50.00"
            assert card["bill_payments"] == "900.00"
        finally:
            gmail._list_ids, gmail._message = real_list, real_msg
            gmail.accounts = real_acc

    _run(check)


def test_two_mailboxes_are_polled_and_never_collide():
    """Gmail message ids are unique WITHIN a mailbox, not across them. Two
    accounts minting the same id must stay two rows, or one account silently
    swallows the other's email."""
    from capabilities import gmail

    def check(store):
        real_list, real_msg, real_acc = (gmail._list_ids, gmail._message,
                                         gmail.accounts)
        # Same bare id in both mailboxes, different content.
        bodies = {
            "personal": "A charge of $11.00 at CAFE was made.",
            "work": "A charge of $22.00 at DELI was made.",
        }
        gmail.accounts = lambda: {"personal": "t1", "work": "t2"}
        gmail._list_ids = lambda label: ["collide"]
        gmail._message = lambda label, mid: {
            "id": f"{label}:{mid}", "account": label,
            "sender": "AMEX <no-reply@americanexpress.com>",
            "subject": "Transaction alert", "body": bodies[label],
            "at": datetime.now(timezone.utc)}
        try:
            assert gmail.poll(USER) == 2
            refs = sorted(e["gmail_message_id"] for e in store["email_events"])
            assert refs == ["personal:collide", "work:collide"], refs
            amounts = sorted(r["amount"] for r in store["expenses"])
            assert amounts == ["11.00", "22.00"], amounts

            # And the second pass still changes nothing, per mailbox.
            assert gmail.poll(USER) == 0
            assert len(store["expenses"]) == 2
        finally:
            gmail._list_ids, gmail._message = real_list, real_msg
            gmail.accounts = real_acc

    _run(check)


def test_one_broken_mailbox_does_not_cost_you_the_others():
    from capabilities import gmail

    def check(store):
        real_list, real_msg, real_acc = (gmail._list_ids, gmail._message,
                                         gmail.accounts)
        gmail.accounts = lambda: {"broken": "t1", "work": "t2"}

        def _list(label):
            if label == "broken":
                raise RuntimeError("invalid_grant: token revoked")
            return ["gm-1"]

        gmail._list_ids = _list
        gmail._message = lambda label, mid: {
            "id": f"{label}:{mid}", "account": label, "sender": "x@y.com",
            "subject": "Transaction alert", "body": "A charge of $5.00 at X.",
            "at": datetime.now(timezone.utc)}
        try:
            assert gmail.poll(USER) == 1, "the healthy mailbox was skipped too"
            assert len(store["expenses"]) == 1
        finally:
            gmail._list_ids, gmail._message = real_list, real_msg
            gmail.accounts = real_acc

    _run(check)


def test_accounts_are_discovered_from_the_environment():
    from capabilities import gmail
    saved = dict(os.environ)
    try:
        for k in [k for k in os.environ if k.startswith(gmail.TOKEN_PREFIX)]:
            del os.environ[k]
        assert gmail.accounts() == {}
        os.environ["GMAIL_TOKEN_PERSONAL"] = "1//aaa"
        os.environ["GMAIL_TOKEN_WORK"] = "1//bbb"
        os.environ["GMAIL_TOKEN_EMPTY"] = ""     # half-set is not configured
        assert gmail.accounts() == {"personal": "1//aaa", "work": "1//bbb"}
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --- structural -------------------------------------------------------------

def test_the_schema_forbids_a_confirmed_uncategorised_spend():
    sql = open(os.path.join(ROOT, "migrations", "003_expenses.sql"),
               encoding="utf-8").read()
    assert "confirmed_spend_needs_category" in sql
    assert "kind in ('spend','refund','transfer','cc_payment')" in sql
    for category in ("food", "groceries", "housing", "fees", "other"):
        assert f"'{category}'" in sql, category


def test_every_category_in_the_code_is_in_the_constraint():
    from capabilities import expenses
    sql = open(os.path.join(ROOT, "migrations", "003_expenses.sql"),
               encoding="utf-8").read()
    check = sql.split("category      text", 1)[1].split(")", 1)[0]
    for category in expenses.CATEGORIES:
        assert f"'{category}'" in check, f"{category} would fail the CHECK"


def test_the_recurrence_migration_exists():
    sql = open(os.path.join(ROOT, "migrations", "005_recurring.sql"),
               encoding="utf-8").read()
    for column in ("tasks add column if not exists recur",
                   "users add column if not exists digest_hour",
                   "users add column if not exists last_digest_on"):
        assert column in sql, column


def test_the_done_button_and_the_tool_share_one_completion_path():
    """Two code paths for finishing a task means a recurring reminder rolls
    forward one way and not the other."""
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    fast = src.split("def _fast_path", 1)[1].split("@app.post", 1)[0]
    assert "tasks.complete(" in fast, "the Done button bypasses tasks.complete"
    assert '"status": "done"' not in fast, \
        "the button writes its own completion instead of routing through it"


def test_the_digest_runs_outside_the_checkin_hour():
    tick = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    tick = tick.split("async def tick", 1)[1]
    assert "expenses.digest" in tick, "the digest is not wired into /cron/tick"
    assert tick.index("expenses.digest") < tick.index('!= user["checkin_hour"]')
    assert "last_digest_on" in tick, "nothing stops the digest firing hourly"


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
