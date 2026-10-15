"""Pure Atlas page model and owner-bound encryption envelope for the page lifecycle in
atlas_service.py; validates slugs, titles, bodies, and sources, derives stable page
identities, and seals/opens page bodies with the host Fernet key.
"""

from __future__ import annotations

import binascii
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from cryptography.fernet import Fernet, InvalidToken

FORMAT = "astral.atlas-page/v1"
KIND_PAGE = "page"
KIND_REVISION = "revision"
MAX_TITLE_BYTES = 256
MAX_SLUG_CHARACTERS = 128
MAX_BODY_BYTES = 8192
MAX_PLAINTEXT_BYTES = MAX_TITLE_BYTES + MAX_BODY_BYTES + MAX_SLUG_CHARACTERS + 4096
MAX_CIPHERTEXT_BYTES = 16384
MAX_SOURCES = 8
MAX_SOURCE_BYTES = 512
MAX_LINKS = 16
MAX_REVISION = 2**53 - 2
MAX_HISTORY = 20
GLOBAL_PROJECT = "-"
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.ASCII
)


class AtlasPageError(ValueError):
    def __init__(self, code: str, status_code: int = 422):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _bounded_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise AtlasPageError(f"{field}_must_be_text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise AtlasPageError(f"{field}_must_be_text") from None
    if not encoded or len(encoded) > maximum:
        raise AtlasPageError(f"{field}_out_of_bounds")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"} and character not in "\n\t"
        for character in value
    ):
        raise AtlasPageError(f"{field}_has_control_characters")
    return value


def normalize_project(project_id: str | None) -> str | None:
    if project_id is None or project_id == GLOBAL_PROJECT or not project_id.strip():
        return None
    normalized = project_id.strip()
    if len(normalized.encode("utf-8")) > 128:
        raise AtlasPageError("project_out_of_bounds")
    return normalized


def normalize_slug(value: str) -> str:
    slug = _bounded_text(value, field="slug", maximum=MAX_SLUG_CHARACTERS)
    slug = unicodedata.normalize("NFKD", slug).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", slug.strip().lower()).strip("-")
    if not slug or len(slug) < 2 or not _SLUG.fullmatch(slug):
        raise AtlasPageError("slug_invalid")
    return slug


def normalize_title(value: str) -> str:
    title = _bounded_text(value, field="title", maximum=MAX_TITLE_BYTES)
    title = unicodedata.normalize("NFC", title).strip()
    if not title:
        raise AtlasPageError("title_required")
    return title


def normalize_body(value: str) -> str:
    if not isinstance(value, str):
        raise AtlasPageError("body_must_be_text")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    body = _bounded_text(value, field="body", maximum=MAX_BODY_BYTES)
    body = unicodedata.normalize("NFC", body)
    if not body.strip():
        raise AtlasPageError("body_required")
    return body


