"""Bounded source snapshots, deterministic Atlas promotion text, and owner-bound approval
tokens for atlas_service.py; every value is normalized here before it can influence a
proposal, while the service remains responsible for source and destination authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from personalization.atlas_pages import (
    MAX_BODY_BYTES,
    MAX_SOURCES,
    AtlasPageError,
    normalize_title,
)

PROMOTION_FORMAT = "astral.atlas-promotion/v1"
SOURCE_NOTE = "note"
SOURCE_MEMORY = "memory"
SOURCE_KINDS = frozenset({SOURCE_NOTE, SOURCE_MEMORY})
MAX_PROMOTION_SOURCES = 4
MAX_SOURCE_VALUE_BYTES = 4096
MAX_PROMOTION_TOKEN_BYTES = 65536
PROMOTION_TTL_MS = 15 * 60 * 1000
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.ASCII
)


@dataclass(frozen=True, slots=True)
class PromotionSource:
    kind: str
    source_id: str
    revision: int
    category: str
    digest: str
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PromotionSourceInput:
    kind: str
    source_id: str
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class PromotionDestination:
    page_id: str
    slug: str
    revision: int | None
    fingerprint: str | None


@dataclass(frozen=True, slots=True)
class PromotionProposal:
    proposal_id: str
    proposal_digest: str
    approval_token: str = field(repr=False)
    expires_at: int
    owner_id: str
    project_id: str | None
    action: str
    sources: tuple[PromotionSource, ...]
    destination: PromotionDestination
    title: str
    body: str = field(repr=False)
    source_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromotionApplied:
    proposal_id: str
    page: object
    sources: tuple[str, ...]


def canonical_source_identifier(value: object) -> str:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise AtlasPageError("promotion_source_invalid")
    try:
        identifier = str(UUID(value))
    except ValueError:
        raise AtlasPageError("promotion_source_invalid") from None
    if identifier != value:
        raise AtlasPageError("promotion_source_invalid")
    return value


def normalize_source_kind(value: object) -> str:
    if value not in SOURCE_KINDS:
        raise AtlasPageError("promotion_source_invalid")
    return value


def normalize_source_revision(value: object, *, required: bool = True) -> int | None:
    if value is None and not required:
        return None
    if type(value) is not int or value < 0 or value >= 2**53:
        raise AtlasPageError("promotion_source_revision_invalid")
    return value


def normalize_source_value(value: object) -> str:
    if not isinstance(value, str):
        raise AtlasPageError("promotion_source_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise AtlasPageError("promotion_source_invalid") from None
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if (
        not normalized
        or len(encoded) > MAX_SOURCE_VALUE_BYTES
        or len(normalized.encode("utf-8")) > MAX_SOURCE_VALUE_BYTES
        or any(
            unicodedata.category(character) in {"Cc", "Cs"}
            and character not in "\n\t"
            for character in normalized
        )
    ):
        raise AtlasPageError("promotion_source_out_of_bounds")
    return normalized


def source_digest(kind: str, source_id: str, revision: int, category: str, value: str) -> str:
    payload = {
        "kind": kind,
        "id": source_id,
        "revision": revision,
        "category": category,
        "value": value,
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def source_reference(source: PromotionSource) -> str:
    return f"{source.kind}:{source.source_id}@{source.revision}"


def source_snapshot(
    *, kind: str, source_id: str, revision: int, category: str, value: str
) -> PromotionSource:
    normalized_kind = normalize_source_kind(kind)
    normalized_id = canonical_source_identifier(source_id)
    normalized_revision = normalize_source_revision(revision)
    if normalized_revision is None:
        raise AtlasPageError("promotion_source_revision_invalid")
    normalized_value = normalize_source_value(value)
    if not isinstance(category, str) or not category or len(category.encode("utf-8")) > 64:
        raise AtlasPageError("promotion_source_invalid")
    return PromotionSource(
        kind=normalized_kind,
        source_id=normalized_id,
        revision=normalized_revision,
        category=category,
        digest=source_digest(
            normalized_kind,
            normalized_id,
            normalized_revision,
            category,
            normalized_value,
        ),
        value=normalized_value,
    )


def promotion_title(sources: tuple[PromotionSource, ...], requested: str | None) -> str:
    if requested is not None:
        return normalize_title(requested)
    if not sources:
        raise AtlasPageError("promotion_sources_required")
    if len(sources) == 1:
        category = sources[0].category.replace("_", " ").title()
        noun = "Note" if sources[0].kind == SOURCE_NOTE else "Memory"
        return normalize_title(f"Promoted {category} {noun}")
    return normalize_title("Promoted Atlas sources")


def promotion_section(sources: tuple[PromotionSource, ...], *, title: str) -> str:
    if not sources or len(sources) > MAX_PROMOTION_SOURCES:
        raise AtlasPageError("promotion_sources_out_of_bounds")
    lines = [f"## {normalize_title(title)}"]
    for source in sources:
        label = source.category.replace("_", " ").title()
        lines.extend(
            (
                "",
                f"### {label}",
                source.value,
                "",
            )
        )
    section = "\n".join(lines).strip()
    if not section:
        raise AtlasPageError("promotion_body_required")
    return section


def promotion_body(
    sources: tuple[PromotionSource, ...],
    *,
    title: str,
    existing_body: str | None,
) -> str:
    section = promotion_section(sources, title=title)
    body = section if existing_body is None else f"{existing_body}\n\n{section}"
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise AtlasPageError("promotion_body_out_of_bounds")
    return body


def promotion_source_references(
    sources: tuple[PromotionSource, ...], existing: tuple[str, ...] = ()
) -> tuple[str, ...]:
    references = list(existing)
    for source in sources:
        reference = source_reference(source)
        if reference not in references:
            references.append(reference)
    if len(references) > MAX_SOURCES:
        raise AtlasPageError("promotion_sources_out_of_bounds")
    return tuple(references)


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def proposal_payload_bytes(payload: dict) -> bytes:
    return _canonical_bytes(payload)


def proposal_digest(payload: dict) -> str:
    return hashlib.sha256(proposal_payload_bytes(payload)).hexdigest()


def seal_proposal(fernet: Fernet, payload: dict) -> str:
    if not isinstance(fernet, Fernet) or not isinstance(payload, dict):
        raise AtlasPageError("promotion_approval_unavailable", 503)
    raw = proposal_payload_bytes(payload)
    if len(raw) > MAX_PROMOTION_TOKEN_BYTES:
        raise AtlasPageError("promotion_body_out_of_bounds")
    try:
        token = fernet.encrypt(raw).decode("ascii")
    except (ValueError, TypeError):
        raise AtlasPageError("promotion_approval_unavailable", 503) from None
    if len(token) > MAX_PROMOTION_TOKEN_BYTES * 2:
        raise AtlasPageError("promotion_body_out_of_bounds")
    return token


def open_proposal(
    fernet: Fernet,
    token: object,
    *,
    owner_id: str,
    project_id: str | None,
    now_ms: int,
) -> dict:
    if (
        not isinstance(fernet, Fernet)
        or not isinstance(token, str)
        or not 1 <= len(token) <= MAX_PROMOTION_TOKEN_BYTES * 2
        or not isinstance(owner_id, str)
        or not owner_id
        or type(now_ms) is not int
    ):
        raise AtlasPageError("promotion_approval_invalid")
    try:
        raw = fernet.decrypt(token.encode("ascii"))
        payload = json.loads(raw)
    except (UnicodeEncodeError, InvalidToken, ValueError, TypeError, RecursionError):
        raise AtlasPageError("promotion_approval_invalid") from None
    if (
        type(payload) is not dict
        or payload.get("v") != 1
        or payload.get("format") != PROMOTION_FORMAT
        or payload.get("owner") != owner_id
        or payload.get("project") != project_id
    ):
        raise AtlasPageError("promotion_approval_invalid")
    try:
        UUID(payload["proposal_id"])
        created = payload["created"]
        expires = payload["expires"]
        sources = payload["sources"]
        destination = payload["destination"]
        if (
            type(created) is not int
            or type(expires) is not int
            or expires <= created
            or not isinstance(payload["action"], str)
            or payload["action"] not in {"create", "append"}
            or not isinstance(payload["title"], str)
            or not isinstance(payload["body"], str)
            or not isinstance(payload["source_references"], list)
            or not isinstance(sources, list)
            or not 1 <= len(sources) <= MAX_PROMOTION_SOURCES
            or not isinstance(destination, dict)
            or set(destination) != {"page_id", "slug", "revision", "fingerprint"}
        ):
            raise ValueError
        for source in sources:
            if (
                type(source) is not dict
                or set(source) != {"kind", "id", "revision", "category", "digest"}
                or source["kind"] not in SOURCE_KINDS
                or canonical_source_identifier(source["id"]) != source["id"]
                or normalize_source_revision(source["revision"]) != source["revision"]
                or not isinstance(source["category"], str)
                or not isinstance(source["digest"], str)
                or len(source["digest"]) != 64
            ):
                raise ValueError
        if (
            canonical_source_identifier(destination["page_id"]) != destination["page_id"]
            or not isinstance(destination["slug"], str)
            or (
                destination["revision"] is not None
                and (type(destination["revision"]) is not int or destination["revision"] < 1)
            )
            or (
                destination["fingerprint"] is not None
                and (not isinstance(destination["fingerprint"], str)
                     or len(destination["fingerprint"]) != 64)
            )
            or any(not isinstance(reference, str) for reference in payload["source_references"])
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise AtlasPageError("promotion_approval_invalid") from None
    if now_ms >= expires:
        raise AtlasPageError("promotion_expired", 409)
    return payload


__all__ = (
    "MAX_PROMOTION_SOURCES",
    "MAX_PROMOTION_TOKEN_BYTES",
    "MAX_SOURCE_VALUE_BYTES",
    "PROMOTION_FORMAT",
    "PROMOTION_TTL_MS",
    "SOURCE_KINDS",
    "SOURCE_MEMORY",
    "SOURCE_NOTE",
    "PromotionApplied",
    "PromotionDestination",
    "PromotionProposal",
    "PromotionSource",
    "PromotionSourceInput",
    "canonical_source_identifier",
    "normalize_source_kind",
    "normalize_source_revision",
    "normalize_source_value",
    "open_proposal",
    "promotion_body",
    "promotion_section",
    "promotion_source_references",
    "promotion_title",
    "proposal_digest",
    "proposal_payload_bytes",
    "seal_proposal",
    "source_digest",
    "source_reference",
    "source_snapshot",
)