"""Local runner: long-poll getUpdates and feed each update to the real webhook.

Only for development, when the app is not deployed and Telegram has nowhere to
push to. It deliberately calls POST /webhook/telegram rather than reimplementing
the routing, so what you test here is exactly what runs on Fly.

    set -a && . ./.env && set +a && .venv/bin/python dev_poll.py

A registered webhook and getUpdates are mutually exclusive, so this only works
before `setWebhook` (or after `deleteWebhook`).
"""

import os

import httpx
from fastapi.testclient import TestClient

import app

API = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

client = TestClient(app.app)
offset = None
print("polling — Ctrl-C to stop")
while True:
    try:
        r = httpx.get(f"{API}/getUpdates", params={"timeout": 25, "offset": offset},
                      timeout=35).json()
    except Exception as e:
        print(f"  poll failed: {e}")
        continue
    for update in r.get("result", []):
        offset = update["update_id"] + 1
        kind = "callback" if "callback_query" in update else "message"
        print(f"  -> {kind} {update['update_id']}")
        resp = client.post("/webhook/telegram", json=update,
                           headers={"x-telegram-bot-api-secret-token": SECRET})
        print(f"     webhook returned {resp.status_code} {resp.json()}")
