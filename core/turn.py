"""handle_turn() — the single entry point (D6).

Five things: load context, run the Tool Runner, persist both messages, tally
the cost, return the reply. Anything else belongs in a capability. If this file
grows, logic has leaked out of the D3 seam.
"""

import os
from functools import cache

import anthropic

from . import ctx, db, registry

CHAT_MODEL = "claude-haiku-4-5"      # conversational CRUD — short, frequent
PLANNING_MODEL = "claude-opus-5"     # the check-in briefing and the rollover
# $ per million tokens: (input, output). Cache reads bill at a tenth of input.
PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-opus-5": (5.0, 25.0)}

IDENTITY = """\
You are a personal assistant that owns one person's task list over chat.
Plain text only — no markdown, no asterisks, no bullet characters; the reply is
read in a chat app that shows them literally. Be short. Never lecture.
"""

@cache
def client():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _as_messages(rows: list[dict]) -> list[dict]:
    """History -> a valid messages array: system turns read as user turns, no
    leading assistant, no two of the same role in a row."""
    out: list[dict] = []
    for r in rows:
        role = "assistant" if r["role"] == "assistant" else "user"
        if not out and role == "assistant":
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + r["content"]
        else:
            out.append({"role": role, "content": r["content"]})
    return out


def handle_turn(user: dict, text: str, channel_msg_id: str | None = None,
                role: str = "user", model: str = CHAT_MODEL) -> list[tuple]:
    """Returns [(text, buttons), ...] for app.py to send. Empty means the update
    was a duplicate and has already been handled."""
    if not db.save_message(user["id"], role, text, channel_msg_id):
        return []  # unique violation on channel_msg_id => webhook retry (Step 6.1)

    ctx.begin(user)
    runner = client().beta.messages.tool_runner(
        model=model,
        max_tokens=4096,
        system=[{"type": "text", "text": IDENTITY + "\n".join(registry.PROMPTS),
                 "cache_control": {"type": "ephemeral"}}],
        tools=registry.TOOLS,
        messages=_as_messages(db.recent_messages(user["id"], 20)),
    )

    reply = ""
    meta = dict(model=model, input_tokens=0, output_tokens=0, cache_read_input_tokens=0)
    for message in runner:
        meta["input_tokens"] += message.usage.input_tokens
        meta["output_tokens"] += message.usage.output_tokens
        meta["cache_read_input_tokens"] += message.usage.cache_read_input_tokens or 0
        reply = "\n".join(b.text for b in message.content if b.type == "text").strip()

    pin, pout = PRICES[model]
    meta["cost_usd"] = round(
        (meta["input_tokens"] * pin + meta["cache_read_input_tokens"] * pin / 10
         + meta["output_tokens"] * pout) / 1e6, 6)
    db.save_message(user["id"], "assistant", reply or "(no reply)", meta=meta)

    return ([(reply, None)] if reply else []) + ctx.drain()
