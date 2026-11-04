"""Small shared helpers for ElevenLabs-style voice-segment audio tags."""

from __future__ import annotations

import re


VOICE_AUDIO_TAG_RE = re.compile(
    r"\[(?!(?:voice|br|audio)\b)(?![a-z]{2}\])"
    r"[a-z][a-z0-9'’_-]*(?:[ \t,]+[a-z][a-z0-9'’_-]*){0,9}\](?![ \t]*\()",
    re.IGNORECASE | re.ASCII,
)


def strip_voice_audio_tags(text: str) -> str:
    """Remove bracketed English audio tags from text known to be a voice segment."""
    cleaned = VOICE_AUDIO_TAG_RE.sub("", text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([,.;:!?，。！？；：])", r"\1", cleaned)
    cleaned = re.sub(r"(?m)^[ \t]+|[ \t]+$", "", cleaned)
    return cleaned.strip()
