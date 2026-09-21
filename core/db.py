"""Supabase client, the five reads that matter, and the one definition of "today".

Every notion of a day comes from users.timezone via local_today(). Nothing
anywhere asks the system for a naive local date — that would compute the wrong
day for several hours each night and the right one the rest of the time, which
is the worst failure mode there is. test_flow.py greps for it.
"""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from supabase import Client, create_client

OPEN = ("todo", "in progress")

_sb: Client | None = None


def sb() -> Client:
    global _sb
    if _sb is None:
        _sb = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],  # service_role — bypasses RLS (A5)
        )
    return _sb


# --- time -------------------------------------------------------------------

def local_today(user: dict) -> date:
    return datetime.now(ZoneInfo(user["timezone"])).date()


def week_start_of(d: date) -> date:
    """Monday 00:00 (A4). Not now() - 7 days: a task planned Sunday and one
    planned Monday belong to different weeks, and this is where that is true."""
    return d - timedelta(days=d.weekday())


def week_start(user: dict) -> date:
    return week_start_of(local_today(user))


# --- the five reads ---------------------------------------------------------

def get_user_by_channel(channel: str, channel_user_id: str) -> dict | None:
    r = (sb().table("users").select("*")
         .eq("channel", channel).eq("channel_user_id", channel_user_id)
         .limit(1).execute())
    return r.data[0] if r.data else None


def open_tasks(user_id: str, planned_on_or_before: date | None = None) -> list[dict]:
    """The rollover query. Must stay an Index Scan on tasks_open_planned_idx."""
    q = sb().table("tasks").select("*").eq("user_id", user_id).in_("status", OPEN)
    if planned_on_or_before:
        q = q.lte("planned_on", planned_on_or_before.isoformat())
    return q.order("planned_on").execute().data


def unplanned_tasks(user_id: str) -> list[dict]:
    """The inbox: captured and never given a day. This is the pile that rots."""
    return (sb().table("tasks").select("*")
            .eq("user_id", user_id).eq("status", "todo").is_("planned_on", "null")
            .order("created_at").execute().data)


def week_minutes_used(user_id: str, week_start_on: date) -> int:
    """Committed time this week. Done counts — time spent is spent. Captured-but-
    unscheduled never counts, or capture would inflate the week (D8/D9)."""
    rows = (sb().table("tasks").select("estimate_min")
            .eq("user_id", user_id)
            .neq("status", "dropped")
            .gte("planned_on", week_start_on.isoformat())
            .lt("planned_on", (week_start_on + timedelta(days=7)).isoformat())
            .execute().data)
    return sum(r["estimate_min"] for r in rows)


def expenses_between(user_id: str, start: date, end: date) -> list[dict]:
    """Rows in [start, end). The digest, the breakdown and every total read
    through here — one index scan on expenses_month_idx.

    Deleted rows are excluded; cc_payment and transfer rows are NOT. They are
    real rows the digest lists, and it is the `kind` filter at the point of
    summing that keeps them out of spending totals (E7)."""
    return (sb().table("expenses").select("*")
            .eq("user_id", user_id).neq("status", "deleted")
            .gte("spent_at", start.isoformat())
            .lt("spent_at", end.isoformat())
            .order("spent_at").execute().data)


def outstanding_owed(user_id: str) -> float:
    rows = (sb().table("expenses").select("owed_amount")
            .eq("user_id", user_id).neq("status", "deleted")
            .gt("owed_amount", 0).execute().data)
    return round(sum(float(r["owed_amount"]) for r in rows), 2)


def pending_expenses(user_id: str) -> list[dict]:
    """Email-extracted rows whose amount counts but whose meaning you have not
    supplied yet (E2). The digest asks about these."""
    return (sb().table("expenses").select("*")
            .eq("user_id", user_id).eq("status", "pending")
            .order("spent_at").execute().data)


def recent_chat_expense(user_id: str, amount: str, within_min: int = 60) -> dict | None:
    """E2, email direction: did you already tell it about this spend?

    An email whose amount matches a chat row from the last hour adds nothing —
    you said it first. 60 minutes is the one place a time window is the right
    tool: a bank alert lands within about an hour of the swipe.
    """
    since = datetime.now(ZoneInfo("UTC")) - timedelta(minutes=within_min)
    rows = (sb().table("expenses").select("*")
            .eq("user_id", user_id).eq("source", "chat").eq("amount", amount)
            .neq("status", "deleted")
            .gte("created_at", since.isoformat()).execute().data)
    return rows[0] if rows else None


