"""Typed privacy views for source text: reviewed URLs keep identifying components;
everything else fails closed via the ordinary PHI/injection gate. Used by
execution.py and runner.py before source content reaches a model or audit log.
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
    views = []

    def replace(match):
        token = match.group()
        url = token.rstrip(".,;:!)]}")
        if url not in urls:
            return token
        decoded = url
        for _ in range(5):
            if re.search(r"%(?![0-9a-fA-F]{2})", decoded):
                raise ValueError("assignment_source_encoding_refused")
            views.append(decoded.replace("://", ": //"))
            # Second view stops separators from hiding names from PHI scan
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
