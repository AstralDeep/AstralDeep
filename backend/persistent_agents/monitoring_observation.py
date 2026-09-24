"""Classifies a fresh governed read against its prior observation as initial, unchanged,
changed, or insufficient_evidence; pure and source-free. Consumed by
persistent_agents/runner.py using research_result.py's page facts.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from persistent_agents.dispatch_context import canonical
from persistent_agents.models import validate_id
from persistent_agents.research_result import (
    MAX_RESULT_BYTES,
    build_page_result,
    page_passages,
)
from persistent_agents.runtime_values import digest, thaw

OBSERVATION_VERSION = 1
KINDS = frozenset({"initial", "unchanged", "changed", "insufficient_evidence"})
INCORPORATED_KINDS = frozenset({"initial", "changed"})
MAX_OBSERVATION_BYTES = 20480
MAX_PASSAGES = 8
PASSAGE_CHARACTERS = 512
_DIGEST = re.compile(r"[a-f0-9]{64}")
_LEGACY_KEYS = frozenset({"text", "revision_digest", "truncated", "redacted"})
_PAGE_KEYS = frozenset(
    {
        "version", "requested_url", "final_url", "retrieved_at", "media_type",
        "extraction_profile", "title", "text", "body_complete", "extraction_complete",
        "excerpt_complete", "redacted", "source_action_id", "revision_digest",
    }
)
_FLAGS = ("body_complete", "extraction_complete", "excerpt_complete")
_RECORD_KEYS = frozenset(
    {
        "version", "kind", "reason", "observation_sequence", "revision_digest",
        "context_digest", "prior_revision_digest", "prior_result_digest",
        "complete_source_set", "normalized_final_urls", "completeness",
        "source_configuration_digest",
    }
)
_ERROR = "assignment_observation_invalid"
_RESULT_ERROR = "assignment_research_result_invalid"


def _is_digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _optional_digest(value):
    return value if _is_digest(value) else None


def _sequence(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def observation_shape(observed):
    if type(observed) is not dict:
        return None
    keys = set(observed)
    if keys == _PAGE_KEYS and all(type(observed[flag]) is bool for flag in _FLAGS):
        return "page"
    if (keys == _LEGACY_KEYS and type(observed["text"]) is str
            and type(observed["truncated"]) is bool and type(observed["redacted"]) is bool):
        return "legacy"
    return None


def normalize_url(url):
    if type(url) is not str or not url.strip() or len(url.encode("utf-8")) > 8192:
        raise ValueError(_ERROR)
    parts = urlsplit(url)
    if not parts.scheme or parts.hostname is None:
        raise ValueError(_ERROR)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != {"https": 443, "http": 80}.get(scheme):
        host = f"{host}:{port}"
    userinfo = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
    return urlunsplit((scheme, userinfo + host, parts.path or "/", parts.query, ""))


def extraction_facts(observed, source):
    shape = observation_shape(observed)
    source = thaw(source)
    if shape is None or type(source) is not dict:
        raise ValueError(_ERROR)
    arguments = source.get("arguments")
    requested = arguments.get("url") if isinstance(arguments, dict) else None
    if shape == "page":
        flags = {flag: observed[flag] for flag in _FLAGS}
        requested = observed["requested_url"]
        final_urls = [normalize_url(observed["final_url"])]
    else:
        flags = {"body_complete": None, "extraction_complete": None,
                 "excerpt_complete": not observed["truncated"]}
        final_urls = []
    if type(requested) is not str or not requested.strip():
        raise ValueError(_ERROR)
    return {
        "shape": shape,
        "revision_digest": _optional_digest(observed.get("revision_digest")),
        "requested_url": requested,
        "normalized_final_urls": final_urls,
        "source_configuration_digest": digest(source),
        **flags,
    }


def _prior_binding(prior):
    if prior is None:
        return None
    if (type(prior) is not dict or set(prior) != {"revision_digest", "result_digest", "sequence"}
            or not _sequence(prior["sequence"])
            or any(prior[key] is not None and not _is_digest(prior[key])
                   for key in ("revision_digest", "result_digest"))):
        raise ValueError(_ERROR)
    return prior


def _record(*, kind, reason, sequence, revision, context_digest, prior, facts):
    incomplete = facts["body_complete"] is False or facts["extraction_complete"] is False
    record = {
        "version": OBSERVATION_VERSION,
        "kind": kind,
        "reason": reason,
        "observation_sequence": sequence,
        "revision_digest": revision,
        "context_digest": context_digest,
        "prior_revision_digest": None if prior is None else prior["revision_digest"],
        "prior_result_digest": None if prior is None else prior["result_digest"],
        "complete_source_set": [] if incomplete else [facts["requested_url"]],
        "normalized_final_urls": list(facts["normalized_final_urls"]),
        "completeness": {flag: facts[flag] for flag in _FLAGS},
        "source_configuration_digest": facts["source_configuration_digest"],
    }
    if len(canonical(record).encode("utf-8")) > MAX_OBSERVATION_BYTES:
        raise ValueError(_ERROR)
    return record


def classify_observation(prior, observed, extraction):
    prior = _prior_binding(prior)
    if observation_shape(observed) is None or type(extraction) is not dict:
        raise ValueError(_ERROR)
    facts = extraction
    revision = facts["revision_digest"]
    if revision != _optional_digest(observed.get("revision_digest")):
        raise ValueError(_ERROR)
    context_digest = digest(observed)
    sequence = 0 if prior is None else prior["sequence"]
    if facts["body_complete"] is False or facts["extraction_complete"] is False:
        kind, reason = "insufficient_evidence", "extraction_incomplete"
    elif revision is None:
        kind, reason = "insufficient_evidence", "revision_unavailable"
    elif prior is None:
        kind, reason, sequence = "initial", None, 1
    elif prior["revision_digest"] is None or prior["result_digest"] is None:
        kind, reason = "insufficient_evidence", "prior_result_missing"
    elif prior["revision_digest"] == revision:
        kind, reason = "unchanged", None
    else:
        kind, reason, sequence = "changed", None, prior["sequence"] + 1
    return _record(kind=kind, reason=reason, sequence=sequence, revision=revision,
                   context_digest=context_digest, prior=prior, facts=facts)


def event_observation(checkpoint, observed, extraction, *, sequence, revision):
    if (observation_shape(observed) is None or type(extraction) is not dict
            or not _sequence(sequence) or sequence < 1 or not _is_digest(revision)
            or extraction["revision_digest"] != revision):
        raise ValueError(_ERROR)
    prior = prior_observation(
        checkpoint, source_configuration_digest=extraction["source_configuration_digest"],
        pending_sequence=sequence)
    return _record(kind="initial" if prior is None else "changed", reason=None,
                   sequence=sequence, revision=revision, context_digest=digest(observed),
                   prior=prior, facts=extraction)


def valid_observation_record(value):
    if (type(value) is not dict or set(value) != _RECORD_KEYS
            or value["version"] != OBSERVATION_VERSION or value["kind"] not in KINDS
            or not _sequence(value["observation_sequence"])
            or not _is_digest(value["source_configuration_digest"])
            or type(value["complete_source_set"]) is not list
            or type(value["normalized_final_urls"]) is not list
            or type(value["completeness"]) is not dict):
        return False
    for key in ("revision_digest", "context_digest", "prior_revision_digest", "prior_result_digest"):
        if value[key] is not None and not _is_digest(value[key]):
            return False
    if value["kind"] in INCORPORATED_KINDS and (
            value["revision_digest"] is None or value["context_digest"] is None):
        return False
    return True


def prior_observation(checkpoint, *, source_configuration_digest, pending_sequence=None):
    checkpoint = thaw(checkpoint)
    if type(checkpoint) is not dict:
        return None
    record = checkpoint.get("observation")
    if record is not None:
        if (not valid_observation_record(record)
                or record["source_configuration_digest"] != source_configuration_digest):
            return None
        if record["kind"] in INCORPORATED_KINDS:
            return {"revision_digest": record["revision_digest"],
                    "result_digest": record["context_digest"],
                    "sequence": record["observation_sequence"]}
        if record["prior_revision_digest"] is None or record["prior_result_digest"] is None:
            return None
        return {"revision_digest": record["prior_revision_digest"],
                "result_digest": record["prior_result_digest"],
                "sequence": record["observation_sequence"]}
    last = checkpoint.get("last_observation")
    last_revision = (_optional_digest(last.get("revision_digest"))
                     if observation_shape(last) is not None else None)
    if pending_sequence is not None:
        if pending_sequence <= 1:
            return None
        return {"revision_digest": last_revision,
                "result_digest": digest(last) if last_revision is not None else None,
                "sequence": pending_sequence - 1}
    cursor = checkpoint.get("cursor")
    if (type(cursor) is not dict or set(cursor) != {"revision", "sequence"}
            or not _is_digest(cursor["revision"]) or not _sequence(cursor["sequence"])
            or cursor["sequence"] < 1):
        return None
    bound = last_revision is not None and last_revision == cursor["revision"]
    return {"revision_digest": cursor["revision"],
            "result_digest": digest(last) if bound else None,
            "sequence": cursor["sequence"]}


def extractive_passages(text):
    if type(text) is not str:
        raise ValueError(_RESULT_ERROR)
    start, passages = 0, []
    while start < len(text):
        end = min(start + PASSAGE_CHARACTERS, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start + PASSAGE_CHARACTERS // 2, end)
            if boundary >= 0:
                end = boundary + 1
        passages.append({"id": f"p{len(passages) + 1:03d}", "text": text[start:end]})
        start = end
    return passages


def build_initial_result(observed, extraction, *, source_action_id, source_result_digest):
    shape = observation_shape(observed)
    if shape == "page":
        selection = [item["id"] for item in page_passages(observed)[:MAX_PASSAGES]]
        return build_page_result(observed, selection, source_action_id=source_action_id,
                                 source_result_digest=source_result_digest)
    try:
        if (shape != "legacy" or type(extraction) is not dict
                or extraction.get("shape") != "legacy"
                or not _is_digest(source_result_digest)
                or not _is_digest(observed["revision_digest"])):
            raise ValueError
        validate_id(source_action_id)
        passages = extractive_passages(observed["text"])[:MAX_PASSAGES]
        result = {
            "version": 1,
            "scope": "one_page_excerpts",
            "disposition": "evidence",
            "source": {
                "requested_url": extraction["requested_url"],
                "action_id": source_action_id,
                "result_digest": source_result_digest,
                "revision_digest": observed["revision_digest"],
                "excerpt_complete": not observed["truncated"],
                "redacted": observed["redacted"],
            },
            "passages": passages,
        }
        while len(canonical(result).encode("utf-8")) > MAX_RESULT_BYTES and result["passages"]:
            result["passages"] = result["passages"][:-1]
        if not any(item["text"].strip() for item in result["passages"]):
            result["passages"] = []
            result["disposition"] = "insufficient_evidence"
        if len(canonical(result).encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError(_RESULT_ERROR) from None
