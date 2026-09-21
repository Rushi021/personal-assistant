"""FastAPI ingress. Routes, never logic.

Two rules this file exists to keep:
  - it touches no Telegram-specific field; everything comes off Inbound (D11)
  - it never returns a non-200 to a webhook, because that makes Telegram retry
    and do the work twice (Step 6.2)
"""

import asyncio
import logging
import os
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from capabilities import expenses, gmail, tasks
from core import audit, ctx, db, models, stt
from core.channels import telegram as channel
from core.turn import PLANNING_MODEL, handle_turn

log = logging.getLogger("assistant")
app = FastAPI()

CRON_SECRET = os.environ.get("CRON_SECRET", "")


@app.get("/health")
def health():
    return {"ok": True}


async def _deliver(to: str, messages: list[tuple]) -> None:
    for text, buttons in messages:
        await channel.send(to, text, buttons)   # raises if Telegram refused it
        audit.step("send", to=to, chars=len(text), sent=True,
                   buttons=[b.label for b in buttons or []], text=audit.redact(text))


def _ingress(inbound, user: dict, kind: str) -> None:
    """The first two steps of every turn: what Telegram actually sent, and who
    it resolved to. Both were previously invisible — the trail started at the
    model, so a turn that went wrong at the door looked like a turn that never
    happened."""
    audit.step("ingress", channel=inbound.channel, chat=inbound.channel_user_id,
               msg_id=inbound.channel_msg_id, kind=kind,
               text=audit.redact(inbound.text), voice=bool(inbound.voice_ref),
               button=inbound.button_payload)
    audit.step("user", id=user["id"], tz=user["timezone"],
               checkin_hour=user["checkin_hour"])


TAP_LABELS = {"d": "Done", "t": "Tomorrow", "x": "Drop",
              "ka": "Keep all", "da": "Drop all", "ob": "One by one"}


def _fast_path(user: dict, payload: str, channel_msg_id: str) -> list[tuple]:
    """D4 — a button tap is a deterministic parse and a direct write. No model,
    no tokens, ~100ms. Roughly 40% of turns should land here."""
    today = db.local_today(user)
    kind, _, task_id = payload.partition(":")
    ctx.begin(user)  # roll_to and the calendar sync both read ctx.user()

    # Claim the tap before acting on it. Telegram retries callbacks too, and
    # "Tomorrow" applied twice would silently slip a task two days (Step 6.1).
    tapped = f"(tapped {TAP_LABELS.get(kind, kind)})"
    if not db.save_message(user["id"], "user", tapped, channel_msg_id):
        audit.usage(status="duplicate")
        return []

    if kind in ("ka", "da", "ob"):
        stale = [t for t in db.open_tasks(user["id"], planned_on_or_before=today)
                 if t["planned_on"] and t["planned_on"] < today.isoformat()]
        if kind == "ka":
            for t in stale:
                tasks.roll_to(t["id"], user["id"], today)
            audit.step("write", table="tasks", op="roll", rows=len(stale))
            done = f"Kept {len(stale)}, all on today."
        elif kind == "da":
            for t in stale:
                db.sb().table("tasks").update({"status": "dropped"}).eq("id", t["id"]).execute()
            audit.step("write", table="tasks", op="drop", rows=len(stale))
            done = f"Dropped {len(stale)}."
        else:
            for t in stale[:10]:
                tasks.render(t)
            text = f"{len(stale)} to go through."
            db.save_message(user["id"], "assistant", text, meta={"fast_path": True})
            return [(text, None)] + ctx.drain()
    elif kind == "d":
        # Routed through tasks.complete so the button and the tool cannot drift:
        # a recurring reminder must roll forward whichever way it was finished.
        # in_(OPEN) inside it is what makes a second tap a no-op — the dedupe
        # above only catches Telegram redelivering ONE callback, and a person
        # tapping Done twice sends two different callback ids.
        row = tasks.complete(task_id, user["id"], user["timezone"])
        audit.step("write", table="tasks", op="done", row=task_id, matched=bool(row))
        if not row:
            done = "Already handled."
        elif row.get("recurred_to"):
            done = f"Done: {row['title']}. Back on {row['recurred_to']}."
        else:
            done = f"Done: {row['title']}."
    elif kind == "t":
        row = tasks.roll_to(task_id, user["id"], today + timedelta(days=1))
        audit.step("write", table="tasks", op="roll", row=task_id, matched=bool(row))
        done = f"Tomorrow: {row['title']}." if row else "Already handled."
    elif kind == "x":
        row = db.sb().table("tasks").update({"status": "dropped"}).eq(
            "id", task_id).eq("user_id", user["id"]).in_("status", db.OPEN).execute().data
        audit.step("write", table="tasks", op="drop", row=task_id, matched=bool(row))
        done = f"Dropped: {row[0]['title']}." if row else "Already handled."
    else:
        return []

    left = len(db.open_tasks(user["id"], planned_on_or_before=today))
    text = f"{done} {left} left today."
    # meta carries no model, no tokens, no cost. That absence is the whole point.
    db.save_message(user["id"], "assistant", text, meta={"fast_path": True})
    return [(text, None)]


