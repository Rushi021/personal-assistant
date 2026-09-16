"""
core/tool.py — lightweight @tool decorator for Gemini function calling.

Reads Python type hints and the docstring Args: section to build a Gemini
functionDeclaration. Exposes .name, .call() and .to_dict() so registry.py and
turn.py do not care which provider is underneath.
"""
import inspect
import json
import re
from typing import Any, get_type_hints

from . import audit


def _py_to_json_type(annotation) -> str:
    """Map Python type annotation to JSON schema type string."""
    import types
    # Handle Optional / X | None
    origin = getattr(annotation, "__origin__", None)
    if origin is types.UnionType or str(origin) == "typing.Union":
        args = [a for a in annotation.__args__ if a is not type(None)]
        return _py_to_json_type(args[0]) if args else "string"
    if annotation in (str, type(None)):
        return "string"
    if annotation == int:
        return "integer"
    if annotation == bool:
        return "boolean"
    if annotation == float:
        return "number"
    return "string"


def _parse_args_docstring(docstring: str) -> dict[str, str]:
    """Extract per-arg descriptions from the Args: section of a docstring."""
    arg_docs: dict[str, str] = {}
    if not docstring:
        return arg_docs
    parts = re.split(r"\n\s*Args:\s*\n", docstring, maxsplit=1)
    if len(parts) < 2:
        return arg_docs
    args_section = parts[1]
    # Each arg is indented 8 spaces; continuation lines are indented more
    for m in re.finditer(
        r"^[ ]{6,12}(\w+):\s*(.+?)(?=\n[ ]{6,12}\w+:|\Z)",
        args_section, re.DOTALL | re.MULTILINE
    ):
        arg_docs[m.group(1)] = " ".join(m.group(2).split())
    return arg_docs


