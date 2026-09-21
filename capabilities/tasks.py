"""Tasks — Phase 1a. Six tools, no more.

A capability is one file (D3): TOOLS is required, PROMPT is optional, and core
never learns this module's name.
"""

import calendar as _cal
import re
from datetime import date, datetime, timedelta, timezone

from core.tool import tool as beta_tool

from core import audit, ctx, db
from core.channels.base import Button

from . import calendar

# Rendered on every task card. D11 caps this at 3 labels of <= 20 chars.
CARD_BUTTONS = (("Done", "d"), ("Tomorrow", "t"), ("Drop", "x"))

# ponytail: a burst of cards is worse than none. Above this the model's text
# carries the list and bulk triage takes over (D8).
MAX_CARDS = 5

PROMPT = """\
TASKS
- estimate_min: quick 5, short 25, deep 90. Infer it; "it's quick" means 5.
- category: money | admin | work | study | health | life. Infer it, don't ask.
- due_on is the world's deadline; planned_on is the day the user intends to do
  it. "pay the bill by Friday" sets due_on, not planned_on.
- deadline_hard is true only when missing it has a real consequence (a bill, an
  exam, a flight) — not for self-imposed dates.
- Never ask for a field you can reasonably infer. One short confirmation line.
- Task ids are uuids: never invent one, only use ids a tool returned.

RECURRING REMINDERS
- recur makes a task come back after it is done: "daily", "weekly:mon",
  "monthly:15", "monthly:last", "yearly:03-15". Set it whenever the user says
  every / each / monthly / annually — "cancel the trial every month on the 3rd"
  is recur="monthly:3", not three separate tasks.
- Always set planned_on to the FIRST occurrence when you set recur. The task
  moves itself forward from there; never add future copies by hand.
- A recurring bill or subscription is category money and usually
  deadline_hard — missing it costs real money.
"""

# --- recurrence -------------------------------------------------------------
#
# A recurring task is ONE row that moves forward when it is finished, leaving a
# dated copy behind as the record (migrations/005_recurring.sql). Nothing is
# materialised in advance, so there is no cron job here that could run twice.

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RECUR = re.compile(
    r"^(daily|weekly:(?:mon|tue|wed|thu|fri|sat|sun)|"
    r"monthly:(?:[1-9]|[12]\d|3[01]|last)|yearly:\d{2}-\d{2})$")


def valid_recur(rule: str | None) -> bool:
    return bool(rule) and bool(_RECUR.match(rule.strip().lower()))


def _month_day(year: int, month: int, day: int | str) -> date:
    """Clamp to the month's real length. 'monthly:31' in February is the 28th
    (or 29th) — the alternative is skipping February entirely, which for a bill
    reminder is the expensive kind of wrong."""
    last = _cal.monthrange(year, month)[1]
    return date(year, month, last if day == "last" else min(int(day), last))


def next_occurrence(rule: str | None, after: date) -> date | None:
    """The next date strictly after `after`, or None if the rule is unusable.

    An unparseable rule is not an error: the task simply behaves as a one-off.
    A reminder that throws on completion would be worse than one that stops
    repeating, because you would find out by the task refusing to close.
    """
    if not valid_recur(rule):
        return None
    kind, _, arg = rule.strip().lower().partition(":")

    if kind == "daily":
        return after + timedelta(days=1)
    if kind == "weekly":
        ahead = (_WEEKDAYS.index(arg) - after.weekday()) % 7
        return after + timedelta(days=ahead or 7)
    if kind == "monthly":
        year, month = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
        this = _month_day(after.year, after.month, arg)
        # Set on the 25th but completed on the 3rd? The 25th is still ahead.
        return this if this > after else _month_day(year, month, arg)
    month, _, day = arg.partition("-")
    this = _month_day(after.year, int(month), int(day))
    return this if this > after else _month_day(after.year + 1, int(month), int(day))


def render(task: dict) -> None:
    when = task.get("planned_on") or task.get("due_on")
    line = f"{task['title']}" + (f" · {when}" if when else "")
    ctx.card(line, [Button(label, f"{key}:{task['id']}") for label, key in CARD_BUTTONS])


def _slim(task: dict) -> dict:
    """What the model needs. Sending whole rows back wastes the context window."""
    return {k: task[k] for k in
            ("id", "title", "status", "planned_on", "due_on", "deadline_hard",
             "estimate_min", "priority_level", "category", "slip_count")}


