"""Free-tier model rotation.

The Gemini free tier caps requests per DAY per model, not just per minute:
Flash Lite allows 500, each of the bigger Flash models 20. One turn costs two
or three requests — the model answers, calls a tool, then answers again — so
3.6 Flash alone is about seven conversations before it starts refusing. That
refusal arrives as a 429 in the middle of a sentence, and before this file
existed it reached the user as "Something broke on my side".

So a 429 does not fail a turn here. It parks that model and hands the same
request to the next one down the ladder. Only when every model is parked does
anything actually wait.
"""

import logging
import time

import httpx

from . import audit

log = logging.getLogger("assistant.models")


class Exhausted(RuntimeError):
    """Every model is out of free-tier quota. Distinct from a crash because the
    honest answer is different: "try again" is wrong when the quota resets at
    midnight Pacific and nothing the user does before then will help."""


# What the user is told when it happens. Not "something broke" — nothing did.
OUT_OF_QUOTA = "I've used up today's model budget. I'll be back after midnight."

API = "https://generativelanguage.googleapis.com/v1beta/models"

# (model, requests per minute on the free tier). Order is the fallback order:
# the model with 500 requests a day carries the routine, the scarce ones are
# the reserve. Add a model by adding a line.
LADDER = [
    ("gemini-3.5-flash-lite", 15),   # 500 RPD — the workhorse
    ("gemini-3.8-flash", 5),         #  20 RPD
    ("gemini-3.6-flash", 5),         #  20 RPD
    ("gemini-3.7-flash", 5),         #  20 RPD
    ("gemini-3.5-flash", 5),         #  20 RPD
    ("gemini-flash-latest", 5),      #  20 RPD
]
RPM = dict(LADDER)
DEFAULT_RPM = 5
DEADLINE = 300          # give up on a turn rather than wait past this
OVERLOAD_PARK = 15      # 503 means busy, not out of quota
MALFORMED_TRIES = 6     # then hand it back rather than loop to the deadline
NUDGE_AFTER = 2         # retries before changing the context rather than repeating it
NUDGE = ("Your last tool call could not be parsed. Call it again, with only "
         "the arguments you are certain of.")

calls = 0                         # model requests this process has made
_free_at: dict[str, float] = {}   # model -> monotonic time it may be used again
_last: dict[str, float] = {}      # model -> monotonic time of its last request


def _order(preferred: str) -> list[str]:
    """The preferred model first, then everything else still on the ladder."""
    return [preferred] + [m for m, _ in LADDER if m != preferred]


def _park(model: str, seconds: float, reason: str) -> None:
    _free_at[model] = time.monotonic() + seconds
    audit.step("rotate", model=model, reason=reason, parked_for_s=int(seconds))
    log.warning("parking %s for %ds (%s)", model, seconds, reason)


def _pace(model: str) -> None:
    """Free tier counts requests per minute as well as per day. Spacing them
    out is cheaper than being refused and having to come back."""
    gap = 60.0 / RPM.get(model, DEFAULT_RPM) + 0.5
    wait = _last.get(model, 0.0) + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last[model] = time.monotonic()


def generate(preferred: str, body: dict, key: str) -> tuple[str, dict]:
    """One model turn. Returns (model that actually answered, response json).

    Raises only if every model is out of quota for longer than DEADLINE — at
    which point the turn genuinely cannot be served and the caller should say
    so rather than pretend.
    """
    give_up_at = time.monotonic() + DEADLINE
    order = _order(preferred)
    tried: list[str] = []
    malformed = 0

    while time.monotonic() < give_up_at:
        ready = [m for m in order if _free_at.get(m, 0.0) <= time.monotonic()]
        if not ready:
            # Everything is parked. The soonest one back decides the nap, and
            # a per-day park is 24h — so this is where a turn legitimately dies.
            nap = min(_free_at[m] for m in order) - time.monotonic()
            if time.monotonic() + nap > give_up_at:
                break
            log.warning("all models parked, waiting %.1fs", nap)
            time.sleep(max(nap, 1))
            continue

        for model in ready:
            global calls
            calls += 1
            _pace(model)
            tried.append(model)
            try:
                r = httpx.post(f"{API}/{model}:generateContent",
                               params={"key": key}, json=body, timeout=90)
            except httpx.RequestError as e:            # network blip, not quota
                _park(model, OVERLOAD_PARK, f"{type(e).__name__}")
                continue
            if r.status_code == 429:
                # Per-day is terminal until midnight Pacific; per-minute clears
                # in under a minute. Telling them apart is the difference
                # between losing a model for a day and losing it for a sentence.
                per_day = "PerDay" in r.text
                _park(model, 86400 if per_day else 60,
                      "per-day quota" if per_day else "per-minute quota")
                continue
            if r.status_code in (500, 502, 503, 504):
                _park(model, OVERLOAD_PARK, f"http {r.status_code}")
                continue
            r.raise_for_status()
            data = r.json()
            # MALFORMED_FUNCTION_CALL: the model meant to call a tool and
            # botched the JSON. It comes back with no call AND no text, so the
            # turn used to end in total silence — the user's message simply
            # went nowhere. Another model usually gets it right first try.
            if (malformed < MALFORMED_TRIES and any(
                    c.get("finishReason") == "MALFORMED_FUNCTION_CALL"
                    for c in data.get("candidates", []))):
                # Not a park: the model is not out of quota, it fumbled one
                # response. _pace() already spaces the retry, and another model
                # takes it instead whenever one is free.
                malformed += 1
                audit.step("malformed", model=model, attempt=malformed)
                if malformed == NUDGE_AFTER:
                    # Same context, same model, same bad call — it is not
                    # random, so retrying it unchanged just burns quota. One
                    # extra turn makes the input different enough to shake it.
                    body = {**body, "contents": body["contents"] + [
                        {"role": "user", "parts": [{"text": NUDGE}]}]}
                continue
            if model != preferred:
                audit.step("model_swap", asked=preferred, used=model)
            return model, data

    raise Exhausted(
        f"every model is out of free-tier quota (tried {', '.join(dict.fromkeys(tried))})")


def status() -> list[dict]:
    """What the ladder looks like right now — for the terminal, and for a test
    run that needs to know how much budget is left before it starts."""
    now = time.monotonic()
    return [{"model": m, "rpm": rpm,
             "parked_for_s": max(0, int(_free_at.get(m, 0.0) - now))}
            for m, rpm in LADDER]