def tool(fn):
    """Decorator. Generates Mistral-compatible tool schema; exposes .name,
    .call(input_dict), and .to_dict() so registry.py works unchanged."""
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}
    hints.pop("return", None)

    sig = inspect.signature(fn)
    doc = inspect.getdoc(fn) or ""

    # Description = everything before the first blank line or "Args:"
    description = re.split(r"\n\s*(?:Args|Returns|Raises):\s*\n", doc)[0].strip()

    arg_docs = _parse_args_docstring(fn.__doc__ or "")

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        annotation = hints.get(param_name, str)
        json_type = _py_to_json_type(annotation)
        prop: dict[str, Any] = {"type": json_type}
        if param_name in arg_docs:
            prop["description"] = arg_docs[param_name]
        properties[param_name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    # A Gemini functionDeclaration. A zero-argument tool must omit `parameters`
    # entirely — an empty properties object is rejected as an invalid schema.
    schema: dict[str, Any] = {"name": fn.__name__, "description": description}
    if properties:
        schema["parameters"] = {"type": "object", "properties": properties,
                                "required": required}

    fn.name = fn.__name__
    fn.tool_schema = schema

    def call(input_dict: dict):
        return fn(**input_dict)

    fn.call = call
    fn.to_dict = lambda: schema
    return fn


def contents_from(rows: list[dict]) -> list[dict]:
    """Stored history → a valid Gemini contents array: the assistant is "model",
    system turns read as user turns, no leading model turn, and no two of the
    same role in a row.

    An assistant turn that called tools is replayed as the three turns it
    actually was — the functionCall, its response, then the text. Without that
    the history reads as a column of bare confirmations, and the model learns
    that confirming IS the action: after three or four turns it starts
    answering "Added." having added nothing, and the task is simply lost. That
    is not a prompt problem and no wording fixes it; the evidence has to be in
    the history.
    """
    out: list[dict] = []

    def push(role: str, parts: list[dict]) -> None:
        if not out and role == "model":
            return                                  # no leading model turn
        if (out and out[-1]["role"] == role
                and "text" in parts[0] and "text" in out[-1]["parts"][0]):
            out[-1]["parts"][0]["text"] += "\n" + parts[0]["text"]
        else:
            out.append({"role": role, "parts": parts})

    for r in rows:
        role = "model" if r["role"] == "assistant" else "user"
        calls = (r.get("meta") or {}).get("calls") or []
        if calls and out:      # a call with nothing before it has nothing to answer
            push("model", [{"functionCall": c} for c in calls])
            # ponytail: the real tool result is not replayed — the reply text
            # already carries it and whole rows would crowd the window. Gemini
            # only needs the pairing to accept the history. Store the results
            # too if a turn ever needs to reason over an older one.
            push("user", [{"functionResponse": dict(
                {"id": c["id"]} if c.get("id") else {},
                name=c.get("name", ""), response={"ok": True})}
                for c in calls])
        push(role, [{"text": r["content"]}])
    return out


# Past-tense reports of a change. Tools that only READ cannot back one up.
DID = re.compile(r"\b(added|created|scheduled|rescheduled|moved|set|updated|"
                 r"changed|marked|completed|dropped|deleted|cancell?ed|canceled|"
                 r"removed|renamed|rolled|postponed)\b", re.I)
READ_ONLY = {"list_tasks", "day_status"}

CORRECTION = ("You reported a change but called no tool, so nothing actually "
              "happened and the user has been told it did. Call the tool now.")


def unbacked_claim(reply: str, called: list[dict]) -> bool:
    """The reply says something changed and no tool changed anything.

    The model does this intermittently — no wording in the prompt stops it,
    because it is not a misunderstanding, it is the model finishing a turn one
    step early. It has to be caught here instead: this is the only failure the
    user cannot detect and cannot recover from, since they believe the
    confirmation, stop tracking the thing, and it is simply gone.
    """
    if any(c.get("name") not in READ_ONLY for c in called):
        return False                      # something really was written
    return bool(DID.search(reply or ""))


def correction(reply: str, called: list[dict], content: dict) -> list[dict] | None:
    """The two turns that hand an unbacked claim back to the model, or None
    when the reply is honest. Lives here with the rest of "what a model turn
    means", so turn.py keeps only the sequencing."""
    if not unbacked_claim(reply, called):
        return None
    audit.step("unbacked", claimed=reply)
    return [content, {"role": "user", "parts": [{"text": CORRECTION}]}]


def calls_in(parts: list[dict]) -> list[dict]:
    """The functionCall blocks of one model turn. Gemini may emit several."""
    return [p["functionCall"] for p in parts if "functionCall" in p]


def text_of(parts: list[dict]) -> str:
    """The visible reply. A turn can mix thought signatures and text parts."""
    return "".join(p["text"] for p in parts if "text" in p).strip()


def dispatch(calls: list[dict], tools: list) -> dict:
    """Run the model's function calls and return the turn that answers them.

    Lives here rather than in turn.py because knowing how to invoke a tool is
    this module's job — turn.py only sequences the turn. A tool that raises
    comes back as a result the model can read: killing the turn over one bad
    argument loses the user's message, while telling the model it failed lets
    it correct itself.
    """
    parts = []
    for call in calls:
        name = call.get("name", "")
        args = call.get("args") or {}          # Gemini sends a dict, not JSON text
        fn = next((t for t in tools if t.name == name), None)
        if fn is None:
            result = {"error": f"unknown tool: {name}"}
        else:
            try:
                result = fn.call(args)
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
        # Gemini gives every call an id and pairs the response to it by that
        # id. Answering without one leaves the call unmatched, and the next
        # step comes back MALFORMED_FUNCTION_CALL — no text, no call, a turn
        # that silently does nothing.
        response = {"name": name, "response": json.loads(json.dumps(result, default=str))}
        if call.get("id"):
            response["id"] = call["id"]
        parts.append({"functionResponse": response})
    return {"role": "user", "parts": parts}
