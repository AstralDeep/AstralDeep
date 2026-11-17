"""Closed record types for skill revisions and explicit notes; skills keep immutable
history, notes keep only current ciphertext or a deletion tombstone. Carries no
execution authority; consumed by repositories/guidance.py and assignments.py.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field

from astralplane.repositories import RepositoryValidationError

MAX_REVISION = 2**53 - 1
MAX_TIME = 2**63 - 1
_CATEGORIES = frozenset({"profession", "goal", "preference", "workflow_tag", "context"})


def integer(value: object, *, minimum: int = 0, maximum: int = MAX_REVISION) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RepositoryValidationError("guidance integer outside declared bounds")
    return value


def identifier(value: object) -> str:
    try:
        parsed = uuid.UUID(value) if type(value) is str else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise RepositoryValidationError("guidance identity must be canonical UUID4")
    return value


def text(value: object, *, minimum: int = 1, maximum: int = 256, multiline=False) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        raise RepositoryValidationError("guidance text outside declared bounds")
    if any(
        unicodedata.category(c) in {"Cc", "Cs"} and not (multiline and c in "\n\t\r") for c in value
    ):
        raise RepositoryValidationError("guidance text contains invalid characters")
    return value


def owner(value: object) -> str:
    value = text(value)
    if value != value.strip() or len(value.encode("utf-8")) > 1024:
        raise RepositoryValidationError("invalid guidance owner")
    return value


def digest(value: object) -> str:
    if type(value) is not str or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise RepositoryValidationError("invalid guidance digest")
    return value


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def slug(value: object) -> str:
    if type(value) is not str or re.fullmatch("[a-z0-9][a-z0-9-]{0,47}", value) is None:
        raise RepositoryValidationError("invalid skill slug")
    return value


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    name: str
    instructions: str = field(repr=False)
    applies_to: tuple[str, ...] = ()
    alias: str = ""
    enabled: bool = True
    format_version: int = 1

    def __post_init__(self):
        text(self.name, minimum=2, maximum=60)
        text(self.instructions, minimum=10, maximum=4000, multiline=True)
        if (
            type(self.format_version) is not int
            or self.format_version != 1
            or type(self.enabled) is not bool
        ):
            raise RepositoryValidationError("unsupported skill definition")
        if (
            type(self.applies_to) is not tuple
            or len(self.applies_to) > 8
            or len(set(self.applies_to)) != len(self.applies_to)
        ):
            raise RepositoryValidationError("invalid skill applicability")
        for item in self.applies_to:
            if type(item) is not str or re.fullmatch("[A-Za-z0-9_.:-]{1,64}", item) is None:
                raise RepositoryValidationError("invalid skill applicability")
        if type(self.alias) is not str or (
            self.alias and re.fullmatch("[a-z][a-z0-9_-]{0,23}", self.alias) is None
        ):
            raise RepositoryValidationError("invalid skill alias")

    @property
    def definition_digest(self) -> str:
        return sha(asdict(self))


@dataclass(frozen=True, slots=True)
class SkillCommand:
    owner_id: str = field(repr=False)
    skill_id: str
    command_id: str
    command: str
    expected_revision: int
    slug: str | None = None
    definition: SkillDefinition | None = field(default=None, repr=False)

    def __post_init__(self):
        owner(self.owner_id)
        identifier(self.skill_id)
        identifier(self.command_id)
        integer(self.expected_revision, maximum=MAX_REVISION - 1)
        if type(self.command) is not str or self.command not in {"create", "replace", "delete"}:
            raise RepositoryValidationError("invalid skill command")
        if self.command == "create":
            if self.expected_revision != 0:
                raise RepositoryValidationError("skill creation requires revision zero")
            slug(self.slug)
        elif self.slug is not None or self.expected_revision == 0:
            raise RepositoryValidationError("invalid skill revision command")
        if self.command == "delete":
            if self.definition is not None:
                raise RepositoryValidationError("skill deletion must not include content")
        elif type(self.definition) is not SkillDefinition:
            raise RepositoryValidationError("typed skill definition required")

    @property
    def request_digest(self) -> str:
        return sha({"version": 1, "kind": "owner_skill_command", **asdict(self)})


@dataclass(frozen=True, slots=True)
class SkillHead:
    owner_id: str = field(repr=False)
    skill_id: str
    slug: str
    revision: int
    name: str
    alias: str
    applies_to: tuple[str, ...]
    enabled: bool
    definition_digest: str
    created_at: int
    updated_at: int
    deleted_at: int | None = None


@dataclass(frozen=True, slots=True)
class SkillRevisionRecord:
    owner_id: str = field(repr=False)
    skill_id: str
    revision: int
    definition: SkillDefinition = field(repr=False)
    definition_digest: str
    created_at: int
    deleted: bool = False
    legacy_markdown: bytes | None = field(default=None, repr=False)
    legacy_digest: str | None = None


@dataclass(frozen=True, slots=True)
class SkillReceipt:
    owner_id: str = field(repr=False)
    skill_id: str
    command_id: str
    command: str
    request_digest: str
    revision: int
    created_at: int


@dataclass(frozen=True, slots=True)
class SkillPreparation:
    command: SkillCommand = field(repr=False)
    head: SkillHead | None = field(repr=False)
    revision: SkillRevisionRecord | None = field(repr=False)
    receipt: SkillReceipt | None
    replayed: bool
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class SkillChangeResult:
    head: SkillHead = field(repr=False)
    revision: SkillRevisionRecord | None = field(repr=False)
    receipt: SkillReceipt
    replayed: bool


@dataclass(frozen=True, slots=True)
class LegacySkillEntry:
    skill_id: str
    slug: str
    definition: SkillDefinition = field(repr=False)
    markdown: bytes = field(repr=False)
    format: str = "deep_owner_markdown_v1"
    legacy_updated_at: int = 0

    def __post_init__(self):
        identifier(self.skill_id)
        slug(self.slug)
        integer(self.legacy_updated_at, maximum=MAX_TIME)
        if type(self.definition) is not SkillDefinition or self.format != "deep_owner_markdown_v1":
            raise RepositoryValidationError("invalid legacy skill definition")
        if type(self.markdown) is not bytes or not 1 <= len(self.markdown) <= 32768:
            raise RepositoryValidationError("invalid legacy skill byte geometry")
        try:
            self.markdown.decode("utf-8")
        except UnicodeError as exc:
            raise RepositoryValidationError("legacy skill is not UTF-8") from exc


@dataclass(frozen=True, slots=True)
class LegacySkillMapping:
    slug: str
    skill_id: str
    revision: int
    legacy_digest: str
    definition_digest: str


@dataclass(frozen=True, slots=True)
class SkillMaterialization:
    owner_id: str = field(repr=False)
    manifest_digest: str
    mappings: tuple[LegacySkillMapping, ...]
    created_at: int
    replayed: bool


def legacy_manifest(entries: tuple[LegacySkillEntry, ...]) -> str:
    if (
        type(entries) is not tuple
        or len(entries) > 20
        or any(type(e) is not LegacySkillEntry for e in entries)
    ):
        raise RepositoryValidationError("invalid legacy skill catalog")
    if len({e.slug for e in entries}) != len(entries) or len({e.skill_id for e in entries}) != len(
        entries
    ):
        raise RepositoryValidationError("duplicate legacy skill identity")
    aliases = [e.definition.alias for e in entries if e.definition.alias]
    if len(set(aliases)) != len(aliases):
        raise RepositoryValidationError("duplicate legacy skill alias")
    return sha(
        {
            "version": 1,
            "entries": [
                {
                    "slug": e.slug,
                    "format": e.format,
                    "legacy_digest": hashlib.sha256(e.markdown).hexdigest(),
                    "definition_digest": e.definition.definition_digest,
                    "legacy_updated_at": e.legacy_updated_at,
                }
                for e in sorted(entries, key=lambda e: e.slug)
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class ExplicitNoteRecord:
    owner_id: str = field(repr=False)
    note_id: str
    revision: int
    category: str
    enabled: bool
    created_at: int
    updated_at: int
    ciphertext: bytes = field(repr=False)
    expires_at: int | None = None
    format_version: int = 1

    def __post_init__(self):
        owner(self.owner_id)
        identifier(self.note_id)
        integer(self.revision, minimum=1, maximum=MAX_REVISION - 1)
        integer(self.created_at, maximum=MAX_REVISION)
        integer(self.updated_at, minimum=self.created_at, maximum=MAX_REVISION)
        if self.expires_at is not None:
            integer(self.expires_at, minimum=self.updated_at + 1, maximum=MAX_REVISION)
        if (
            type(self.category) is not str
            or self.category not in _CATEGORIES
            or type(self.enabled) is not bool
        ):
            raise RepositoryValidationError("invalid explicit note metadata")
        if type(self.format_version) is not int or self.format_version != 1:
            raise RepositoryValidationError("unsupported explicit note version")
        if type(self.ciphertext) is not bytes or not 1 <= len(self.ciphertext) <= 16384:
            raise RepositoryValidationError("invalid explicit note ciphertext geometry")


@dataclass(frozen=True, slots=True)
class ExplicitNoteTombstone:
    owner_id: str = field(repr=False)
    note_id: str
    revision: int
    deleted_at: int
    deleted_reason: str

    def __post_init__(self):
        owner(self.owner_id)
        identifier(self.note_id)
        integer(self.revision, minimum=1)
        integer(self.deleted_at, maximum=MAX_REVISION)
        if type(self.deleted_reason) is not str or self.deleted_reason not in {
            "forgotten",
            "expired",
        }:
            raise RepositoryValidationError("invalid explicit note deletion reason")


@dataclass(frozen=True, slots=True)
class ExplicitNotePreparation:
    owner_id: str = field(repr=False)
    note_id: str
    expected_revision: int
    current: ExplicitNoteRecord | ExplicitNoteTombstone | None = field(repr=False)
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class ExplicitNoteRetirementPreparation:
    owner_id: str = field(repr=False)
    note_id: str
    expected_revision: int
    reason: str
    current: ExplicitNoteRecord | ExplicitNoteTombstone = field(repr=False)
    observed_at_ms: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class NoteExpiryCursor:
    expires_at: int
    owner_id: str = field(repr=False)
    note_id: str


@dataclass(frozen=True, slots=True)
class NoteExpiryCandidate:
    owner_id: str = field(repr=False)
    note_id: str
    revision: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class NoteExpiryPage:
    records: tuple[NoteExpiryCandidate, ...]
    cutoff_ms: int
    next_cursor: NoteExpiryCursor | None


@dataclass(frozen=True, slots=True)
class GuidanceReference:
    kind: str
    resource_id: str
    revision: int

    def __post_init__(self):
        if type(self.kind) is not str or self.kind not in {"skill", "note"}:
            raise RepositoryValidationError("invalid guidance kind")
        identifier(self.resource_id)
        integer(self.revision, minimum=1)


def legacy_directory_digest(owner_id: str, entries: tuple[LegacySkillEntry, ...]) -> str:
    owner(owner_id)
    legacy_manifest(entries)
    return sha(
        {
            "version": 1,
            "kind": "owner_skill_legacy_manifest",
            "owner_id": owner_id,
            "files": [
                {"filename": e.slug + ".md", "sha256": hashlib.sha256(e.markdown).hexdigest()}
                for e in sorted(entries, key=lambda e: e.slug + ".md")
            ],
        }
    )
