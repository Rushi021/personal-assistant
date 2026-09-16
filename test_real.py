"""Real-world scenarios against the real pipeline.

Not unit tests. Every scenario is a sentence someone would actually type at
their assistant on a Tuesday, driven through POST /webhook/telegram in the
Telegram update shape the bot really receives, and checked against the ROWS
that came out — not against the wording of the reply, which is the model's
business and changes.

It runs on an isolated user (TEST chat id, its own users row), so it can never
touch the real task list, and it captures outbound messages instead of sending
them, so a suite run does not put 40 messages on your phone.

    set -a && . ./.env && set +a && PYTHONPATH=. .venv/bin/python test_real.py
    ... A            # one phase
    ... A3 C1        # named scenarios

Free tier: core/models.py paces and rotates, so the suite slows down rather
than failing when a model runs out. AUDIT_TRACE=1 (the default) prints the full
step trail of every turn underneath each scenario.
"""

import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

import app
from core import db, models
from core.channels import telegram as channel

CHAT = os.environ.get("REAL_TEST_CHAT_ID", "999000111")   # never a real chat
TZ = "America/New_York"

sent: list[tuple] = []          # what the bot tried to put on the wire
# Telegram message ids only ever go up, and messages.channel_msg_id is unique
# forever — so a suite that restarts at 1000 collides with its own last run and
# every turn comes back "duplicate". Start each run where the clock is.
_msg_id = int(time.time())


async def _capture(to, text, buttons=None):
    sent.append((text, [b.label for b in buttons or []]))


