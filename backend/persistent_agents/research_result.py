"""Closed retained page facts and deterministic one-page excerpt results.

These pure helpers neither authorize a source nor execute research. Callers must
use the ordinary reader and its current, authentic action result. Completeness is
relative to the named text extractor, never a claim about a full visual page.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from persistent_agents.models import SourceSelection, canonical, digest, validate_id

MAX_RESULT_BYTES = 8192
_RAW_KEYS = frozenset(
    {
        "version",
        "requested_url",
        "final_url",
        "retrieved_at",
        "media_type",
        "extraction_profile",
        "title",
        "text",
        "body_complete",
        "extraction_complete",
        "excerpt_complete",
        "redacted",
    }
)
_BOUND_KEYS = frozenset({"source_action_id", "revision_digest"})
_FLAGS = ("body_complete", "extraction_complete", "excerpt_complete", "redacted")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_SOURCE_ERROR = "assignment_source_observation_invalid"
_RESULT_ERROR = "assignment_research_result_invalid"


def _bytes(value):
    return canonical(value).encode("utf-8")


def _string(value, maximum, *, empty=False):
    if (
        type(value) is not str
        or len(value.encode("utf-8")) > maximum
        or (not empty and not value.strip())
    ):
        raise ValueError(_SOURCE_ERROR)


def _validate(value, *, retained=False):
    """Validate the closed factual domain before scanning or using its text."""
    if type(value) is not dict or set(value) != _RAW_KEYS | (
        _BOUND_KEYS if retained else set()
    ):
        raise ValueError(_SOURCE_ERROR)
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError(_SOURCE_ERROR)
    for key in _FLAGS:
        if type(value[key]) is not bool:
            raise ValueError(_SOURCE_ERROR)
    if not retained and value["redacted"]:
        raise ValueError(_SOURCE_ERROR)
    for key in ("requested_url", "final_url"):
        _string(value[key], 8192)
        SourceSelection(
            agent_id="web-research-1",
            tool_name="fetch_page",
            arguments={"url": value[key]},
        )
    _string(value["title"], 512, empty=True)
    _string(value["text"], 65536)
    if len(value["text"]) > 20000:
        raise ValueError(_SOURCE_ERROR)
    _string(value["retrieved_at"], 40)
    when = datetime.fromisoformat(value["retrieved_at"])
    if when.utcoffset() != timedelta(0):
        raise ValueError(_SOURCE_ERROR)
    profiles = {
        "text/html": "html_readable_v1",
        "application/xhtml+xml": "html_readable_v1",
        "text/plain": "plain_text_v1",
    }
    if (
        type(value["media_type"]) is not str
        or type(value["extraction_profile"]) is not str
        or profiles.get(value["media_type"]) != value["extraction_profile"]
    ):
        raise ValueError(_SOURCE_ERROR)
    if retained:
        validate_id(value["source_action_id"])
        if type(value["revision_digest"]) is not str or not _DIGEST.fullmatch(
            value["revision_digest"]
        ):
            raise ValueError(_SOURCE_ERROR)
    if len(_bytes(value)) > (MAX_RESULT_BYTES if retained else 65536):
        raise ValueError(_SOURCE_ERROR)
    return dict(value)


def legacy_page_response(response):
    """Remove only additive fixed-reader facts before historical normalization.

    The caller selects the exact fixed reader. Original UI, data and response
    remain untouched, preserving its previous generic digests and size limits.
    """
    result = getattr(response, "result", None)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("_data"), dict)
        or "page_observation" not in result["_data"]
    ):
        return response
    data = {
        key: value
        for key, value in result["_data"].items()
        if key != "page_observation"
    }
    return SimpleNamespace(
        error=getattr(response, "error", None),
        ui_components=getattr(response, "ui_components", None),
        result={**result, "_data": data},
    )


def read_page_observation(response, *, requested_url):
    """Validate actual fixed-reader metadata without guessing missing facts."""
    try:
        if getattr(response, "error", None):
            raise ValueError
        data = response.result["_data"]
        value = _validate(data["page_observation"])
        if (
            value["requested_url"] != requested_url
            or data["url"] != requested_url
            or data["title"] != value["title"]
            or type(data["characters"]) is not int
            or data["characters"] != len(value["text"])
            or type(data["truncated"]) is not bool
            or data["truncated"] == value["excerpt_complete"]
        ):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ValueError(_SOURCE_ERROR) from None


def retain_page_observation(value, *, source_action_id, redacted):
    """Fit already scanned/redacted prose while retaining exact source facts.

    The caller redacts only title/text and refuses unsafe metadata. No excerpt is
    selected until the entire existing bounded observation has passed its gates.
    """
    try:
        value = _validate(value)
        validate_id(source_action_id)
        if type(redacted) is not bool:
            raise ValueError
        result = {**value, "redacted": redacted, "source_action_id": source_action_id}
        result["revision_digest"] = digest({**value, "redacted": redacted})
        if len(_bytes(result)) <= MAX_RESULT_BYTES:
            return result
        # Canonical JSON escaping and multibyte text, not character count, decide
        # the retained domain. Only text may shrink; source metadata never does.
        text = result["text"]
        result["excerpt_complete"] = False
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            result["text"] = text[:middle]
            if len(_bytes(result)) <= MAX_RESULT_BYTES:
                low = middle
            else:
                high = middle - 1
        result["text"] = text[:low]
        if not result["text"].strip() or len(_bytes(result)) > MAX_RESULT_BYTES:
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ValueError(_SOURCE_ERROR) from None


def page_passages(observation):
    """Return stable, contiguous exact-text passages tied to a retained result."""
    try:
        value = _validate(observation, retained=True)
        text, start, passages = value["text"], 0, []
        while start < len(text):
            end = min(start + 512, len(text))
            if end < len(text):
                boundary = text.rfind(" ", start + 256, end)
                if boundary >= 0:
                    end = boundary + 1
            passages.append(
                {"id": f"p{len(passages) + 1:03d}", "text": text[start:end]}
            )
            start = end
        return passages
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ValueError(_SOURCE_ERROR) from None


def build_page_result(
    observation, selection, *, source_action_id, source_result_digest
):
    """Build exact attributed excerpts from one authentic available source result.

    Supplied action/digest must come from the caller's guarded ledger read. This
    pure equality check is not a replacement for owner or execution authority.
    Empty selection means insufficient evidence for this scoped excerpt profile.
    """
    try:
        value = _validate(observation, retained=True)
        if (
            source_action_id != value["source_action_id"]
            or type(source_result_digest) is not str
            or source_result_digest != digest(value)
            or type(selection) is not list
            or len(selection) > 8
            or any(type(item) is not str for item in selection)
            or len(set(selection)) != len(selection)
        ):
            raise ValueError
        available = {item["id"]: item for item in page_passages(value)}
        selected = [available[identity] for identity in selection]
        source = {key: value[key] for key in _RAW_KEYS - {"version", "text"}}
        source.update(
            action_id=source_action_id,
            result_digest=source_result_digest,
            revision_digest=value["revision_digest"],
        )
        result = {
            "version": 1,
            "scope": "one_page_excerpts",
            "disposition": "evidence" if selected else "insufficient_evidence",
            "source": source,
            "passages": selected,
        }
        if len(_bytes(result)) > MAX_RESULT_BYTES:
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ValueError(_RESULT_ERROR) from None
