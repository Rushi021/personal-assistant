"""The checks the plan leaves behind, written in the step that created the
behaviour rather than in a hardening pass at the end.

    python test_flow.py          # or: pytest test_flow.py

Structural checks run anywhere and need nothing. The live ones need a real
Supabase and a real key; without them they raise SkipTest, which pytest reports
as a skip and the runner below prints as SKIP. A skipped check is not a passing
check — run them once the schema is up.
"""

import os
import re
import subprocess
import sys
from datetime import date, timedelta
from unittest import SkipTest

ROOT = os.path.dirname(os.path.abspath(__file__))
# The live checks delete every task and message belonging to TEST_CHAT_ID
# before each one. Point that at a SECOND users row, not the one you use, and
# opt in on purpose — a test run must never be able to eat your real list.
LIVE = all(os.environ.get(k) for k in
           ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GEMINI_API_KEY",
            "TEST_CHAT_ID", "ALLOW_DESTRUCTIVE_TESTS"))
WHY_NOT = ("set SUPABASE_URL, SUPABASE_SERVICE_KEY, GEMINI_API_KEY, "
           "TEST_CHAT_ID and ALLOW_DESTRUCTIVE_TESTS=1")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _grep(pattern, *paths):
    r = subprocess.run(["grep", "-rn", pattern, *paths], cwd=ROOT,
                       capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if ln]


def _user():
    from core import db
    u = db.get_user_by_channel("telegram", os.environ["TEST_CHAT_ID"])
    assert u, "seed your user row in migrations/001_init.sql first"
    return u


def _fresh(user):
    """Every live check starts from a clean slate for this user."""
    from core import db
    assert os.environ.get("ALLOW_DESTRUCTIVE_TESTS") == "1", "refusing to delete"
    db.sb().table("tasks").delete().eq("user_id", user["id"]).execute()
    db.sb().table("messages").delete().eq("user_id", user["id"]).execute()
    db.sb().table("users").update({"last_nudge_on": None}).eq("id", user["id"]).execute()
    user["last_nudge_on"] = None


# --- Step 0: the channel seam (D11) -----------------------------------------

def test_channel_seam_is_intact():
    """A single hit means the channel leaked into the logic and the WhatsApp
    swap stops being a day's work."""
    hits = subprocess.run(["grep", "-rni", "telegram", "core/turn.py", "capabilities/"],
                          cwd=ROOT, capture_output=True, text=True).stdout
    assert not hits.strip(), f"channel leaked into the logic:\n{hits}"


def test_replies_are_plain_and_never_edited():
    """D11's two unretrofittable rules, checked where they would be broken."""
    tg = _read("core", "channels", "telegram.py")
    for banned in ("parse_mode", "editMessageText"):
        assert banned not in tg.replace(f"# {banned}", ""), f"{banned} is a WhatsApp dead end"


def test_turn_stays_small():
    """Under 60 lines of actual code. Counted without comments or docstrings so
    that documenting the file cannot break the guard and padding it cannot hide
    inside prose — the rule is about logic leaking out of a capability, and
    audit instrumentation is not logic."""
    src = _read("core", "turn.py")
    body = re.sub(r'(?s)\"\"\".*?\"\"\"', "", src)
    n = len([ln for ln in body.splitlines()
             if ln.strip() and not ln.strip().startswith("#")])
    assert n < 60, f"core/turn.py is {n} code lines — something belongs in a capability"


def test_nothing_asks_the_system_for_a_naive_date():
    """Every notion of "today" goes through local_today(user)."""
    assert not _grep("date.today()", "core/", "capabilities/", "app.py")


# --- Step 1: schema and the query that grows --------------------------------

def test_rollover_query_uses_index():
    """The one query that runs every morning. Seq Scan here is the thing that
    gets slow in month three and is invisible in week one."""
    if not LIVE:
        raise SkipTest(WHY_NOT)
    from core import db
    user = _user()
    q = (db.sb().table("tasks").select("*").eq("user_id", user["id"])
         .in_("status", db.OPEN).lte("planned_on", db.local_today(user).isoformat()))
    if not hasattr(q, "explain"):
        raise SkipTest("supabase-py without .explain(); run EXPLAIN in the SQL editor")
    plan = str(q.explain())
    assert "tasks_open_planned_idx" in plan, f"not using the partial index:\n{plan}"