@beta_tool
def add_task(title: str, planned_on: str | None = None, planned_at: str | None = None,
             due_on: str | None = None, deadline_hard: bool = False, estimate_min: int = 25,
             priority_level: str = "normal", category: str | None = None,
             notes: str | None = None, recur: str | None = None) -> dict:
    """Add a task. Capture is never blocked — call this first, discuss after.

    Args:
        title: The task, in the user's own words, trimmed.
        planned_on: YYYY-MM-DD, the day the user intends to DO it. Omit unless
            they said when they'd do it. Setting this puts it on their calendar.
        planned_at: HH:MM in 24-hour local time, only when the user names a
            time ("gym at 7", "call at 14:30"). Makes it a timed block of
            estimate_min minutes instead of an all-day entry.
        due_on: YYYY-MM-DD, the world's deadline. "by Friday", "due the 3rd".
        deadline_hard: True when missing the date has a real consequence.
        estimate_min: Minutes of real work. quick 5, short 25, deep 90.
        priority_level: high | normal | low.
        category: money | admin | work | study | health | life.
        notes: Anything said that does not belong in the title.
        recur: Makes it a repeating reminder: "daily", "weekly:mon",
            "monthly:15", "monthly:last", or "yearly:03-15". Set planned_on to
            the first occurrence as well. The task moves itself forward each
            time it is completed — never add future copies by hand.
    """
    if recur and not valid_recur(recur):
        # Refusing is better than silently storing a rule that never fires: the
        # user would believe the reminder exists and hear nothing again.
        return {"error": "unusable recur rule", "recur": recur,
                "allowed": "daily | weekly:mon | monthly:15 | monthly:last | yearly:03-15"}
    row = db.sb().table("tasks").insert({
        "user_id": ctx.user()["id"], "title": title,
        "planned_on": planned_on, "due_on": due_on, "deadline_hard": deadline_hard,
        "estimate_min": estimate_min, "priority_level": priority_level,
        "category": category, "notes": notes, "recur": recur,
        "source": ctx.source(),
        "meta": {"planned_at": planned_at} if planned_at else {},
    }).execute().data[0]
    calendar.sync(row, ctx.user()["timezone"])
    render(row)
    return _slim(row)


@beta_tool
def list_tasks(scope: str) -> dict:
    """List tasks.

    Args:
        scope: One of "today" (planned for today or earlier and still open),
            "open" (everything still open), "unplanned" (captured, never given
            a day), "overdue" (past its due_on and still open).
    """
    user = ctx.user()
    today = db.local_today(user)
    if scope == "today":
        rows = db.open_tasks(user["id"], planned_on_or_before=today)
    elif scope == "unplanned":
        rows = db.unplanned_tasks(user["id"])
    elif scope == "overdue":
        rows = [t for t in db.open_tasks(user["id"])
                if t["due_on"] and date.fromisoformat(t["due_on"]) < today]
    else:
        rows = db.open_tasks(user["id"])

    if len(rows) <= MAX_CARDS:
        for t in rows:
            render(t)
    return {"scope": scope, "count": len(rows), "tasks": [_slim(t) for t in rows]}


def _update(task_id: str, fields: dict) -> dict:
    """Every update funnels through here, which is why the calendar sync lives
    here and not in each of the five callers."""
    r = (db.sb().table("tasks").update(fields)
         .eq("id", task_id).eq("user_id", ctx.user()["id"]).execute())
    if not r.data:
        return {"error": "no such task for this user", "task_id": task_id}
    calendar.sync(r.data[0], ctx.user()["timezone"])
    return _slim(r.data[0])


@beta_tool
def update_task(task_id: str, title: str | None = None, planned_on: str | None = None,
                planned_at: str | None = None,
                due_on: str | None = None, deadline_hard: bool | None = None,
                estimate_min: int | None = None, priority_level: str | None = None,
                category: str | None = None, notes: str | None = None) -> dict:
    """Change fields on an existing task. Only pass what changes.

    Args:
        task_id: The uuid of the task, from a previous tool result.
        title: New title.
        planned_on: YYYY-MM-DD. To move a task to another day prefer
            reschedule_task, which records the slip.
        planned_at: HH:MM local, the time of day. Pass "" to clear it and make
            the task an all-day entry again.
        due_on: YYYY-MM-DD.
        deadline_hard: Whether missing the date has a real consequence.
        estimate_min: Corrected estimate, e.g. "that's a 40 minute job".
        priority_level: high | normal | low.
        category: money | admin | work | study | health | life.
        notes: Replaces existing notes.
    """
    given = (("title", title), ("planned_on", planned_on), ("due_on", due_on),
             ("deadline_hard", deadline_hard), ("estimate_min", estimate_min),
             ("priority_level", priority_level), ("category", category), ("notes", notes))
    fields = {k: v for k, v in given if v is not None}
    if planned_at is not None:
        # meta is jsonb: read-modify-write, folded into the same update so one
        # edit is one round trip and one calendar sync.
        cur = (db.sb().table("tasks").select("meta")
               .eq("id", task_id).eq("user_id", ctx.user()["id"]).execute().data)
        if not cur:
            return {"error": "no such task for this user", "task_id": task_id}
        fields["meta"] = {**(cur[0]["meta"] or {}), "planned_at": planned_at or None}
    return _update(task_id, fields) if fields else {"error": "nothing to update"}


