"""Typed privacy views; reviewed URLs retain every identifying component.

Only the exact public resources in the validated owner definition receive a
classifier representation. The stored URL and dispatched arguments never change.
Unknown URLs and arbitrary reader data retain the ordinary fail-closed gate.
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote_plus

from persistent_agents.models import SourceSelection
from persistent_agents.runtime_values import thaw


def reviewed_urls(source) -> tuple[str, ...]:
    source = SourceSelection.model_validate(thaw(source))
    if source.profile != "public_page":
        return ()
    return (source.arguments["url"], *source.linked_document_urls)


def privacy_text(text: str, urls: tuple[str, ...] = ()) -> str:
    """Separate URL syntax from prose without discarding identifiers.

    Presidio can label a complete public URL as a location. Breaking the scheme
    delimiter avoids that syntactic false positive; hostname, path, query keys,
    and values still undergo the same detector, including decoded token views.
    No arbitrary URL-looking text is exempted and decoding is strictly bounded.
    """
    views = []

    def replace(match):
        token = match.group()
        # Markdown/prose closing punctuation is outside the URL token. Never
        # match a reviewed prefix of a longer path, hostname, query or fragment.
        url = token.rstrip(".,;:!)]}")
        if url not in urls:
            return token
        decoded = url
        for _ in range(5):
            if re.search(r"%(?![0-9a-fA-F]{2})", decoded):
                raise ValueError("assignment_source_encoding_refused")
            views.append(decoded.replace("://", ": //"))
            # A second view prevents separators from concealing names or labels.
            views.append(re.sub(r"[/_.?&=+%:-]+", " ", decoded))
            if "%" not in decoded:
                break
            decoded = unquote_plus(decoded, errors="strict")
        else:
            raise ValueError("assignment_source_encoding_refused")
        return token.replace("://", ": //", 1)

    text = re.sub(r"https://[^\s<>\"']+", replace, text)
    return "\n".join([text, *views])


def content_text(value, depth=0) -> str:
    """Scan raw leaves, including JSON nested in model message content.

    JSON escaping must not hide whitespace from the injection detector, nor
    manufacture name-like spans from escaped newlines for the PHI detector.
    """
    if depth > 20:
        raise ValueError("assignment_source_limit")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, (dict, list, str)) and decoded != value:
            return content_text(decoded, depth + 1)
        return value
    if isinstance(value, dict):
        return "\n".join(content_text(item, depth + 1)
                         for pair in value.items() for item in pair)
    if isinstance(value, (list, tuple)):
        return "\n".join(content_text(item, depth + 1) for item in value)
    return "" if value is None else str(value)


def redact_observation(value, gate):
    """Redact each bounded raw source leaf before hashing or retention."""
    changed = False

    def walk(node, depth=0):
        nonlocal changed
        if depth > 16:
            raise ValueError("assignment_source_limit")
        if isinstance(node, str):
            result, redacted = gate.redact_for_storage(node)
            changed |= redacted
            return result
        if isinstance(node, dict):
            result = {}
            for key, child in node.items():
                key = walk(key, depth + 1)
                if key in result:
                    raise ValueError("assignment_redaction_key_collision")
                result[key] = walk(child, depth + 1)
            return result
        if isinstance(node, list):
            return [walk(child, depth + 1) for child in node]
        return node

    result = walk(value)
    return result, changed


def model_evidence(observation):
    """Give models source prose and flags, keeping ledger digests in Plane.

    This is only for the executor's own result envelope, never arbitrary
    registered-reader dictionaries or model-created keys.
    """
    if observation is None:
        return None
    observation = thaw(observation)
    if (not isinstance(observation, dict)
            or not set(observation) <= {"text", "revision_digest", "truncated", "redacted"}
            or not isinstance(observation.get("text"), str)
            or any(type(observation[key]) is not bool for key in ("truncated", "redacted")
                   if key in observation)
            or ("revision_digest" in observation and not re.fullmatch(
                r"[a-f0-9]{64}", str(observation["revision_digest"])))):
        raise ValueError("assignment_evidence_invalid")
    return {key: value for key, value in observation.items() if key != "revision_digest"}