def test_week_boundary_is_monday_local():
    """A task planned Sunday and one planned Monday fall in different weeks."""
    from core import db
    sunday, monday = date(2026, 9, 13), date(2026, 9, 14)
    assert sunday.weekday() == 6 and monday.weekday() == 0
    assert db.week_start_of(sunday) != db.week_start_of(monday)
    assert db.week_start_of(monday) == monday
    assert db.week_start_of(sunday) == sunday - timedelta(days=6)


# --- Step 2: tasks, end to end ----------------------------------------------

def test_six_tools_each_documented_for_the_model():
    """The docstring is the tool's whole interface. The summary becomes the
    tool description and every Args line becomes a parameter description —
    an undescribed parameter is one the model guesses at."""
    from capabilities import tasks
    assert len(tasks.TOOLS) == 6, f"{len(tasks.TOOLS)} tools — six was the budget"
    for t in tasks.TOOLS:
        decl = t.to_dict()
        assert decl["description"].strip(), f"{t.name} has no description"
        for param, spec in decl.get("parameters", {}).get("properties", {}).items():
            assert spec.get("description"), f"{t.name}.{param} is undocumented"


def test_natural_language_creates_structured_task():
    """The real check: four fields set, none of them named by the user."""
    if not LIVE:
        raise SkipTest(WHY_NOT)
    from core import db
    from core.turn import handle_turn
    user = _user()
    _fresh(user)
    friday = db.local_today(user) + timedelta(days=(4 - db.local_today(user).weekday()) % 7 or 7)
    handle_turn(user, "add: pay the electricity bill friday, it's quick")
    rows = db.open_tasks(user["id"])
    assert len(rows) == 1, f"expected one task, got {len(rows)}"
    t = rows[0]
    assert t["due_on"] == friday.isoformat(), f"due_on={t['due_on']}, wanted {friday}"
    assert t["estimate_min"] <= 10, f"estimate_min={t['estimate_min']}"
    assert t["category"] == "money", f"category={t['category']}"


# --- Calendar: which tasks become events ------------------------------------

def test_only_planned_tasks_reach_the_calendar():
    """The rule, in one place: planned_on puts it on the calendar, due_on does
    not. "finish this by the end of the week" is a deadline, not a block of
    time, and scheduling it would be inventing a plan the user never agreed to."""
    from capabilities.calendar import wanted
    base = {"id": "11111111-2222-3333-4444-555555555555", "title": "x",
            "status": "todo", "meta": {}, "estimate_min": 25}

    assert not wanted({**base, "due_on": "2026-09-18"}), "a deadline is not a plan"
    assert wanted({**base, "planned_on": "2026-09-18"})
    assert not wanted({**base, "planned_on": "2026-09-18", "status": "done"})
    assert not wanted({**base, "planned_on": "2026-09-18", "status": "dropped"})
    assert not wanted({**base, "planned_on": None, "due_on": "2026-09-18"})


def test_ics_is_wellformed_and_timezone_correct():
    """A timed task is written in UTC so the file needs no VTIMEZONE block, and
    an all-day DTEND is exclusive — off-by-one here is a two-day event."""
    from capabilities import calendar
    task = {"id": "abc", "title": "gym, early; sharp", "status": "todo",
            "planned_on": "2026-09-14", "estimate_min": 60,
            "meta": {"planned_at": "07:00"}}
    ics = calendar._ics(task, "America/New_York")
    assert "DTSTART:20260914T110000Z" in ics, ics   # 07:00 EDT == 11:00 UTC
    assert "DTEND:20260914T120000Z" in ics          # + estimate_min
    assert r"SUMMARY:gym\, early\; sharp" in ics    # unescaped comma truncates
    assert ics.startswith("BEGIN:VCALENDAR") and ics.rstrip().endswith("END:VCALENDAR")

    allday = calendar._ics({**task, "meta": {}}, "America/New_York")
    assert "DTSTART;VALUE=DATE:20260914" in allday
    assert "DTEND;VALUE=DATE:20260915" in allday, "all-day DTEND must be exclusive"


def test_calendar_failure_never_breaks_a_task_write():
    """D8 extends to the write path: iCloud being down must not lose a task."""
    from capabilities import calendar
    calendar.sync({"id": "abc", "planned_on": "2026-09-14", "status": "todo",
                   "title": "x", "meta": {}, "estimate_min": 25}, "Bad/Zone")
    calendar.sync({}, "UTC")
    calendar.sync(None, "UTC")  # must not raise


