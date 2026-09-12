"""Capability auto-discovery — the entire plugin system (D3).

Adding Gmail means adding capabilities/gmail.py. This file does not change.
Sorted so the cached prompt prefix is byte-identical between requests.
"""

import importlib
import pkgutil

import capabilities

TOOLS: list = []
PROMPTS: list[str] = []

for _mod in sorted(m.name for m in pkgutil.iter_modules(capabilities.__path__)):
    _m = importlib.import_module(f"capabilities.{_mod}")
    TOOLS.extend(getattr(_m, "TOOLS", []))
    if getattr(_m, "PROMPT", None):
        PROMPTS.append(_m.PROMPT)