def normalize_sources(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_SOURCES:
        raise AtlasPageError("sources_out_of_bounds")
    sources: list[str] = []
    for entry in value:
        source = _bounded_text(entry, field="source", maximum=MAX_SOURCE_BYTES)
        source = source.strip()
        if source and source not in sources:
            sources.append(source)
    return tuple(sources)


def page_identity(owner_id: str, project_id: str | None, slug: str) -> str:
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise AtlasPageError("owner_required")
    if not isinstance(slug, str) or not slug.strip():
        raise AtlasPageError("slug_required")
    scope = "" if project_id is None else project_id
    if not isinstance(scope, str):
        raise AtlasPageError("project_required")
    digest = uuid5(NAMESPACE_URL, f"{FORMAT}\0{owner_id}\0{scope}\0{slug}")
    return str(digest)


def revision_identity(page_id: str, revision: int, token: str) -> str:
    digest = uuid5(
        NAMESPACE_URL, f"{FORMAT}\0revision\0{page_id}\0{revision}\0{token}"
    )
    return str(digest)


def canonical_page_id(value: str) -> str | None:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class PageSealed:
    page_id: str
    owner_id: str
    project_id: str | None
    slug: str
    title: str
    revision: int
    sources: tuple[str, ...]
    ciphertext: str


def _seal_payload(sealed: PageSealed, body: str) -> bytes:
    return _canonical_bytes(
        {
            "format": FORMAT,
            "page_id": sealed.page_id,
            "owner_id": sealed.owner_id,
            "project_id": sealed.project_id,
            "slug": sealed.slug,
            "revision": sealed.revision,
            "title": sealed.title,
            "sources": list(sealed.sources),
            "body": body,
        }
    )


class PageCipher:
    def __init__(self, fernet: Fernet):
        if not isinstance(fernet, Fernet):
            raise AtlasPageError("cipher_unavailable", 503)
        self._fernet = fernet

    def seal(self, sealed: PageSealed, body: str) -> str:
        if not isinstance(sealed, PageSealed) or not isinstance(body, str):
            raise AtlasPageError("page_seal_input_invalid")
        try:
            token = self._fernet.encrypt(_seal_payload(sealed, body))
        except (InvalidToken, ValueError, TypeError):
            raise AtlasPageError("page_seal_failed", 503) from None
        if len(token) > MAX_CIPHERTEXT_BYTES:
            raise AtlasPageError("page_body_out_of_bounds")
        return token.decode("ascii")

    def open(self, sealed: PageSealed, ciphertext: str) -> str:
        if not isinstance(sealed, PageSealed) or not isinstance(ciphertext, str):
            raise AtlasPageError("page_open_input_invalid")
        try:
            payload = self._fernet.decrypt(ciphertext.encode("ascii"))
        except (binascii.Error, InvalidToken, ValueError, UnicodeEncodeError):
            raise AtlasPageError("page_decrypt_failed", 503) from None
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AtlasPageError("page_payload_invalid", 503) from None
        if not isinstance(decoded, dict) or decoded.get("format") != FORMAT:
            raise AtlasPageError("page_payload_invalid", 503)
        identity = (
            decoded.get("page_id"),
            decoded.get("owner_id"),
            decoded.get("project_id"),
            decoded.get("slug"),
            decoded.get("revision"),
            decoded.get("title"),
            tuple(decoded.get("sources") or ()),
        )
        expected = (
            sealed.page_id,
            sealed.owner_id,
            sealed.project_id,
            sealed.slug,
            sealed.revision,
            sealed.title,
            sealed.sources,
        )
        if identity != expected or not isinstance(decoded.get("body"), str):
            raise AtlasPageError("page_binding_mismatch", 503)
        return decoded["body"]


def envelope_bytes(sealed: PageSealed, *, kind: str, prev_revision_id: str | None) -> str:
    if kind not in {KIND_PAGE, KIND_REVISION}:
        raise AtlasPageError("envelope_kind_invalid")
    payload = {
        "format": FORMAT,
        "kind": kind,
        "page_id": sealed.page_id,
        "owner_id": sealed.owner_id,
        "project_id": sealed.project_id,
        "slug": sealed.slug,
        "revision": sealed.revision,
        "title": sealed.title,
        "sources": list(sealed.sources),
        "body": sealed.ciphertext,
        "prev_revision_id": prev_revision_id,
    }
    encoded = _canonical_bytes(payload)
    if len(encoded) > MAX_CIPHERTEXT_BYTES:
        raise AtlasPageError("page_out_of_bounds")
    return encoded.decode("utf-8")


def parse_envelope(value: object) -> dict | None:
    if not isinstance(value, str) or not value.startswith('{"body":'):
        return None
    if len(value) > MAX_CIPHERTEXT_BYTES:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(decoded, dict)
        or decoded.get("format") != FORMAT
        or decoded.get("kind") not in {KIND_PAGE, KIND_REVISION}
        or not isinstance(decoded.get("page_id"), str)
        or not isinstance(decoded.get("owner_id"), str)
        or not isinstance(decoded.get("revision"), int)
        or not isinstance(decoded.get("body"), str)
    ):
        return None
    return decoded


def page_keywords(slug: str, title: str) -> str:
    tokens = re.findall(r"[a-z0-9]{3,}", f"{slug} {(title or '').lower()}")
    return " ".join(dict.fromkeys(tokens))[:256]


def envelope_fingerprint(envelope: dict) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "page_id": envelope.get("page_id"),
                "revision": envelope.get("revision"),
                "title": envelope.get("title"),
                "slug": envelope.get("slug"),
                "body": envelope.get("body"),
            }
        )
    ).hexdigest()
