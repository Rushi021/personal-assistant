"""Telegram adapter — the one Channel implementation (D6, D11).

Three things this adapter deliberately never does, because WhatsApp cannot do
them and retrofitting is a redesign rather than a port: it sends no rich-text
formatting mode, it never updates a message it already sent, and it never puts
more than three buttons on one. test_flow.py greps this file for the API names.
"""

import os

import httpx

from .base import MAX_BUTTONS, Button, Inbound

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"

name = "telegram"


def verify(headers: dict, body: bytes = b"") -> bool:
    """Telegram echoes the secret we registered with setWebhook."""
    if not SECRET:
        return True  # not configured yet — Step 0 echo works without it
    return headers.get("x-telegram-bot-api-secret-token") == SECRET


def parse(body: dict) -> Inbound | None:
    cb = body.get("callback_query")
    if cb:
        return Inbound(
            channel=name,
            channel_user_id=str(cb["message"]["chat"]["id"]),
            channel_msg_id=f"cb:{cb['id']}",
            button_payload=cb.get("data"),
        )

    msg = body.get("message") or body.get("edited_message")
    if not msg:
        return None

    voice = msg.get("voice") or msg.get("audio")
    return Inbound(
        channel=name,
        channel_user_id=str(msg["chat"]["id"]),
        channel_msg_id=f"{msg['chat']['id']}:{msg['message_id']}",
        text=msg.get("text") or msg.get("caption"),
        voice_ref=voice["file_id"] if voice else None,
    )


async def fetch_voice(voice_ref: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as http:
        meta = (await http.get(f"{API}/getFile", params={"file_id": voice_ref})).json()
        path = meta["result"]["file_path"]
        return (await http.get(f"{FILE_API}/{path}")).content


async def send(to: str, text: str, buttons: list[Button] | None = None) -> None:
    payload: dict = {"chat_id": to, "text": text}
    if buttons:
        assert len(buttons) <= MAX_BUTTONS, "D11: at most 3 buttons"
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": b.label, "callback_data": b.payload} for b in buttons]]
        }
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(f"{API}/sendMessage", json=payload)
    # Telegram answers 400 with a reason ("chat not found", "message is too
    # long") and this used to discard it — the turn recorded ok and the phone
    # showed nothing. A refused send is a failed turn.
    if r.status_code != 200:
        raise RuntimeError(f"telegram sendMessage {r.status_code}: {r.text[:300]}")


async def ack(channel_msg_id: str) -> None:
    """Stop the client-side spinner on a tapped button. The "cb:" prefix this
    module put on the id in parse() is this module's business to strip again —
    app.py just hands back the Inbound it was given."""
    if not channel_msg_id.startswith("cb:"):
        return
    async with httpx.AsyncClient(timeout=10) as http:
        await http.post(f"{API}/answerCallbackQuery",
                        json={"callback_query_id": channel_msg_id[3:]})