def pending_match(user_id: str, amount: str, on: date) -> list[dict]:
    """E2, chat direction: is this spend already sitting here as a pending row?

    Returns every match, because the count decides the behaviour — one attaches,
    two means the digest asks rather than guessing between two real charges.
    """
    return (sb().table("expenses").select("*")
            .eq("user_id", user_id).eq("status", "pending").eq("amount", amount)
            .gte("spent_at", on.isoformat())
            .lt("spent_at", (on + timedelta(days=1)).isoformat())
            .execute().data)


def applications(user_id: str, status: str | None = None) -> list[dict]:
    q = (sb().table("applications").select("*")
         .eq("user_id", user_id).order("last_email_at", desc=True))
    if status:
        q = q.eq("status", status)
    return q.execute().data


def recent_messages(user_id: str, limit: int = 20) -> list[dict]:
    rows = (sb().table("messages").select("role,content,meta")
            .eq("user_id", user_id).order("created_at", desc=True)
            .limit(limit).execute().data)
    return list(reversed(rows))


# --- writes -----------------------------------------------------------------

def save_message(user_id: str, role: str, content: str,
                 channel_msg_id: str | None = None, meta: dict | None = None) -> bool:
    """False means this channel_msg_id was already processed — a webhook retry.

    The uniqueness is a database guarantee (messages_dedupe_idx), not code that
    has to remember to be careful.
    """
    try:
        sb().table("messages").insert({
            "user_id": user_id, "role": role, "content": content,
            "channel_msg_id": channel_msg_id, "meta": meta or {},
        }).execute()
        return True
    except Exception as e:
        if "23505" in str(e) or "duplicate key" in str(e):
            return False
        raise


def seen_message_ids(user_id: str, ids: list[str]) -> set[str]:
    """Which of these Gmail ids have already been handled (Phase 1b).

    One query for the whole window rather than one per message: the poll lists
    two days of mail every hour and almost all of it is already done, so this
    is what keeps the steady state at a handful of messages.get calls.
    """
    if not ids:
        return set()
    rows = (sb().table("email_events").select("gmail_message_id")
            .eq("user_id", user_id).in_("gmail_message_id", ids).execute().data)
    return {r["gmail_message_id"] for r in rows}


def record_email_event(user_id: str, gmail_message_id: str, lane: str | None,
                       outcome: str, sender: str, subject: str,
                       detail: dict) -> bool:
    """False means this message was already recorded — the same guarantee
    save_message() gives, from the same kind of unique index
    (email_events_dedupe_idx).

    `detail` carries extracted scalars only. The body never comes near this
    function; evaluation_plan.md §4 is a trust boundary, not a preference.
    """
    try:
        sb().table("email_events").insert({
            "user_id": user_id, "gmail_message_id": gmail_message_id,
            "lane": lane, "outcome": outcome,
            "sender": sender[:400], "subject": (subject or "")[:400],
            "detail": detail,
        }).execute()
        return True
    except Exception as e:
        if "23505" in str(e) or "duplicate key" in str(e):
            return False
        raise


def has_activity_today(user: dict) -> bool:
    """Did the day already open itself? The 11am check-in writes a system
    message, so it self-guards against firing twice (D6)."""
    start = datetime.combine(local_today(user), datetime.min.time(),
                             tzinfo=ZoneInfo(user["timezone"]))
    r = (sb().table("messages").select("id")
         .eq("user_id", user["id"]).in_("role", ["user", "system"])
         .gte("created_at", start.isoformat()).limit(1).execute())
    return bool(r.data)


def mark_digest_sent(user: dict, on: date) -> None:
    """The once-a-day guard for the expense digest, flipped whether or not
    there was anything to say — a quiet day must not leave the door open for
    the next hour's tick to try again."""
    sb().table("users").update({"last_digest_on": on.isoformat()}).eq(
        "id", user["id"]).execute()
    user["last_digest_on"] = on.isoformat()


def all_users() -> list[dict]:
    return sb().table("users").select("*").execute().data