# --- Step 3: the fast path (D4) ---------------------------------------------

def test_callback_payload_round_trips_and_fits():
    """<= 64 bytes is WhatsApp's ceiling; a raw uuid is 36, so a short prefix
    is all there is room for."""
    from core.channels.base import MAX_PAYLOAD, Button
    task_id = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    for label, key in (("Done", "d"), ("Tomorrow", "t"), ("Drop", "x")):
        b = Button(label, f"{key}:{task_id}")          # asserts both ceilings
        assert len(b.payload.encode()) <= MAX_PAYLOAD
        kind, _, parsed = b.payload.partition(":")
        assert kind == key and parsed == task_id


def test_fast_path_makes_no_model_call():
    """Tapping Done writes the row and spends nothing. That absence in
    messages.meta is the whole point of the step."""
    if not LIVE:
        raise SkipTest(WHY_NOT)
    from app import _fast_path
    from core import db
    user = _user()
    _fresh(user)
    t = db.sb().table("tasks").insert(
        {"user_id": user["id"], "title": "gym",
         "planned_on": db.local_today(user).isoformat()}).execute().data[0]

    out = _fast_path(user, f"d:{t['id']}", "cb:test-1")
    assert out and "gym" in out[0][0].lower()
    assert db.sb().table("tasks").select("status").eq(
        "id", t["id"]).execute().data[0]["status"] == "done"

    metas = [m for m in db.sb().table("messages").select("meta,role")
             .eq("user_id", user["id"]).execute().data if m["role"] == "assistant"]
    assert metas and all("model" not in m["meta"] for m in metas), \
        f"a model was called on the fast path: {metas}"

    # The retry guard: the same callback twice must not apply twice.
    assert _fast_path(user, f"d:{t['id']}", "cb:test-1") == []


# --- Step 4: planning (D8, D9) ----------------------------------------------

def test_budget_never_speaks():
    """A6 — day_status hands back a boolean. Minutes decide when to speak; they
    are not part of the answer, and the prompt must not ask for them."""
    from capabilities import planning
    prompt = planning.PROMPT.lower()
    for phrase in ("minutes left", "how many minutes", "report the total",
                   "state the remaining"):
        assert phrase not in prompt
    assert "week_is_full" in planning.PROMPT
    # The tool may compute with minutes; it may not hand any back. Bounded to
    # day_status's own dict: the audit step below it logs the minutes on
    # purpose, and a log is not something the assistant can say out loud.
    src = _read("capabilities", "planning.py")
    returned = src.split("    out = {", 1)[1].split("\n    }", 1)[0]
    keys = re.findall(r'^\s+"(\w+)":', returned, re.M)
    assert keys, "could not read day_status's return keys"
    for k in keys:
        assert not re.search(r"min|hour|budget|used|remain|total", k), \
            f"day_status hands back arithmetic: {k}"


def test_capture_never_blocked():
    """With ten unreconciled tasks, a bare capture still confirms first (D8)."""
    if not LIVE:
        raise SkipTest(WHY_NOT)
    from core import db
    from core.turn import handle_turn
    user = _user()
    _fresh(user)
    old = (db.local_today(user) - timedelta(days=3)).isoformat()
    db.sb().table("tasks").insert(
        [{"user_id": user["id"], "title": f"old {i}", "planned_on": old}
         for i in range(10)]).execute()

    out = handle_turn(user, "add: renew insurance")
    assert out, "capture produced no reply at all"
    assert any(t["title"].lower().startswith("renew") for t in db.open_tasks(user["id"])), \
        "the task was not created before anything was said"


def test_nag_fires_once():
    """Five captures in a row produce exactly one nudge. Guarded by
    users.last_nudge_on in code, never by the model's judgement."""
    if not LIVE:
        raise SkipTest(WHY_NOT)
    from capabilities.planning import day_status
    from core import ctx, db
    user = _user()
    _fresh(user)
    old = (db.local_today(user) - timedelta(days=2)).isoformat()
    db.sb().table("tasks").insert(
        {"user_id": user["id"], "title": "yesterday's thing", "planned_on": old}).execute()

    ctx.begin(user)
    nudges = [day_status()["should_nudge"] for _ in range(5)]
    assert nudges.count(True) == 1, f"nudged {nudges.count(True)} times: {nudges}"


