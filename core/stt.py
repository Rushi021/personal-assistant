"""Voice is a message type, not a channel (D1). Groq whisper-large-v3-turbo,
$0.04 per hour of audio. Telegram sends no transcript, so every note is sent.
"""

import os

import httpx

URL = "https://api.groq.com/openai/v1/audio/transcriptions"


async def transcribe(audio: bytes) -> str:
    # An unset key otherwise reaches httpx as the header "Bearer " and comes
    # back as LocalProtocolError, which names neither Groq nor the key.
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set — voice notes cannot be transcribed")
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(
            URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-large-v3-turbo", "response_format": "json"},
        )
        r.raise_for_status()
        return r.json()["text"].strip()