@app.post("/webhook/telegram")
async def webhook(request: Request):
    """Returns 200 no matter what. A non-200 makes Telegram redeliver, and a
    redelivery of work that half-succeeded is worse than the original failure.
    Everything — including the user lookup — is therefore inside the guard."""
    chat, messages = None, []
    try:
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not channel.verify(headers, await request.body()):
            audit.drop("verify", reason="secret token did not match")
            return {"ok": True}

        body = await request.json()
        inbound = channel.parse(body)
        if not inbound:
            audit.drop("parse", reason="no message in update", keys=sorted(body))
            return {"ok": True}
        chat = inbound.channel_user_id

        user = await asyncio.to_thread(db.get_user_by_channel, inbound.channel, chat)
        if not user:
            audit.drop("user", reason="chat id has no users row", chat=chat)
            await channel.send(chat, "Not a registered user.")
            return {"ok": True}

        kind = ("button" if inbound.button_payload else
                "voice" if inbound.voice_ref else "text" if inbound.text else "unsupported")

        if inbound.button_payload:
            audit.begin(user, "button", "fast", f"(tap {inbound.button_payload})",
                        inbound.channel_msg_id)
            _ingress(inbound, user, kind)
            await channel.ack(inbound.channel_msg_id)
            messages = await asyncio.to_thread(
                _fast_path, user, inbound.button_payload, inbound.channel_msg_id)
        else:
            text, source = inbound.text, "chat"
            # Opened before transcription, so a failed transcription is a traced
            # turn rather than a turn that never happened.
            audit.begin(user, "voice" if inbound.voice_ref else "message", "model",
                        text or ("(voice note)" if inbound.voice_ref
                                 else f"({kind} message)"), inbound.channel_msg_id)
            _ingress(inbound, user, kind)
            if inbound.voice_ref:
                audio = await channel.fetch_voice(inbound.voice_ref)
                audit.step("stt", bytes=len(audio))
                try:
                    text = await stt.transcribe(audio)
                except Exception as e:
                    # A note the bot cannot hear is a normal failure with a
                    # useful answer. "Try again" is not it — no key and no
                    # audio means it will fail identically every time.
                    audit.step("stt", ok=False, error=f"{type(e).__name__}: {e}")
                    msg = "I couldn't make out that voice note — send it as text?"
                    await _deliver(chat, [(msg, None)])
                    audit.finish(msg, status="error", error=f"{type(e).__name__}: {e}")
                    return {"ok": True}
                audit.step("stt", transcript=audit.redact(text), chars=len(text))
                audit.usage(input=audit.redact(text))
                source = "voice"
            if not text:
                # A sticker, a photo, a contact card. Until now this returned
                # silently and read, from the phone, as the bot being dead.
                audit.step("unsupported", kinds_accepted="text, voice")
                messages = [("I can read text and voice notes — that one I can't.", None)]
                await _deliver(chat, messages)
                audit.finish(messages[0][0])
                return {"ok": True}
            ctx.set_source(source)  # contextvars survive to_thread
            messages = await asyncio.to_thread(
                handle_turn, user, text, inbound.channel_msg_id)

        await _deliver(chat, messages)
    except Exception as e:
        # The webhook swallows everything by design, so without this write a
        # failure leaves no trace at all — only a reply you never got.
        audit.finish(status="error", error=f"{type(e).__name__}: {e}")
        log.error("turn failed:\n%s", traceback.format_exc())
        if chat:
            try:
                await channel.send(chat, models.OUT_OF_QUOTA
                                   if isinstance(e, models.Exhausted)
                                   else "Something broke on my side. Try again?")
            except Exception:
                log.error("could not even send the failure reply")
    else:
        audit.finish(" ".join(t for t, _ in messages))

    return {"ok": True}


