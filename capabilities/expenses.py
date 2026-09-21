"""Expenses — the chat path, the analytics, and the 21:00 digest (Phase 1c).

One file against the D3 contract. The schema is migrations/003_expenses.sql;
the decisions are PLAN-EXPENSES.md E1-E9 and are referenced by number here
rather than re-argued.

Three rules shape everything below:

  E3  No model touches your numbers. Every total in this file is sum() over
      rows. The model is involved only when you ANSWER the digest.
  E7  `kind` is the highest-risk field in the system. A card bill payment is
      not a spend — it settles swipes already counted — so it is stored, listed
      and excluded from every total. Getting it wrong double-counts your month.
  E8  Cards are config (users.cards), validated here at write time rather than
      by a CHECK, so adding a card is an edit and not a migration.

The arithmetic is the part that will be wrong silently, so it lives in exactly
one function (_split) called by both write paths.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from core.tool import tool as beta_tool

from core import audit, ctx, db

# Eleven, and the CHECK constraint in 003_expenses.sql holds the same list.
# food is eating out, groceries is buying to cook: they move independently and
# only one of them is discretionary. housing covers rent AND the utility bills
# — they arrive together and are one decision.
CATEGORIES = ("food", "groceries", "transport", "housing", "subscriptions",
              "shopping", "health", "travel", "entertainment", "fees", "other")

KINDS = ("spend", "refund", "transfer", "cc_payment")

# Rows that are not spending. Every total filters on this, in one place, so
# "excluded from the totals" is a fact about the code rather than a promise.
NOT_SPENDING = ("cc_payment", "transfer")

PROMPT = """\
EXPENSES
- log_expense on anything the user says they spent, paid or bought. Capture is
  never blocked here either: log it first, ask about the category after.
- category: food (eating out) | groceries (to cook) | transport | housing (rent
  AND utility bills) | subscriptions | shopping | health | travel |
  entertainment | fees | other. Infer it; only ask when genuinely ambiguous.
- card: use the exact nickname from the user's card list, which list_expenses
  and card_summary return. Never invent one. Omit it if they did not say.
- A CREDIT CARD BILL PAYMENT is kind="cc_payment", never a spend and never
  given a category. "paid off the amex", "paid my card bill" is a settlement of
  swipes already counted — logging it as a spend double-counts the month. On a
  cc_payment, card is the card BEING PAID, not the account paying it.
- Moving money between your own accounts is kind="transfer".
- headcount is how many people SHARED it, including the user: "dinner for 4,
  I paid" is headcount=4. Never guess it above 1 unless they said so.
- settle_split when someone pays the user back. Not update_expense — a
  repayment must not change what the month cost you.
- Amounts are plain numbers, no currency symbol. Never do arithmetic yourself;
  the tools return every total you need.
