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

OPEN = ("todo", "doing")

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


def recent_messages(user_id: str, limit: int = 20) -> list[dict]:
    rows = (sb().table("messages").select("role,content")
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


def has_activity_today(user: dict) -> bool:
    """Did the day already open itself? The 11am check-in writes a system
    message, so it self-guards against firing twice (D6)."""
    start = datetime.combine(local_today(user), datetime.min.time(),
                             tzinfo=ZoneInfo(user["timezone"]))
    r = (sb().table("messages").select("id")
         .eq("user_id", user["id"]).in_("role", ["user", "system"])
         .gte("created_at", start.isoformat()).limit(1).execute())
    return bool(r.data)


def all_users() -> list[dict]:
    return sb().table("users").select("*").execute().data
