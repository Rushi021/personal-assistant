"""One audit_log row per turn: what came in, every step taken, what went out.

begin() at the app boundary, step() from anywhere, finish() in a finally — so
a turn that raises still leaves a record. Those are the ones worth reading.

Nothing in here raises. A failure to log must never cost you a reply, so every
path out of finish() is swallowed and logged to stderr instead.
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from . import db

log = logging.getLogger("assistant.audit")

# The dict is set once per turn and then MUTATED. That is deliberate: app.py
# runs the turn in asyncio.to_thread, which copies the context — a var re-set
# inside the thread would not be visible out here, but appends to a dict that
# already existed are. Same reason ctx.card() works.
_trace: ContextVar[dict | None] = ContextVar("trace", default=None)

MAX_TEXT = 4000

# $ per million tokens: (input, output).
# Gemini free-tier models cost nothing; the table stays so a paid
# tier is one edit rather than a new mechanism.
PRICES = {
    "gemini-3.5-flash-lite": (0.0, 0.0),   # free tier — the whole ladder is
    "gemini-3.5-flash": (0.0, 0.0),        # free today, so the table exists so
    "gemini-3.6-flash": (0.0, 0.0),        # that a paid tier is one edit rather
    "gemini-3.7-flash": (0.0, 0.0),        # than a new mechanism.
    "gemini-3.8-flash": (0.0, 0.0),
    "gemini-flash-latest": (0.0, 0.0),
}

# evaluation_plan.md §4 — the trust boundary. Applied once, at write time, to
# input, reply, error and every step. Not at read time, not "scrubbed later".
REDACTIONS = (
    (re.compile(r"eyJ[\w\-]{8,}\.[\w\-]{8,}\.[\w\-]{8,}"), "[jwt]"),          # supabase keys
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"), "[bot-token]"),         # telegram
    # Vendor-prefixed API keys. A key reaches a trace the same way anything
    # else does: pasted into chat, or echoed inside a provider's error body.
    (re.compile(r"(?i)\b(?:sk|gsk|pk|rk)[-_][A-Za-z0-9\-_]{16,}"), "[api-key]"),
    # Apple app-specific password, xxxx-xxxx-xxxx-xxxx (CalDAV Basic auth).
    (re.compile(r"\b[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}\b"), "[app-password]"),
    (re.compile(r"(?i)\b(?:a/c|acct|account)\W{0,8}[xX*•]*(\d{4})\d*\b"), r"a/c x\1"),
    (re.compile(r"\b(?:\d[ -]?){12,15}(\d{4})\b"), r"x\1"),                    # PAN -> last 4
    (re.compile(r"(?i)\b(otp|one[ -]?time (?:code|password)|verification code|"
                r"security code)\b\D{0,15}\d{4,8}"), r"\1 [redacted]"),
)


def scrub(s: str) -> str:
    for pattern, replacement in REDACTIONS:
        s = pattern.sub(replacement, s)
    return s


def redact(s: str | None) -> str:
    return scrub(s)[:MAX_TEXT] if s else ""


def cost(model: str, tokens: dict) -> float:
    """What this turn cost, in dollars. Makes the ~$13/month estimate a
    measured number rather than a promise."""
    if model not in PRICES:
        return 0.0
    per_in, per_out = PRICES[model]
    return round((tokens["input_tokens"] * per_in
                  + tokens["output_tokens"] * per_out) / 1e6, 6)


def prompt_hash(*parts: str) -> str:
    """Pins a reply to the exact prompt that produced it. 'It got worse on
    Tuesday' is only answerable if you can tell which turns ran which prompt."""
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:12]


# --- the three calls --------------------------------------------------------

def begin(user: dict, trigger: str, path: str, text: str,
          turn_ref: str | None = None) -> None:
    _trace.set({
        "user_id": user["id"], "trigger": trigger, "path": path,
        "turn_ref": turn_ref, "input": redact(text), "model": None,
        "prompt_hash": None, "steps": [], "_t0": time.monotonic(),
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cost_usd": 0,
    })


def step(t: str, **fields) -> None:
    """Append one event to the trail. A no-op when no turn is in flight, so
    tests and scripts can call handle_turn() without a trace open."""
    trace = _trace.get()
    if trace is not None:
        trace["steps"].append(
            {"t": t, "at": int((time.monotonic() - trace["_t0"]) * 1000), **fields})


def usage(**fields) -> None:
    """model, prompt_hash, and the running token/cost tally from turn.py."""
    trace = _trace.get()
    if trace is not None:
        trace.update(fields)


