"""Per-turn context. Tools take only the arguments the model supplies, so the
user and the outbound buffer travel out-of-band.

Imported by capabilities and by core/turn.py. Imports only audit, which
imports only db — so the registry still cannot import in a circle.
"""

from contextvars import ContextVar

from . import audit
from .channels.base import Button

_user: ContextVar[dict] = ContextVar("user")
_outbox: ContextVar[list] = ContextVar("outbox")


def user() -> dict:
    return _user.get()


def card(text: str, buttons: list[Button] | None = None) -> None:
    """Queue a message for app.py to send after the model's reply."""
    _outbox.get().append((text, buttons))
    # Cards go out after the reply, so judging the reply alone judges half the
    # output — and this is the only record of what the buttons actually were.
    audit.step("card", text=text, buttons=[b.label for b in buttons or []])


def begin(u: dict) -> None:
    _user.set(u)
    _outbox.set([])


def drain() -> list:
    """The cards queued during this turn, in the order they were rendered."""
    return _outbox.get([])


_source: ContextVar[str] = ContextVar("source", default="chat")


def source() -> str:
    """'chat' | 'voice' — tasks.source, set by app.py before the turn runs."""
    return _source.get()


def set_source(s: str) -> None:
    _source.set(s)