def complete(task_id: str, user_id: str, tz: str) -> dict | None:
    """Finish a task. The ONE place completion happens, because a recurring
    task finishing is not an update — it is a write and a roll, and the Done
    button (D4) must do exactly what the tool does.

    A recurring task leaves a dated `done` copy behind and moves itself to its
    next occurrence. Without the copy there would be no record you ever paid
    August's bill; without the roll you would have to re-add the reminder every
    month, which is the thing you asked not to do.

    Returns the row as the user should hear about it, or None if nothing
    matched — a second tap of Done, or someone else's task.
    """
    cur = (db.sb().table("tasks").select("*")
           .eq("id", task_id).eq("user_id", user_id)
           .in_("status", db.OPEN).execute().data)
    if not cur:
        return None
    task = cur[0]
    now = datetime.now(timezone.utc).isoformat()

    nxt = next_occurrence(task.get("recur"),
                          date.fromisoformat(task["planned_on"])
                          if task["planned_on"] else db.local_today({"timezone": tz}))
    if not nxt:
        r = (db.sb().table("tasks")
             .update({"status": "done", "completed_at": now})
             .eq("id", task_id).eq("user_id", user_id)
             .in_("status", db.OPEN).execute())
        if not r.data:
            return None
        calendar.sync(r.data[0], tz)
        return r.data[0]

    # The record of this occurrence. recur is null on the copy so the history
    # can never roll itself forward a second time.
    db.sb().table("tasks").insert({
        **{k: task[k] for k in ("user_id", "title", "notes", "planned_on",
                                "due_on", "deadline_hard", "estimate_min",
                                "priority_level", "category", "source")},
        "status": "done", "completed_at": now, "recur": None,
        "meta": {"recurred_from": task["id"]},
    }).execute()

    # The live row moves on. slip_count resets: a reminder that has come round
    # again is not a task you have been avoiding, and leaving the count would
    # make the assistant challenge you about a bill you pay every month.
    r = (db.sb().table("tasks").update({
        "planned_on": nxt.isoformat(), "slip_count": 0,
        "meta": {k: v for k, v in (task["meta"] or {}).items()
                 if k not in ("slipped_on", "challenged")},
    }).eq("id", task_id).eq("user_id", user_id).execute())
    audit.step("write", table="tasks", op="recur", row=task_id, next=nxt.isoformat())
    calendar.sync(r.data[0], tz)
    return {**r.data[0], "recurred_to": nxt.isoformat()}


@beta_tool
def complete_task(task_id: str) -> dict:
    """Mark a task done. A recurring reminder rolls to its next date instead of
    closing, and says so.

    Args:
        task_id: The uuid of the task.
    """
    row = complete(task_id, ctx.user()["id"], ctx.user()["timezone"])
    if not row:
        return {"error": "no such open task for this user", "task_id": task_id}
    out = _slim(row)
    if row.get("recurred_to"):
        out["recurred_to"] = row["recurred_to"]
    return out


@beta_tool
def drop_task(task_id: str) -> dict:
    """Drop a task the user no longer intends to do. Confirm before calling
    this unless the user was explicit ("drop it", "forget it").

    Args:
        task_id: The uuid of the task.
    """
    return _update(task_id, {"status": "dropped"})


@beta_tool
def reschedule_task(task_id: str, planned_on: str) -> dict:
    """Move a task to a different day and record the slip. Use this rather than
    update_task whenever a task moves — slip_count is how the assistant notices
    something is not actually happening.

    Args:
        task_id: The uuid of the task.
        planned_on: YYYY-MM-DD, the new intended day.
    """
    cur = (db.sb().table("tasks").select("slip_count")
           .eq("id", task_id).eq("user_id", ctx.user()["id"]).execute().data)
    if not cur:
        return {"error": "no such task for this user", "task_id": task_id}
    return _update(task_id, {"planned_on": planned_on,
                             "slip_count": cur[0]["slip_count"] + 1})


def roll_to(task_id: str, user_id: str, when: date) -> dict | None:
    """The Tomorrow button (D4). Same write as reschedule_task, no model."""
    # Only an OPEN task can slip. Without that, a second tap of Tomorrow — two
    # different callback ids, so the dedupe never sees it — counts a second
    # slip, and the assistant starts challenging you about a task you moved once.
    cur = (db.sb().table("tasks").select("slip_count")
           .eq("id", task_id).eq("user_id", user_id).in_("status", db.OPEN).execute().data)
    if not cur:
        return None
    r = (db.sb().table("tasks")
         .update({"planned_on": when.isoformat(), "slip_count": cur[0]["slip_count"] + 1})
         .eq("id", task_id).eq("user_id", user_id).in_("status", db.OPEN).execute())
    if r.data:
        calendar.sync(r.data[0], ctx.user()["timezone"])
    return r.data[0] if r.data else None


TOOLS = [add_task, list_tasks, update_task, complete_task, drop_task, reschedule_task]