def test_slip_challenge_fires_once():
    """Fires on the day the threshold is crossed and not again the next day."""
    if not LIVE:
        raise SkipTest(WHY_NOT)
    from capabilities.planning import day_status
    from core import ctx, db
    user = _user()
    _fresh(user)
    old = (db.local_today(user) - timedelta(days=4)).isoformat()
    db.sb().table("tasks").insert(
        {"user_id": user["id"], "title": "the assessment", "planned_on": old,
         "slip_count": user["slip_threshold"]}).execute()

    ctx.begin(user)
    first = day_status()["slipping"]
    assert len(first) == 1, f"challenge did not fire: {first}"
    second = day_status()["slipping"]
    assert second == [], f"challenged twice: {second}"


def test_bulk_triage_is_one_message():
    """Twelve open tasks produce one message, not twelve."""
    if not LIVE:
        raise SkipTest(WHY_NOT)
    from capabilities.planning import day_status
    from core import ctx, db
    user = _user()
    _fresh(user)
    old = (db.local_today(user) - timedelta(days=1)).isoformat()
    db.sb().table("tasks").insert(
        [{"user_id": user["id"], "title": f"t{i}", "planned_on": old} for i in range(12)]
    ).execute()

    ctx.begin(user)
    status = day_status()
    cards = ctx.drain()
    assert status["bulk_triage_sent"] is True
    assert len(cards) == 1, f"{len(cards)} messages for 12 tasks"
    assert len(cards[0][1]) == 3, "bulk triage needs exactly Keep all / Drop all / One by one"


# --- Step 5: the 11am check-in (D6, S7) -------------------------------------

def test_checkin_uses_the_same_entry_point():
    """If a second path ever appears here, the seam is gone."""
    src = _read("app.py")
    tick = src.split("async def tick", 1)[1]
    assert "handle_turn" in tick, "the cron tick stopped calling handle_turn"
    assert len(re.findall(r"\bhandle_turn\b", src)) >= 2


def test_checkin_skipped_when_already_active():
    """It does not fire on a day you messaged at 9am."""
    if not LIVE:
        raise SkipTest(WHY_NOT)
    from core import db
    user = _user()
    _fresh(user)
    assert db.has_activity_today(user) is False
    db.save_message(user["id"], "user", "morning")
    assert db.has_activity_today(user) is True


# --- The audit log (evaluation_plan.md) --------------------------------------

def test_audit_redacts_before_writing():
    """§4 is a trust boundary, not a lazy-eligible simplification. Traces are a
    second copy of your life in a table the service_role key reads freely."""
    from core import audit
    cases = {
        "key sk-ant-api03-abcdefghij123": "[api-key]",
        "a/c XX1234567 debited": "a/c x1234",
        "card 4111 1111 1111 1234": "x1234",
        "OTP is 448213": "[redacted]",
    }
    for raw, expected in cases.items():
        out = audit.scrub(raw)
        assert expected in out, f"{raw!r} -> {out!r}"
        assert not re.search(r"\d{5,}", out), f"digits survived redaction: {out!r}"
    # An amount is the point of the expense tracker and must NOT be eaten.
    assert audit.scrub("spent 56.00 at Blue Bottle") == "spent 56.00 at Blue Bottle"


def test_audit_steps_survive_the_worker_thread():
    """app.py opens the trace, then runs the turn in asyncio.to_thread, which
    COPIES the context. Steps appended in there are only visible back here
    because they mutate a dict that already existed — re-setting the ContextVar
    would silently lose every step. This is the check for that."""
    import asyncio

    from core import audit
    audit.begin({"id": "u1"}, "message", "model", "hello")

    async def run():
        await asyncio.to_thread(audit.step, "tool", name="add_task", args={"x": 1})
        await asyncio.to_thread(audit.usage, model="claude-haiku-4-5")

    asyncio.run(run())
    trace = audit._trace.get()
    assert [s["t"] for s in trace["steps"]] == ["tool"], trace["steps"]
    assert trace["model"] == "claude-haiku-4-5"
    audit._trace.set(None)


def test_audit_failure_never_costs_a_reply():
    """A logging failure must not become a user-visible one. finish() with no
    database reachable has to swallow it."""
    import logging

    from core import audit
    audit.begin({"id": "u1"}, "message", "model", "hello")
    logging.disable(logging.CRITICAL)   # the swallowed traceback is the point
    try:
        audit.finish("some reply")      # no SUPABASE_URL in a structural run
    finally:
        logging.disable(logging.NOTSET)
    assert audit._trace.get() is None
    audit.step("tool", name="orphan")   # and a step outside a turn is a no-op


