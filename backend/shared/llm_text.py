"""Strips Harmony-style `<|channel|>`/`<|message|>` control markup and `<think>`
reasoning blocks that leak into model output when a serving stack fails to consume
them; used before text reaches the user.
"""

from __future__ import annotations

import re

_NAMES = r"(?:start|channel|message|constrain|end|return|call)"
_TOKEN = re.compile(rf"<\|{_NAMES}\|>|<\|{_NAMES}>|<{_NAMES}\|>", re.IGNORECASE)
_CHANNEL = r"(?:<\|channel\|>|<\|channel>|<channel\|>)"
_MESSAGE = r"(?:<\|message\|>|<\|message>|<message\|>)"

_REASONING_BLOCK = re.compile(
    rf"{_CHANNEL}\s*(?:analysis|thought|thinking|commentary|reflection)\b"
    rf".*?(?={_CHANNEL}|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_FINAL_HEADER = re.compile(rf"{_CHANNEL}\s*final\s*{_MESSAGE}?", re.IGNORECASE)
_START_ROLE = re.compile(
    r"(?:<\|start\|>|<\|start>|<start\|>)\s*(?:assistant|user|system|tool)?",
    re.IGNORECASE,
)
_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL)


def strip_reasoning_markup(text):
    if not isinstance(text, str) or "<" not in text:
        return text
    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = _REASONING_BLOCK.sub("", cleaned)
    cleaned = _FINAL_HEADER.sub("", cleaned)
    cleaned = _START_ROLE.sub("", cleaned)
    cleaned = _TOKEN.sub("", cleaned)
    cleaned = cleaned.strip()
    if cleaned:
        return cleaned
    fallback = _TOKEN.sub("", _THINK_BLOCK.sub("", text)).strip()
    return fallback
