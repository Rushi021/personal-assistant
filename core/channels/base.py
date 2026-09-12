"""The channel contract — D11.

Four methods, two dataclasses. Nothing outside core/channels/ may import a
channel module. If `grep -rn "telegram" core/turn.py capabilities/` ever
returns a line, the seam is already broken.

The three WhatsApp ceilings are adopted now because they cannot be retrofitted:
at most 3 buttons, labels <= 20 chars, payloads <= 64 bytes, plain text only,
and a sent message is never edited.
"""

from dataclasses import dataclass
from typing import Protocol

MAX_BUTTONS = 3
MAX_LABEL = 20
MAX_PAYLOAD = 64


@dataclass
class Inbound:
    channel: str                # "telegram" | "whatsapp"
    channel_user_id: str        # chat id, or E.164
    channel_msg_id: str         # idempotency key -> messages.channel_msg_id
    text: str | None = None
    voice_ref: str | None = None       # opaque handle; only the adapter fetches it
    button_payload: str | None = None  # set => fast path, D4


@dataclass
class Button:
    label: str
    payload: str

    def __post_init__(self):
        # A ceiling that fails loudly here is a ceiling you cannot ship past.
        assert len(self.label) <= MAX_LABEL, f"label >{MAX_LABEL} chars: {self.label}"
        assert len(self.payload.encode()) <= MAX_PAYLOAD, f"payload >{MAX_PAYLOAD} bytes"


class Channel(Protocol):
    name: str

    def verify(self, headers: dict, body: bytes) -> bool: ...
    def parse(self, body: dict) -> Inbound | None: ...
    async def fetch_voice(self, voice_ref: str) -> bytes: ...
    async def send(self, to: str, text: str, buttons: list[Button] | None = None) -> None: ...
    async def ack(self, channel_msg_id: str) -> None: ...  # button tap seen; no-op where absent