channel.send = _capture         # the one seam a suite must not cross
client = TestClient(app.app)
H = {"x-telegram-bot-api-secret-token": os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")}


# --- driving the bot --------------------------------------------------------

def user() -> dict:
    u = db.get_user_by_channel("telegram", CHAT)
    if not u:
        db.sb().table("users").insert({
            "channel": "telegram", "channel_user_id": CHAT, "timezone": TZ,
            "display_name": "suite", "weekly_budget_min": 720}).execute()
        u = db.get_user_by_channel("telegram", CHAT)
    return u


def reset(u: dict) -> None:
    db.sb().table("tasks").delete().eq("user_id", u["id"]).execute()
    db.sb().table("messages").delete().eq("user_id", u["id"]).execute()
    db.sb().table("users").update({"last_nudge_on": None}).eq("id", u["id"]).execute()


def say(text: str, msg_id: int | None = None) -> list[tuple]:
    """One inbound text message, exactly as Telegram delivers it."""
    global _msg_id
    _msg_id = msg_id if msg_id is not None else _msg_id + 1
    sent.clear()
    client.post("/webhook/telegram", headers=H, json={
        "update_id": _msg_id,
        "message": {"message_id": _msg_id, "date": int(time.time()),
                    "chat": {"id": int(CHAT), "type": "private"},
                    "from": {"id": int(CHAT), "is_bot": False, "first_name": "R"},
                    "text": text}})
    return list(sent)


def tap(payload: str) -> list[tuple]:
    global _msg_id
    _msg_id += 1
    sent.clear()
    client.post("/webhook/telegram", headers=H, json={
        "update_id": _msg_id,
        "callback_query": {"id": str(_msg_id), "data": payload,
                           "from": {"id": int(CHAT), "is_bot": False, "first_name": "R"},
                           "message": {"message_id": _msg_id,
                                       "chat": {"id": int(CHAT), "type": "private"}}}})
    return list(sent)


def raw(update: dict) -> list[tuple]:
    sent.clear()
    client.post("/webhook/telegram", headers=H, json=update)
    return list(sent)


def tasks(u: dict) -> list[dict]:
    return db.sb().table("tasks").select("*").eq("user_id", u["id"]).order(
        "created_at").execute().data


def find(u: dict, *words: str) -> dict | None:
    """The task whose title mentions these words — how a person would point at
    it, since the model chose the wording and we did not."""
    for t in tasks(u):
        if all(w.lower() in t["title"].lower() for w in words):
            return t
    return None


def text_of(replies: list[tuple]) -> str:
    return " ".join(t for t, _ in replies).lower()


# --- dates, computed the way the user's day is -------------------------------

def today() -> date:
    return datetime.now(__import__("zoneinfo").ZoneInfo(TZ)).date()


def next_weekday(name: str) -> date:
    """The next Friday etc. from today, never today itself — which is what a
    person means by "by Friday" on any day that is not Friday."""
    want = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"].index(name.lower())
    d = today() + timedelta(days=1)
    while d.weekday() != want:
        d += timedelta(days=1)
    return d


# --- scenarios ---------------------------------------------------------------
# Each is (id, what a person actually typed, check). The check reads rows.

SCENARIOS: list[tuple] = []


def scenario(sid: str, prompt: str):
    def wrap(fn):
        SCENARIOS.append((sid, prompt, fn))
        return fn
    return wrap


# --- A · capture: the things people throw at it during the day ---------------

@scenario("A1", "remind me to pay the electricity bill by friday")
def a1(u, r):
    t = find(u, "electric") or find(u, "bill")
    assert t, "no task was created"
    assert t["due_on"] == next_weekday("friday").isoformat(), \
        f"due_on {t['due_on']}, expected {next_weekday('friday')}"
    assert t["planned_on"] is None, \
        f"planned_on was set to {t['planned_on']} — a deadline is not a plan"
    assert t["deadline_hard"] is True, "a bill with a date is a hard deadline"


@scenario("A2", "dentist appointment on thursday at 3pm")
def a2(u, r):
    t = find(u, "dentist")
    assert t, "no task was created"
    assert t["planned_on"] == next_weekday("thursday").isoformat(), \
        f"planned_on {t['planned_on']}, expected {next_weekday('thursday')}"


@scenario("A3", "add call mom, buy groceries, and fix the kitchen sink")
def a3(u, r):
    for words in (("mom",), ("groceries",), ("sink",)):
        assert find(u, *words), f"{words[0]} did not become its own task"


@scenario("A4", "i have to finish the quarterly report by the end of the month, "
                "it's going to take me about three hours")
def a4(u, r):
    t = find(u, "report")
    assert t, "no task was created"
    assert 150 <= t["estimate_min"] <= 210, \
        f"estimate_min {t['estimate_min']} — three hours is 180"
    assert t["due_on"], "an end-of-month deadline was dropped"


# --- B · the questions actually asked, every day -----------------------------

@scenario("B1", "what's on today?")
def b1(u, r):
    assert r, "no reply at all"
    assert len(text_of(r)) > 5, "empty answer to the most common question there is"


@scenario("B2", "what's due this week?")
def b2(u, r):
    body = text_of(r)
    assert body, "no reply"
    assert "electric" in body or "bill" in body or "report" in body, \
        f"the week's deadlines were not named: {body[:200]}"


@scenario("B3", "am i overloaded this week?")
def b3(u, r):
    assert r, "no reply"
    assert "720" not in text_of(r) and "minute" not in text_of(r), \
        "the budget spoke in raw numbers — D8 says it never does"


# --- C · changing things by sentence, not by button --------------------------

@scenario("C1", "paid the electricity bill just now")
def c1(u, r):
    t = find(u, "electric") or find(u, "bill")
    assert t, "the task vanished instead of being completed"
    assert t["status"] == "done", f"status is {t['status']}, expected done"


@scenario("C2", "actually move the dentist to next monday")
def c2(u, r):
    t = find(u, "dentist")
    assert t, "the dentist task vanished"
    assert t["planned_on"] == next_weekday("monday").isoformat(), \
        f"planned_on {t['planned_on']}, expected {next_weekday('monday')}"


@scenario("C3", "forget the groceries, i'll do it another time")
def c3(u, r):
    t = find(u, "groceries")
    assert t, "the task was deleted rather than dropped"
    assert t["status"] == "dropped", f"status is {t['status']}, expected dropped"


# --- D · the edges a real week produces --------------------------------------

@scenario("D1", "mark the car insurance renewal as done")
def d1(u, r):
    """A task that was never captured. Saying so is right; recording it as
    already done is also right — people really do report things they finished
    but never tracked. Leaving an OPEN task behind while reporting it complete
    is the one answer that is wrong, because the list now disagrees with what
    the user was just told."""
    t = find(u, "insurance")
    assert not (t and t["status"] not in ("done", "dropped")), \
        f"left an open task behind while reporting it complete: {t['title']} ({t['status']})"
    body = text_of(r)
    assert body, "no reply"
    if not t:
        assert not any(w in body for w in ("marked it done", "all done", "completed it")), \
            f"claimed to complete a task that does not exist: {body[:200]}"


@scenario("D2", "🏃 run 5k tomorrow morning before work")
def d2(u, r):
    t = find(u, "5k") or find(u, "run")
    assert t, "an emoji at the start swallowed the message"
    assert t["planned_on"] == (today() + timedelta(days=1)).isoformat(), \
        f"planned_on {t['planned_on']}, expected tomorrow"


@scenario("D3", "ok so tomorrow i need to drop the car at the garage before ten, "
                "then there's the standup at 10:30 which i cannot miss, and if "
                "there's time i want to look at the insurance renewal, oh and "
                "mum's birthday is on the 30th i still haven't got anything")
def d3(u, r):
    """A voice note, typed out. Run-on, several tasks, one hard deadline, one
    maybe. The failure mode is capturing one of them and silently losing four."""
    got = [w for w in ("garage", "standup", "insurance", "birthday") if find(u, w)]
    assert len(got) >= 3, f"only captured {got} out of four things"


@scenario("D4", "   ")
def d4(u, r):
    """Whitespace. Telegram sends it, a fat thumb produces it."""
    before = len(tasks(u))
    assert len(tasks(u)) == before, "whitespace created a task"


@scenario("D5", "what did i say i'd do about the car?")
def d5(u, r):
    """Memory of its own conversation, three turns back."""
    body = text_of(r)
    assert "garage" in body or "car" in body, \
        f"it lost the thread of its own conversation: {body[:200]}"


# --- E · the pipeline itself -------------------------------------------------

@scenario("E1", "__duplicate__")
def e1(u, r):
    """Telegram redelivers on any hiccup. The same message twice must not make
    the same task twice."""
    say("book a haircut for saturday", msg_id=777001)
    n = len(tasks(u))
    say("book a haircut for saturday", msg_id=777001)      # the identical update
    assert len(tasks(u)) == n, "a redelivered webhook created a second task"


@scenario("E2", "__buttons__")
def e2(u, r):
    """The fast path: a tap is a write with no model call, and tapping twice is
    not two writes."""
    t = find(u, "haircut") or find(u, "standup") or tasks(u)[0]
    before = models.calls
    r1 = tap(f"d:{t['id']}")
    assert r1, "the Done button produced no reply"
    assert db.sb().table("tasks").select("status").eq("id", t["id"]).execute(
    ).data[0]["status"] == "done", "the tap did not complete the task"
    assert models.calls == before, "the fast path spent a model request"
    r2 = tap(f"d:{t['id']}")
    assert "already" in text_of(r2) or not r2, \
        f"a repeated tap was applied twice: {text_of(r2)[:120]}"


@scenario("E3", "__junk__")
def e3(u, r):
    """A sticker, a bad secret, a stranger. None may crash, all must be seen."""
    rr = raw({"update_id": 880001, "message": {
        "message_id": 880001, "chat": {"id": int(CHAT)}, "sticker": {"file_id": "x"}}})
    assert rr, "a sticker got silence"
    assert client.post("/webhook/telegram", headers={
        "x-telegram-bot-api-secret-token": "wrong"},
        json={"update_id": 880002, "message": {"message_id": 880002,
              "chat": {"id": int(CHAT)}, "text": "hi"}}).status_code == 200
    assert client.post("/webhook/telegram", headers=H, json={
        "update_id": 880003, "edited_message": {}}).status_code == 200


# --- F · the ones the first pass found by accident ----------------------------

@scenario("F1", "call mom")
def f1(u, r):
    """No date was mentioned. The first pass silently invented due_on three
    days out — an invented deadline is worse than none, because the whole
    point of due_on is that it came from the world."""
    t = find(u, "mom")
    assert t, "no task was created"
    assert t["due_on"] is None, f"invented a deadline: due_on={t['due_on']}"
    assert t["planned_on"] is None, f"invented a plan: planned_on={t['planned_on']}"


@scenario("F2", "__depth__")
def f2(u, r):
    """The regression that mattered: the model stopped calling tools once the
    history filled with its own confirmations. It began at turn four, so this
    goes to twelve."""
    reset(u)      # counting new rows only means something against an empty list
    lines = ["pick up the parcel on friday", "renew my passport", "book the mot for the car",
             "dentist follow up next week", "pay the credit card on the 25th",
             "order a birthday cake", "call the landlord about the boiler",
             "submit the expense claim", "buy running shoes", "book flights for december",
             "send the contract back", "chase the refund from amazon"]
    misses = []
    for i, line in enumerate(lines, 1):
        n = len(tasks(u))
        say(line)
        if len(tasks(u)) == n:
            misses.append(f"turn {i}: {line}")
    assert not misses, f"{len(misses)}/{len(lines)} turns did nothing: " + "; ".join(misses[:4])


@scenario("F3", "__rollover__")
def f3(u, r):
    """Yesterday's unfinished work is the entire product. It must come back."""
    db.sb().table("tasks").insert({
        "user_id": u["id"], "title": "file the insurance claim",
        "planned_on": (today() - timedelta(days=2)).isoformat(),
        "status": "todo", "estimate_min": 25, "slip_count": 2}).execute()
    body = text_of(say("morning, what should i be doing today?"))
    assert "insurance" in body or "claim" in body, \
        f"two-day-old unfinished work was never mentioned: {body[:220]}"


@scenario("F4", "__plan_a_deadline__")
def f4(u, r):
    """Giving an existing deadline a day. due_on is the world's, planned_on is
    yours — naming the day must not overwrite the deadline.

    It drives both messages itself: the runner sends a scenario's prompt before
    the check runs, so a setup line written inside the check arrives second and
    tests nothing.
    """
    reset(u)
    say("finish the quarterly report by the end of the month, it'll take about three hours")
    t = find(u, "report")
    assert t and t["due_on"], "the deadline capture itself failed — nothing to plan"
    deadline = t["due_on"]
    say("i'll do the quarterly report on wednesday")
    t = find(u, "report")
    assert t, "the report task vanished"
    assert t["planned_on"] == next_weekday("wednesday").isoformat(), \
        f"planned_on {t['planned_on']}, expected {next_weekday('wednesday')}"
    assert t["due_on"] == deadline, \
        f"setting a plan changed the deadline: {deadline} -> {t['due_on']}"


@scenario("F5", "__voice__")
def f5(u, r):
    """Voice with no transcription key. Whatever happens, the user must be told
    something true — not silence, and not a generic breakage."""
    import core.channels.telegram as ch
    async def _fake(ref): return b"OGGfake"
    real, ch.fetch_voice = ch.fetch_voice, _fake
    try:
        rr = raw({"update_id": 990101, "message": {
            "message_id": 990101, "chat": {"id": int(CHAT)},
            "voice": {"file_id": "vx", "duration": 3}}})
    finally:
        ch.fetch_voice = real
    assert rr, "a voice note produced total silence"
    assert "broke" not in text_of(rr), \
        f"a missing STT key surfaced as a generic breakage: {text_of(rr)[:150]}"


@scenario("F6", "__rotation__")
def f6(u, r):
    """Free tier: the primary model runs out mid-day. The turn must still land
    on another model rather than reaching the user as a failure."""
    primary = models.LADDER[0][0]
    models._free_at[primary] = time.monotonic() + 120      # pretend it is spent
    try:
        n = len(tasks(u))
        rr = say("add: take the cat to the vet on friday")
        assert len(tasks(u)) > n, f"the turn did nothing while {primary} was out"
        assert rr, "no reply while the primary model was out"
    finally:
        models._free_at.pop(primary, None)


@scenario("F7", "__checkin__")
def f7(u, r):
    """The 11am check-in runs through handle_turn like everything else. It is
    the one turn nobody is watching, so it is the one that rots."""
    from core import turn as turnmod
    out = turnmod.handle_turn(u, "<system> morning check-in",
                              f"checkin:{int(time.time())}", "system",
                              turnmod.PLANNING_MODEL)
    assert out, "the morning check-in produced nothing to send"
    assert len(" ".join(t for t, _ in out)) > 10, "the check-in said essentially nothing"


# --- runner ------------------------------------------------------------------

def main(argv: list[str]) -> int:
    u = user()
    picked = [s for s in SCENARIOS
              if not argv or s[0] in argv or s[0][0] in argv]
    if not argv or "A1" in [s[0] for s in picked]:
        reset(u)          # a run that starts at the beginning starts clean
    print(f"\n{len(picked)} scenarios · user {u['id'][:8]} · {u['timezone']} "
          f"· today {today()}\n")

    results = []
    for sid, prompt, check in picked:
        shown = prompt if not prompt.startswith("__") else f"[{prompt.strip('_')}]"
        print(f"\n\033[1m── {sid} ─ {shown[:110]}\033[0m")
        try:
            replies = [] if prompt.startswith("__") else say(prompt)
            check(u, replies)
            print(f"\033[32m   PASS\033[0m  {text_of(replies)[:160] or '(no reply)'}")
            results.append((sid, "PASS", ""))
        except AssertionError as e:
            print(f"\033[31m   FAIL\033[0m  {e}")
            results.append((sid, "FAIL", str(e)))
        except Exception as e:
            print(f"\033[31m   ERROR\033[0m {type(e).__name__}: {e}")
            traceback.print_exc()
            results.append((sid, "ERROR", f"{type(e).__name__}: {e}"))

    print("\n" + "=" * 78)
    for sid, verdict, why in results:
        mark = {"PASS": "\033[32mPASS\033[0m"}.get(verdict, f"\033[31m{verdict}\033[0m")
        print(f"  {sid:<4} {mark}  {why[:100]}")
    bad = [r for r in results if r[1] != "PASS"]
    print(f"\n  {len(results) - len(bad)}/{len(results)} passed")
    print(f"  model requests this run: {models.calls}")
    print("  models:", ", ".join(
        f"{m['model'].replace('gemini-', '')}"
        + (f" parked {m['parked_for_s']}s" if m["parked_for_s"] else "")
        for m in models.status()))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