def test_terminal_trace_names_every_step_and_its_output():
    """The trail is only full visibility if each step says what it IS and what
    it produced. Render one turn of every step type and check both survive."""
    from core import audit
    row = {
        "user_id": "abcd1234-0000", "trigger": "message", "path": "model",
        "turn_ref": "chat:1", "input": "buy milk", "reply": "Added Buy milk.",
        "status": "ok", "error": None, "latency_ms": 900,
        "input_tokens": 12, "output_tokens": 3, "cost_usd": 0,
        "steps": [
            {"t": "ingress", "at": 0, "kind": "text", "chat": "1"},
            {"t": "model", "at": 10, "stop": "STOP", "calls": 1},
            {"t": "tool", "at": 20, "name": "add_task",
             "args": {"title": "Buy milk"}, "ok": True, "result": {"id": "t1"}},
            {"t": "send", "at": 30, "chars": 15, "sent": True},
        ],
    }
    out = audit._render(row)
    for step in row["steps"]:
        assert audit.LABELS[step["t"]] in out, f"{step['t']} has no label"
    assert "add_task" in out and '"title": "Buy milk"' in out  # arguments
    assert '"id": "t1"' in out                                  # and the output
    assert "buy milk" in out and "Added Buy milk." in out       # in and out
    assert out.count("\n") == len(row["steps"]) + 3            # header, IN, OUT, tail


def test_missing_stt_key_says_which_key():
    """An unset GROQ_API_KEY used to surface as LocalProtocolError: Illegal
    header value b'Bearer ' — a message that names neither Groq nor the key."""
    import asyncio
    import os

    from core import stt
    before = os.environ.get("GROQ_API_KEY")
    os.environ["GROQ_API_KEY"] = ""
    try:
        asyncio.run(stt.transcribe(b"x"))
    except RuntimeError as e:
        assert "GROQ_API_KEY" in str(e), e
    else:
        raise AssertionError("an empty key was accepted")
    finally:
        if before is not None:
            os.environ["GROQ_API_KEY"] = before


def test_refused_send_is_not_a_successful_turn():
    """Telegram answers 400 with a reason and the adapter used to discard it:
    the turn recorded ok and the phone showed nothing."""
    src = _read("core", "channels", "telegram.py")
    send = src.split("async def send", 1)[1].split("async def ack", 1)[0]
    assert "raise" in send, "send() still swallows a non-200 from Telegram"


def test_a_claim_with_no_tool_behind_it_is_caught():
    """The one failure the user cannot detect: "Added." when nothing was added.
    The model does it intermittently, so the check has to be in code."""
    from core import tool
    write = [{"name": "add_task", "args": {}}]
    read = [{"name": "list_tasks", "args": {}}]
    assert tool.unbacked_claim("Added.", [])
    assert tool.unbacked_claim("Rescheduled the report to Wednesday.", read)
    assert tool.unbacked_claim("Marked it done.", read)
    assert not tool.unbacked_claim("Added.", write)          # a tool really ran
    assert not tool.unbacked_claim("You have three things today.", read)
    assert not tool.unbacked_claim("", [])


def test_running_out_of_quota_says_so():
    """The free tier runs out every day. "Try again" is a lie then — nothing
    the user does before midnight Pacific changes the answer."""
    from core import models
    assert issubclass(models.Exhausted, RuntimeError)
    assert "broke" not in models.OUT_OF_QUOTA.lower()
    src = _read("app.py")
    assert "models.Exhausted" in src, "the webhook still reports quota as a crash"


def test_every_tool_is_traced():
    """One wrapper in the registry covers all of them. A tool that escapes it
    is a tool whose arguments you cannot audit — and wrong arguments, not wrong
    sentences, are how this system actually fails."""
    from core import registry
    for tool in registry.TOOLS:
        assert tool.call.__qualname__.startswith("_traced"), \
            f"{tool.name} is not traced"


# --- runner ------------------------------------------------------------------

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = skipped = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except SkipTest as e:
            skipped += 1
            print(f"  SKIP  {name}  ({e})")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed - skipped} passed, {skipped} skipped, {failed} failed")
    sys.exit(1 if failed else 0)
