"""
handle_turn() — the single entry point (D6).

Five things: load context, run the tool loop, persist both messages, tally the
cost, return the reply. Anything else belongs in a capability.
"""

import os
from functools import cache

from . import audit, ctx, db, models, registry, tool

# Both are a PREFERENCE, not a decision: core/models.py rotates down the ladder
# when the free tier refuses one, so these say where a turn starts, not where it
# necessarily runs. Flash Lite leads because 500 requests a day is the only
# free-tier budget that survives real use — the 20-a-day models are the reserve.
CHAT_MODEL = "gemini-3.5-flash-lite"   # conversational CRUD — short, frequent
PLANNING_MODEL = "gemini-3.6-flash"    # the check-in briefing and the rollover
# A model that keeps calling tools would otherwise loop forever on a paid API.
MAX_STEPS = 8

IDENTITY = """\
You are a personal assistant that owns one person's task list over chat.
Plain text only — no markdown, no asterisks, no bullet characters; the reply is
read in a chat app that shows them literally. Be short. Never lecture.

Nothing you say makes anything happen — only a tool call does. Never report an
action you did not call a tool for in this turn. Answering "Added." without
calling add_task is the one failure you cannot come back from: the user
believes you, stops thinking about it, and the thing is simply gone.
If a message contains several things to do, call the tool once for each.
"""

PROMPT_HASH = audit.prompt_hash(IDENTITY, *registry.PROMPTS, registry.TOOL_SIG)


@cache
def key() -> str:
    return os.environ["GEMINI_API_KEY"]


def _system(user: dict) -> str:
    """The date is computed, never written by hand. A model told the wrong
    weekday resolves "friday" to the wrong date and nothing downstream notices."""
    today = db.local_today(user)
    return (IDENTITY + "\n".join(registry.PROMPTS)
            + f"\nToday is {today:%A %Y-%m-%d} in {user['timezone']}.")


def handle_turn(user: dict, text: str, channel_msg_id: str | None = None,
                role: str = "user", model: str = CHAT_MODEL) -> list[tuple]:
    """Returns [(text, buttons), ...] for app.py to send."""
    if not db.save_message(user["id"], role, text, channel_msg_id):
        audit.usage(status="duplicate")
        return []

    ctx.begin(user)
    body = {
        "systemInstruction": {"parts": [{"text": _system(user)}]},
        "contents": tool.contents_from(db.recent_messages(user["id"], 20)),
        "tools": [{"functionDeclarations": [t.to_dict() for t in registry.TOOLS]}],
    }
    reply = ""
    called, corrected = [], False
    meta = dict(model=model, input_tokens=0, output_tokens=0)  # model: who answered

    for _ in range(MAX_STEPS):
        meta["model"], data = models.generate(model, body, key())
        usage = data.get("usageMetadata", {})
        meta["input_tokens"] += usage.get("promptTokenCount", 0)
        meta["output_tokens"] += usage.get("candidatesTokenCount", 0)

        content = data["candidates"][0].get("content", {})
        parts = content.get("parts", [])
        calls = tool.calls_in(parts)
        audit.step("model", stop=data["candidates"][0].get("finishReason"),
                   out=usage.get("candidatesTokenCount", 0), calls=len(calls))

        # Gemini reports finishReason STOP even while calling a tool, so the
        # presence of a functionCall is the only thing that says keep going.
        if not calls:
            reply = tool.text_of(parts)
            # Once. A second miss is the model's real answer, not a slip.
            fix = None if corrected else tool.correction(reply, called, content)
            if not fix:
                break
            corrected, body["contents"] = True, body["contents"] + fix
            continue
        called += calls
        body["contents"] += [content, tool.dispatch(calls, registry.TOOLS)]
    else:
        audit.step("model", stop="max_steps")  # bailed out, not finished

    meta["cost_usd"] = audit.cost(meta["model"], meta)
    audit.usage(prompt_hash=PROMPT_HASH, **meta)
    # Store what the user SAW — the reply and the cards — not just the model's
    # sentence. The cards are the only evidence in the history that a tool ever
    # ran, and without them the turns read back as "Added." with nothing behind
    # it; the model then imitates that and answers "Added." having added
    # nothing. Memory that omits half the conversation teaches the wrong thing.
    out = ([(reply, None)] if reply else []) + ctx.drain()
    # Silence is never a valid answer to something the user typed: they cannot
    # tell it apart from the bot being down, and they do not send it again.
    out = out or [("I didn't catch that — try saying it again?", None)]
    # `called` rides in meta, not in the audit usage() above: those keys are
    # audit_log columns and this one is not.
    db.save_message(user["id"], "assistant",
                    "\n".join(t for t, _ in out) or "(no reply)",
                    meta={**meta, "calls": called})

    return out