def finish(reply: str = "", status: str = "ok", error: str | None = None) -> None:
    trace = _trace.get()
    if trace is None:
        return
    _trace.set(None)
    row = None
    saved = False
    try:
        row = {k: v for k, v in trace.items() if not k.startswith("_")}
        # One scrub over the serialised steps catches nested tool arguments and
        # results without walking the structure.
        row["steps"] = json.loads(scrub(json.dumps(row["steps"], default=str)))
        row["reply"] = redact(reply)
        row["status"] = trace.get("status", status)
        row["error"] = redact(error)
        row["latency_ms"] = int((time.monotonic() - trace["_t0"]) * 1000)
        db.sb().table("audit_log").insert(row).execute()
        saved = True
    except Exception:
        log.exception("audit write failed — the turn itself was fine")
    if row is not None:
        _emit(row, saved)


# --- the terminal pipeline --------------------------------------------------
#
# The same trace that goes to audit_log, printed as the pipeline it describes:
# one line per step, in the order things happened, with what that step produced.
# Also appended to a JSONL file so a run survives the scrollback.
#
#   AUDIT_TRACE=0        turn the printing off
#   AUDIT_LOG=path.jsonl move the file (default logs/turns.jsonl)

TRACE = os.environ.get("AUDIT_TRACE", "1").lower() not in ("0", "false", "no")
LOG_FILE = Path(os.environ.get("AUDIT_LOG") or "logs/turns.jsonl")
_TTY = sys.stderr.isatty()

# What each step type IS, so the trail reads as a pipeline rather than a pile
# of dicts. A step type missing from here still prints — under its own name.
LABELS = {
    "ingress":  "parse inbound update",
    "user":     "identify user",
    "stt":      "transcribe voice",
    "model":    "model call",
    "tool":     "tool call",
    "decision": "decision inputs/outputs",
    "write":    "database write",
    "card":     "queue card",
    "send":     "send to channel",
    "drop":         "rejected at the door",
    "rotate":       "model parked (quota)",
    "model_swap":   "fell back to another model",
    "unbacked":     "claimed a change it never made",
    "malformed":    "model botched a tool call, retrying",
    "unsupported":  "no usable input",
}


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _TTY else s


def _val(v) -> str:
    out = v if isinstance(v, str) else json.dumps(v, default=str)
    return out if len(out) <= 220 else out[:217] + "..."


def _summary(step: dict) -> str:
    """Everything the step carried except its type and timestamp — which for a
    tool is the arguments and the result, and that is the whole point."""
    fields = {k: v for k, v in step.items() if k not in ("t", "at")}
    name = fields.pop("name", None)
    body = " ".join(f"{k}={_val(v)}" for k, v in fields.items())
    return f"{name}  {body}".strip() if name else body


def _render(row: dict) -> str:
    head = f"{row['trigger']} -> {row['path']}"
    lines = [f"┌─ {head} · user {str(row['user_id'])[:8]} · ref {row.get('turn_ref') or '-'}",
             f"│ {_dim('IN   ')}{row.get('input') or '-'}"]
    for i, s in enumerate(row["steps"], 1):
        at = _dim(f"{'+' + str(s.get('at', 0)) + 'ms':>9}")
        label = _dim(f"{LABELS.get(s['t'], s['t']):<23}")
        lines.append(f"│ {i:>2}{at}  {s['t']:<9}{label} {_summary(s)}")
    if not row["steps"]:
        lines.append(f"│    {_dim('(no steps — the turn failed before doing anything)')}")
    lines.append(f"│ {_dim('OUT  ')}{row.get('reply') or '-'}")
    tail = (f"└─ {row['status']} · {row['latency_ms']}ms · "
            f"{row['input_tokens']}in/{row['output_tokens']}out tok · "
            f"${row['cost_usd']} · audit_log {row.pop('_saved', '')}")
    lines.append(tail)
    if row.get("error"):
        lines.append(f"   error: {row['error']}")
    return "\n".join(lines)


def _emit(row: dict, saved: bool | None) -> None:
    """Never raises. Losing the trace must not cost the reply — same rule as
    the database write above it."""
    try:
        if TRACE:
            print(_render({**row, "_saved": {True: "ok", False: "FAILED"}.get(saved, "n/a")}),
                  file=sys.stderr, flush=True)
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(),
                 "audit_log_saved": saved, **row}, default=str) + "\n")
    except Exception:
        log.exception("trace render failed — the turn itself was fine")


def drop(stage: str, **fields) -> None:
    """An update rejected before a trace could open: bad secret, unparseable
    update, unknown chat. There is no user_id, so there is no audit_log row —
    the terminal and the JSONL file are the only record these get, and an input
    that vanishes with no record at all is the bug this exists to stop."""
    _emit({"user_id": "-", "trigger": "drop", "path": stage, "turn_ref": None,
           "input": "", "reply": "", "status": "dropped", "error": None,
           "latency_ms": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0,
           "steps": [{"t": "drop", "at": 0, **fields}]}, saved=None)


def sweep(days: int = 60) -> None:
    """Delete unjudged rows older than `days`. Anything you labelled is an eval
    case and is kept forever. Called from /cron/tick at 03:00 UTC."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        db.sb().table("audit_log").delete().lt("created_at", cutoff).is_(
            "verdict", "null").execute()
    except Exception:
        log.exception("audit sweep failed")
