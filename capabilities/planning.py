"""Planning — rollover, capacity, the nag (D8, D9).

Mostly a PROMPT plus one read-only tool, and that split is the point:
**rules live in the prompt, arithmetic lives in code.** A model should not be
doing subtraction on your week.

Three guarantees are enforced here rather than asked of the model, because a
model's judgement about "have I already nagged today" is not a guarantee:
  - at most one nudge per day          -> users.last_nudge_on
  - a slipping task is challenged once -> tasks.meta.challenged
  - over 8 unreconciled is ONE message -> the bulk card below, not the model
"""

from datetime import date

from core.tool import tool as beta_tool

from core import audit, ctx, db
from core.channels.base import Button

BULK_THRESHOLD = 8

PROMPT = """\
PLANNING
Call day_status at the start of any turn where the user is PLANNING — asking
what to do, committing something to today, or the morning check-in. Do not call
it for a bare capture ("add: ..."); an 11pm capture starts no morning ritual.

Rules, in order of how badly it hurts to get them wrong:
1. Capture is never blocked and never lectured. add_task first, always. Any
   reconciliation or capacity remark comes AFTER the confirmation line, never
   instead of it.
2. If day_status returns should_nudge, you may add exactly ONE short line about
   what is still open from earlier days. If it is false, say nothing about it.
3. reconcile lists what is still open from before today. Offer Done / move /
   drop per task. If day_status says bulk_triage_sent, the triage message has
   already been sent — do not list the tasks again.
4. For each task in slipping, challenge it ONCE: name how many days it has
   moved and ask whether it is real or should be dropped. Then leave it alone.
5. A hard deadline goes on the day the user asked for even when week_is_full.
   Add it first, then say the week is heavy and name the one soft task that
   could move.
6. A soft task onto a full week: add it to the list, then offer today rather
   than assume it — "park it, or swap something out?".

week_is_full is a boolean and it is all you get. Never state, estimate, or
imply minutes, hours, totals or remainders — not "4h 20m left", not "about two
hours spare". You may say the week is heavy. You may not do its arithmetic
out loud.

Replies are plain text. No markdown, no asterisks, no bullets characters.
Short. One or two lines is usually right.
"""


def _bulk_card(n: int) -> None:
    ctx.card(
        f"{n} still open from before today.",
        [Button("Keep all", "ka"), Button("Drop all", "da"), Button("One by one", "ob")],
    )


@beta_tool
def day_status() -> dict:
    """Today's planning picture: what is committed for today, what is still
    open from earlier days, which tasks keep slipping, and whether the week is
    full. Call this before planning anything. Read-only from the user's point
    of view — it changes no task the user can see."""
    user = ctx.user()
    today = db.local_today(user)
    open_now = db.open_tasks(user["id"])

    committed, stale = [], []
    for t in open_now:
        if not t["planned_on"]:
            continue
        planned = date.fromisoformat(t["planned_on"])
        if planned == today:
            committed.append(t)
        elif planned < today:
            stale.append(t)

    # Daily slip sweep — a task left sitting on a past day is slipping whether
    # or not anyone rescheduled it. Once per day per task, guarded in meta.
    slipping = []
    for t in stale:
        meta = t["meta"] or {}
        if meta.get("slipped_on") != today.isoformat():
            t["slip_count"] += 1
            meta["slipped_on"] = today.isoformat()
            db.sb().table("tasks").update(
                {"slip_count": t["slip_count"], "meta": meta}
            ).eq("id", t["id"]).execute()
        if t["slip_count"] >= user["slip_threshold"] and not meta.get("challenged"):
            meta["challenged"] = True
            db.sb().table("tasks").update({"meta": meta}).eq("id", t["id"]).execute()
            slipping.append({"id": t["id"], "title": t["title"],
                             "slip_count": t["slip_count"]})

    # Over 8, the list is one message with three buttons — never twelve cards.
    bulk = len(stale) > BULK_THRESHOLD
    if bulk:
        _bulk_card(len(stale))

    # The nag guard. Flipping it here is what makes "five captures produce one
    # nudge" true regardless of what the model decides to say.
    nudged_on = user["last_nudge_on"]          # the value that decided it, pre-flip
    should_nudge = bool(stale) and nudged_on != today.isoformat()
    if should_nudge:
        db.sb().table("users").update(
            {"last_nudge_on": today.isoformat()}).eq("id", user["id"]).execute()
        user["last_nudge_on"] = today.isoformat()

    used = db.week_minutes_used(user["id"], db.week_start(user))

    # A6: minutes decide when to speak. They are not part of the answer.
    out = {
        "today": today.isoformat(),
        "committed_today": [{"id": t["id"], "title": t["title"],
                             "priority_level": t["priority_level"]} for t in committed],
        "reconcile": [] if bulk else [
            {"id": t["id"], "title": t["title"], "planned_on": t["planned_on"]}
            for t in stale],
        "unreconciled_count": len(stale),
        "bulk_triage_sent": bulk,
        "slipping": slipping,
        "should_nudge": should_nudge,
        "week_is_full": used >= user["weekly_budget_min"],
        "unplanned_count": len(db.unplanned_tasks(user["id"])),
    }
    # A6 hides the minutes from you, which also hides them from the debugger.
    # When it says "that's a full week" and you disagree, this is the only
    # record of whether the model misread the boolean or the boolean was wrong.
    audit.step("decision", name="day_status", **{
        "in": {"used_min": used, "budget_min": user["weekly_budget_min"],
               "last_nudge_on": nudged_on, "slip_threshold": user["slip_threshold"],
               "stale": len(stale)},
        "out": out})
    return out


TOOLS = [day_status]
