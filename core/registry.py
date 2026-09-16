"""Capability auto-discovery — the entire plugin system (D3).

Adding Gmail means adding capabilities/gmail.py. This file does not change.
Sorted so the cached prompt prefix is byte-identical between requests.
"""

import importlib
import json
import pkgutil
import time

import capabilities

from . import audit

TOOLS: list = []
PROMPTS: list[str] = []

for _mod in sorted(m.name for m in pkgutil.iter_modules(capabilities.__path__)):
    _m = importlib.import_module(f"capabilities.{_mod}")
    TOOLS.extend(getattr(_m, "TOOLS", []))
    if getattr(_m, "PROMPT", None):
        PROMPTS.append(_m.PROMPT)


def _traced(tool):
    """Log every tool invocation from one place.

    Every call the SDK makes goes through BetaFunctionTool.call, so wrapping it
    here covers all tools and no capability has to know the audit log exists.
    The arguments are the point: almost every failure of this system is a wrong
    argument, not a wrong sentence — planned_on set when the user meant due_on
    reads identically in the reply and only differs here.
    """
    inner, name = tool.call, tool.name

    def call(input):
        t0 = time.monotonic()
        try:
            result = inner(input)
        except Exception as e:
            audit.step("tool", name=name, args=input, ok=False,
                       ms=int((time.monotonic() - t0) * 1000),
                       error=f"{type(e).__name__}: {e}")
            raise
        # A tool that returns {"error": ...} — _update() on a row that matched
        # nothing — has failed, whatever the model does with it next.
        audit.step("tool", name=name, args=input,
                   ok=not (isinstance(result, dict) and "error" in result),
                   ms=int((time.monotonic() - t0) * 1000), result=result)
        return result

    tool.call = call
    return tool


TOOLS = [_traced(t) for t in TOOLS]

# Everything the model is told, in one string. turn.py hashes this with the
# identity block so a reply can be pinned to the prompt that produced it.
TOOL_SIG = json.dumps([t.to_dict() for t in TOOLS], sort_keys=True)