"""


# --- money ------------------------------------------------------------------

def _money(value) -> Decimal:
    """Two decimal places, half-up, always. Floats do not survive a month of
    addition and this is the one place that can stop them trying."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _split(amount: Decimal, headcount: int) -> tuple[Decimal, Decimal]:
    """The whole split arithmetic, in one function, called by both writers.

    share is YOUR portion and is what your spending totals read. owed is what
    is outstanding TO you. They are separate columns precisely so that being
    paid back moves one and not the other — your October must not shrink
    because a friend settled up in November.
    """
    headcount = max(1, int(headcount or 1))
    share = (amount / headcount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return share, amount - share


def _cards(user: dict) -> dict:
    return user.get("cards") or {}


def _check_card(card: str | None, user: dict) -> tuple[str | None, dict | None]:
    """Validate against the keys of users.cards (E8). Returns (card, error).

    An unknown value is refused rather than stored: card='amex' when the config
    says 'amex_everyday' makes every reconciliation return a confident zero,
    and a confident zero is worse than an error.
    """
    if not card:
        return None, None
    cards = _cards(user)
    if card in cards:
        return card, None
    return None, {"error": "unknown card", "card": card,
                  "known_cards": sorted(cards) or "users.cards is empty"}


def _slim(row: dict) -> dict:
    return {k: row[k] for k in
            ("id", "spent_at", "amount", "share_amount", "owed_amount",
             "headcount", "merchant", "kind", "category", "card", "status")}


def _spent_at(when: str | None, user: dict) -> str:
    """A date the user named, else now. Never a naive local date — db.local_today
    is the one definition of a day and everything goes through it."""
    if when:
        try:
            day = date.fromisoformat(when[:10])
            return datetime(day.year, day.month, day.day, 12,
                            tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


# --- the write path ---------------------------------------------------------

@beta_tool
def log_expense(amount: float, merchant: str | None = None,
                category: str | None = None, card: str | None = None,
                spent_at: str | None = None, headcount: int = 1,
                kind: str = "spend", notes: str | None = None) -> dict:
    """Record something the user spent, paid or was refunded.

    Args:
        amount: The total charged, a plain number. For a split, this is the
            WHOLE bill, not the user's share — the share is computed.
        merchant: Where the money went, in the user's words.
        category: food | groceries | transport | housing | subscriptions |
            shopping | health | travel | entertainment | fees | other. Leave
            empty for a cc_payment or a transfer, which never have one.
        card: The card nickname, exactly as it appears in the user's card list.
            On a cc_payment this is the card BEING PAID.
        spent_at: YYYY-MM-DD if the user named a day. Omit for today.
        headcount: How many people shared it, including the user. 4 for
            "dinner for four". Leave at 1 unless they said otherwise.
        kind: spend | refund | transfer | cc_payment. cc_payment is a credit
            card BILL payment and is never a spend.
        notes: Anything said that does not belong in merchant.
    """
    user = ctx.user()
    if kind not in KINDS:
        return {"error": "unknown kind", "kind": kind, "allowed": list(KINDS)}
    if kind in NOT_SPENDING:
        category = None          # E7 — a made-up category here doubles the month
    elif category and category not in CATEGORIES:
        return {"error": "unknown category", "category": category,
                "allowed": list(CATEGORIES)}

    card, err = _check_card(card, user)
    if err:
        return err
    if kind == "cc_payment" and card and _cards(user).get(card, {}).get("type") != "credit":
        # Only a credit card has a bill. Paying "off" a debit account is a
        # transfer, and letting this through would corrupt E9 for that card.
        return {"error": "only a credit card can have a bill payment",
                "card": card, "kind": kind}

    try:
        total = _money(amount)
    except (InvalidOperation, ValueError):
        return {"error": "unreadable amount", "amount": amount}
    if total <= 0:
        return {"error": "amount must be positive", "amount": amount}

    # E2, chat direction: this spend may already be sitting here as a pending
    # row the bank emailed about. Fill it in rather than adding a second one.
    today = db.local_today(user)
    if kind == "spend":
        matches = db.pending_match(user["id"], str(total), today)
        if len(matches) == 1:
            return _attach(matches[0], total, merchant, category, card,
                           headcount, notes)
        if len(matches) > 1:
            # Two real charges of the same amount on one day. Guessing between
            # them is worse than letting the digest ask once.
            audit.step("dedupe", decision="ambiguous", amount=str(total),
                       candidates=[m["id"] for m in matches])

    share, owed = _split(total, headcount)
    audit.step("split", headcount=headcount, amount=str(total),
               share=str(share), owed=str(owed))
    row = db.sb().table("expenses").insert({
        "user_id": user["id"], "spent_at": _spent_at(spent_at, user),
        "amount": str(total), "share_amount": str(share), "owed_amount": str(owed),
        "headcount": max(1, int(headcount or 1)), "merchant": merchant,
        "kind": kind, "category": category, "card": card,
        "source": "voice" if ctx.source() == "voice" else "chat",
        "status": "confirmed", "notes": notes,
    }).execute().data[0]
    audit.step("write", table="expenses", op="insert", row=row["id"], kind=kind)
    return _slim(row)


def _attach(row: dict, total: Decimal, merchant, category, card,
            headcount, notes) -> dict:
    """E2: a chat entry lands on the pending row the email already created.

    The amount, card and date stay as the bank reported them — those are the
    facts it has and you do not. What you supply is the meaning.
    """
    share, owed = _split(total, headcount)
    fields = {"status": "confirmed", "share_amount": str(share),
              "owed_amount": str(owed), "headcount": max(1, int(headcount or 1))}
    if category:
        fields["category"] = category
    if merchant:
        fields["merchant"] = merchant
    if notes:
        fields["notes"] = notes
    if card and not row.get("card"):
        fields["card"] = card      # the email named none; yours is better than null
    out = (db.sb().table("expenses").update(fields)
           .eq("id", row["id"]).execute().data[0])
    audit.step("dedupe", decision="attached", against=row["id"],
               amount=str(total))
    return {**_slim(out), "attached_to_pending_row": True}


@beta_tool
def update_expense(expense_id: str, amount: float | None = None,
                   merchant: str | None = None, category: str | None = None,
                   card: str | None = None, headcount: int | None = None,
                   kind: str | None = None, notes: str | None = None) -> dict:
    """Correct an expense. Only pass what changes. Also how a pending row the
    digest asked about gets its meaning.

    Do NOT use this when someone pays the user back — that is settle_split.

    Args:
        expense_id: The uuid, from a previous tool result or the digest.
        amount: Corrected total. Recomputes the share and what is owed.
        merchant: Corrected merchant.
        category: One of the eleven categories.
        card: The card nickname.
        headcount: How many people shared it. Recomputes the share.
        kind: spend | refund | transfer | cc_payment.
        notes: Replaces existing notes.
    """
    user = ctx.user()
    cur = (db.sb().table("expenses").select("*")
           .eq("id", expense_id).eq("user_id", user["id"]).execute().data)
    if not cur:
        return {"error": "no such expense for this user", "expense_id": expense_id}
    row = cur[0]

    if kind and kind not in KINDS:
        return {"error": "unknown kind", "kind": kind, "allowed": list(KINDS)}
    if category and category not in CATEGORIES:
        return {"error": "unknown category", "category": category,
                "allowed": list(CATEGORIES)}
    card, err = _check_card(card, user)
    if err:
        return err

    fields = {k: v for k, v in (("merchant", merchant), ("category", category),
                                ("card", card), ("kind", kind), ("notes", notes))
              if v is not None}
    if fields.get("kind") in NOT_SPENDING:
        fields["category"] = None          # E7, again — one rule, both writers

    # Share and owed are recomputed whenever either input moves, and never on a
    # settlement. That separation is the whole reason there are three columns.
    if amount is not None or headcount is not None:
        total = _money(amount) if amount is not None else _money(row["amount"])
        heads = headcount if headcount is not None else row["headcount"]
        share, owed = _split(total, heads)
        fields |= {"amount": str(total), "share_amount": str(share),
                   "owed_amount": str(owed), "headcount": max(1, int(heads or 1))}
        audit.step("split", headcount=heads, amount=str(total),
                   share=str(share), owed=str(owed))

    if not fields:
        return {"error": "nothing to update"}
    # Answering the digest is what confirms a pending row.
    if row["status"] == "pending" and (fields.get("category") or merchant):
        fields["status"] = "confirmed"
    out = (db.sb().table("expenses").update(fields)
           .eq("id", expense_id).eq("user_id", user["id"]).execute().data[0])
    audit.step("write", table="expenses", op="update", row=expense_id)
    return _slim(out)


@beta_tool
def settle_split(expense_id: str, amount: float) -> dict:
    """Someone paid the user back. Subtracts from what is outstanding and
    touches nothing else.

    This is deliberately not update_expense: a repayment must never change what
    the month cost you, only what is still owed.

    Args:
        expense_id: The uuid of the shared expense.
        amount: How much was paid back, a plain number.
    """
    user = ctx.user()
    cur = (db.sb().table("expenses").select("*")
           .eq("id", expense_id).eq("user_id", user["id"]).execute().data)
    if not cur:
        return {"error": "no such expense for this user", "expense_id": expense_id}

    paid = _money(amount)
    owed = _money(cur[0]["owed_amount"])
    if paid <= 0:
        return {"error": "amount must be positive", "amount": amount}
    # Never below zero: an overpayment is a conversation, not a negative debt.
    remaining = max(Decimal("0.00"), owed - paid)
    out = (db.sb().table("expenses").update({"owed_amount": str(remaining)})
           .eq("id", expense_id).eq("user_id", user["id"]).execute().data[0])
    audit.step("write", table="expenses", op="settle", row=expense_id,
               was=str(owed), paid=str(paid), now=str(remaining))
    return {**_slim(out), "settled": str(paid), "still_owed": str(remaining)}


# --- the read path ----------------------------------------------------------

def _total(rows: list[dict], field: str = "share_amount") -> str:
    """Spending only. cc_payment and transfer rows are excluded here, in the
    one function every total goes through (E7)."""
    return str(_money(sum(Decimal(str(r[field])) for r in rows
                          if r["kind"] not in NOT_SPENDING) or 0))


def _by_category(rows: list[dict]) -> dict:
    out: dict[str, Decimal] = {}
    for r in rows:
        if r["kind"] in NOT_SPENDING:
            continue
        key = r["category"] or "uncategorised"
        out[key] = out.get(key, Decimal("0")) + Decimal(str(r["share_amount"]))
    return {k: str(_money(v)) for k, v in
            sorted(out.items(), key=lambda kv: kv[1], reverse=True)}


@beta_tool
def list_expenses(scope: str = "today") -> dict:
    """List expenses and their totals. Every number here is summed from rows —
    use them verbatim and never recompute.

    Args:
        scope: "today", "week" (from Monday), "month" (calendar month),
            "pending" (email rows waiting for the user to say what they were),
            "owed" (what people still owe the user), "cards" (the user's card
            nicknames), or "category:food" for one category this month.
    """
    user = ctx.user()
    today = db.local_today(user)

    if scope == "cards":
        return {"scope": scope, "cards": {
            name: cfg.get("type") for name, cfg in sorted(_cards(user).items())}}

    if scope == "pending":
        rows = db.pending_expenses(user["id"])
        return {"scope": scope, "count": len(rows),
                "expenses": [_slim(r) for r in rows],
                "note": "each of these needs a category from the user"}

    if scope == "owed":
        rows = [r for r in db.expenses_between(
            user["id"], today - timedelta(days=365), today + timedelta(days=1))
            if float(r["owed_amount"]) > 0]
        return {"scope": scope, "count": len(rows),
                "outstanding_total": str(_money(db.outstanding_owed(user["id"]))),
                "expenses": [_slim(r) for r in rows]}

    if scope == "week":
        start, end = db.week_start(user), today + timedelta(days=1)
    elif scope == "month" or scope.startswith("category:"):
        start, end = today.replace(day=1), today + timedelta(days=1)
    else:
        scope, start, end = "today", today, today + timedelta(days=1)

    rows = db.expenses_between(user["id"], start, end)
    if scope.startswith("category:"):
        wanted = scope.partition(":")[2]
        if wanted not in CATEGORIES:
            return {"error": "unknown category", "category": wanted,
                    "allowed": list(CATEGORIES)}
        rows = [r for r in rows if r["category"] == wanted]

    payments = [r for r in rows if r["kind"] == "cc_payment"]
    return {
        "scope": scope, "from": start.isoformat(), "count": len(rows),
        "spent_total": _total(rows),
        "by_category": _by_category(rows),
        # Listed, never added in. The bill settles swipes already counted (E7).
        "card_payments_excluded_from_total": [_slim(r) for r in payments],
        "expenses": [_slim(r) for r in rows],
    }


@beta_tool
def card_summary(card: str, period: str = "month") -> dict:
    """What has been captured on one card, and how it compares to the bill.

    The delta is a SIGNAL, not an error: posting lag, tips settling higher than
    the alert, fees the bank never emails about, and refunds all move it
    legitimately. A small, stable delta is normal. A large or growing one means
    alerts are not firing for some transactions — check the bank's alert
    settings before suspecting anything here.

    Args:
        card: The card nickname, exactly as it appears in the user's card list.
        period: "month" for the calendar month, or "statement" to use the
            card's statement close day when one is configured.
    """
    user = ctx.user()
    card, err = _check_card(card, user)
    if err:
        return err
    config = _cards(user).get(card, {})
    today = db.local_today(user)

    closes = config.get("closes") if period == "statement" else None
    if closes:
        # The cycle that is currently running: from the last close to the next.
        day = min(int(closes), 28)
        start = (today.replace(day=day) if today.day > day
                 else (today.replace(day=1) - timedelta(days=1)).replace(day=day))
        end = today + timedelta(days=1)
    else:
        start, end = today.replace(day=1), today + timedelta(days=1)

    rows = [r for r in db.expenses_between(user["id"], start, end)
            if r["card"] == card]
    spend = [r for r in rows if r["kind"] == "spend"]
    paid = [r for r in rows if r["kind"] == "cc_payment"]
    captured = _money(sum(Decimal(str(r["amount"])) for r in spend) or 0)
    billed = _money(sum(Decimal(str(r["amount"])) for r in paid) or 0)

    out = {
        "card": card, "type": config.get("type"), "period": period,
        "from": start.isoformat(),
        "captured_spend": str(captured),
        "bill_payments": str(billed),
        "transactions": len(spend),
    }
    if config.get("type") == "credit" and paid:
        out["delta"] = str(billed - captured)
        out["delta_note"] = ("a signal, not an error — posting lag, tips, fees "
                             "and refunds all move it legitimately")
    elif config.get("type") != "credit":
        out["note"] = "no bill to reconcile against — this is not a credit card"
    if period == "statement" and not closes:
        out["note"] = ("no statement close day configured for this card; "
                       "using the calendar month instead")
    return out


@beta_tool
def list_applications(status: str = "all") -> dict:
    """The user's tracked job applications. These are filled in automatically
    from their email — the user does not add them by hand.

    Args:
        status: "all", "applied" (still open, no decision yet), or "rejected".
    """
    user = ctx.user()
    rows = db.applications(user["id"], None if status == "all" else status)
    return {"status": status, "count": len(rows), "applications": [
        {k: r[k] for k in ("id", "company", "role", "status", "last_email_at")}
        for r in rows]}


# --- the 21:00 digest (E3, E4) ----------------------------------------------
#
# Composed in code. No model call, so it is free, instant, and incapable of
# misquoting an amount — A6's argument about the budget applies unchanged to
# money. The model is involved only when the user REPLIES to it.

def digest(user: dict) -> str | None:
    """Today's money, or None when there is nothing to say.

    Returning None matters: an assistant that messages you to report that
    nothing happened is one you mute, and then you miss the day it matters.
    """
    today = db.local_today(user)
    rows = db.expenses_between(user["id"], today, today + timedelta(days=1))
    pending = db.pending_expenses(user["id"])
    owed = db.outstanding_owed(user["id"])
    confirmed = [r for r in rows if r["status"] != "pending"]

    if not confirmed and not pending:
        return None

    lines = []
    spent = _total(confirmed)
    if confirmed:
        lines.append(f"Today: {spent}")
        for r in confirmed:
            bits = [f"  {r['amount']}", r["merchant"] or "?"]
            if r["kind"] == "cc_payment":
                bits.append(f"card payment · {r['card'] or 'unknown card'}")
            else:
                bits.append(r["category"] or "no category")
                if r["card"]:
                    bits.append(r["card"])
            lines.append(" · ".join(bits))

    if pending:
        # Oldest first, so anything that has been sitting for days leads — a
        # pending row counts toward your totals with no category forever, and
        # it will not nag you twice (E4).
        stale = [p for p in pending
                 if (today - date.fromisoformat(p["spent_at"][:10])).days >= 3]
        lines.append("")
        lines.append(f"{len(pending)} waiting on you"
                     + (f", {len(stale)} for 3 days or more" if stale else "") + ":")
        for r in sorted(pending, key=lambda r: r["spent_at"]):
            lines.append(f"  {r['amount']} · {r['merchant'] or '?'} · "
                         f"{r['card'] or 'unknown card'} · what was this?")

    if owed:
        lines.append("")
        lines.append(f"Owed to you: {owed}")

    audit.step("decision", name="digest", **{
        "in": {"confirmed": len(confirmed), "pending": len(pending), "owed": owed},
        "out": {"spent_total": spent}})
    return "\n".join(lines)


TOOLS = [log_expense, update_expense, settle_split, list_expenses,
         card_summary, list_applications]
