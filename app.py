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

from capabilities import tasks
from core import ctx, db, stt
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
        await channel.send(to, text, buttons)


TAP_LABELS = {"d": "Done", "t": "Tomorrow", "x": "Drop",
              "ka": "Keep all", "da": "Drop all", "ob": "One by one"}


def _fast_path(user: dict, payload: str, channel_msg_id: str) -> list[tuple]:
    """D4 — a button tap is a deterministic parse and a direct write. No model,
    no tokens, ~100ms. Roughly 40% of turns should land here."""
    today = db.local_today(user)
    kind, _, task_id = payload.partition(":")

    # Claim the tap before acting on it. Telegram retries callbacks too, and
    # "Tomorrow" applied twice would silently slip a task two days (Step 6.1).
    tapped = f"(tapped {TAP_LABELS.get(kind, kind)})"
    if not db.save_message(user["id"], "user", tapped, channel_msg_id):
        return []

    if kind in ("ka", "da", "ob"):
        stale = [t for t in db.open_tasks(user["id"], planned_on_or_before=today)
                 if t["planned_on"] and t["planned_on"] < today.isoformat()]
        if kind == "ka":
            for t in stale:
                tasks.roll_to(t["id"], user["id"], today)
            done = f"Kept {len(stale)}, all on today."
        elif kind == "da":
            for t in stale:
                db.sb().table("tasks").update({"status": "dropped"}).eq("id", t["id"]).execute()
            done = f"Dropped {len(stale)}."
        else:
            ctx.begin(user)
            for t in stale[:10]:
                tasks.render(t)
            text = f"{len(stale)} to go through."
            db.save_message(user["id"], "assistant", text, meta={"fast_path": True})
            return [(text, None)] + ctx.drain()
    elif kind == "d":
        row = db.sb().table("tasks").update(
            {"status": "done", "completed_at": datetime.now(ZoneInfo("UTC")).isoformat()}
        ).eq("id", task_id).eq("user_id", user["id"]).execute().data
        done = f"Done: {row[0]['title']}." if row else "Already handled."
    elif kind == "t":
        row = tasks.roll_to(task_id, user["id"], today + timedelta(days=1))
        done = f"Tomorrow: {row['title']}." if row else "Already handled."
    elif kind == "x":
        row = db.sb().table("tasks").update({"status": "dropped"}).eq(
            "id", task_id).eq("user_id", user["id"]).execute().data
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
    chat = None
    try:
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not channel.verify(headers, await request.body()):
            return {"ok": True}

        inbound = channel.parse(await request.json())
        if not inbound:
            return {"ok": True}
        chat = inbound.channel_user_id

        user = await asyncio.to_thread(db.get_user_by_channel, inbound.channel, chat)
        if not user:
            await channel.send(chat, "Not a registered user.")
            return {"ok": True}

        if inbound.button_payload:
            await channel.ack(inbound.channel_msg_id)
            messages = await asyncio.to_thread(
                _fast_path, user, inbound.button_payload, inbound.channel_msg_id)
        else:
            text, source = inbound.text, "chat"
            if inbound.voice_ref:
                text = await stt.transcribe(await channel.fetch_voice(inbound.voice_ref))
                source = "voice"
            if not text:
                return {"ok": True}
            ctx.set_source(source)  # contextvars survive to_thread
            messages = await asyncio.to_thread(
                handle_turn, user, text, inbound.channel_msg_id)

        await _deliver(chat, messages)
    except Exception:
        log.error("turn failed:\n%s", traceback.format_exc())
        if chat:
            try:
                await channel.send(chat, "Something broke on my side. Try again?")
            except Exception:
                log.error("could not even send the failure reply")

    return {"ok": True}


@app.post("/cron/tick")
async def tick(request: Request):
    # A public endpoint that sends you messages and spends tokens. An unset
    # secret is a misconfiguration, not a reason to let anyone in.
    if not CRON_SECRET or request.headers.get("x-cron-secret") != CRON_SECRET:
        return JSONResponse({"ok": False}, status_code=403)

    fired = []
    for user in await asyncio.to_thread(db.all_users):
        try:
            if datetime.now(ZoneInfo(user["timezone"])).hour != user["checkin_hour"]:
                continue
            if await asyncio.to_thread(db.has_activity_today, user):
                continue  # you opened the day yourself (S7)
            messages = await asyncio.to_thread(
                handle_turn, user, "<system> morning check-in",
                None, "system", PLANNING_MODEL)  # the same function the webhook calls
            await _deliver(user["channel_user_id"], messages)
            fired.append(user["id"])
        except Exception:
            # One user's bad timezone must not cancel everyone else's check-in.
            log.error("check-in failed for %s:\n%s", user["id"], traceback.format_exc())

    return {"ok": True, "fired": fired}