@app.post("/cron/tick")
async def tick(request: Request):
    # A public endpoint that sends you messages and spends tokens. An unset
    # secret is a misconfiguration, not a reason to let anyone in.
    if not CRON_SECRET or request.headers.get("x-cron-secret") != CRON_SECRET:
        return JSONResponse({"ok": False}, status_code=403)

    users = await asyncio.to_thread(db.all_users)

    # Gmail runs every tick, for everyone, in its own loop. It deliberately
    # does NOT live in the check-in loop below: the checkin_hour guard there
    # `continue`s, so a poll placed under it would run one hour a day.
    if gmail.configured():
        for user in users:
            try:
                audit.begin(user, "gmail", "rules", "<system> gmail poll")
                n = await asyncio.to_thread(gmail.poll, user)
                audit.finish(f"{n} processed")
            except Exception as e:
                # A mailbox that is down must not cancel anyone's check-in.
                audit.finish(status="error", error=f"{type(e).__name__}: {e}")
                log.error("gmail poll failed for %s:\n%s",
                          user["id"], traceback.format_exc())

    # The money message (E3, E4). Composed in code — no model call, so it is
    # free, instant and cannot misquote an amount. Its own hour and its own
    # guard: the check-in opens the day's work, this closes the day's spending.
    digested = []
    for user in users:
        try:
            today = db.local_today(user)
            if datetime.now(ZoneInfo(user["timezone"])).hour != user.get("digest_hour", 21):
                continue
            if str(user.get("last_digest_on") or "") == today.isoformat():
                continue          # hourly tick, one digest — same guard as the nag
            # Opened BEFORE composing, so the digest's own decision step lands
            # in the trace. A silent day still leaves a row: "why did I not get
            # a digest" is only answerable if the quiet runs are recorded too.
            audit.begin(user, "digest", "rules", "<system> expense digest")
            text = await asyncio.to_thread(expenses.digest, user)
            await asyncio.to_thread(db.mark_digest_sent, user, today)
            if not text:
                audit.finish("(nothing to report)")
                continue          # nothing spent, nothing pending: say nothing
            await _deliver(user["channel_user_id"], [(text, None)])
            # Saved as a message so the model has it in context when the user
            # replies "the amazon one was groceries" — without it that answer
            # refers to something the assistant has no record of saying.
            await asyncio.to_thread(db.save_message, user["id"], "assistant",
                                    text, None, {"digest": True})
            audit.finish(text)
            digested.append(user["id"])
        except Exception as e:
            audit.finish(status="error", error=f"{type(e).__name__}: {e}")
            log.error("digest failed for %s:\n%s", user["id"], traceback.format_exc())

    fired = []
    for user in users:
        try:
            if datetime.now(ZoneInfo(user["timezone"])).hour != user["checkin_hour"]:
                continue
            if await asyncio.to_thread(db.has_activity_today, user):
                continue  # you opened the day yourself (S7)
            audit.begin(user, "cron", "model", "<system> morning check-in")
            messages = await asyncio.to_thread(
                handle_turn, user, "<system> morning check-in",
                None, "system", PLANNING_MODEL)  # the same function the webhook calls
            await _deliver(user["channel_user_id"], messages)
            audit.finish(" ".join(t for t, _ in messages))
            fired.append(user["id"])
        except Exception as e:
            # One user's bad timezone must not cancel everyone else's check-in.
            audit.finish(status="error", error=f"{type(e).__name__}: {e}")
            log.error("check-in failed for %s:\n%s", user["id"], traceback.format_exc())

    # Unjudged turns older than 60 days go; anything you labelled is an eval
    # case and is kept. The tick is hourly, so pin the sweep to one of them.
    if datetime.now(ZoneInfo("UTC")).hour == 3:
        await asyncio.to_thread(audit.sweep)

    return {"ok": True, "fired": fired, "digested": digested}
