"""Strict, pure preparation for controlled legacy user-skill materialization.

The caller supplies bounded bytes captured from verified regular files. This
module never opens a path, changes the current file store, grants owner authority
or asserts that capture/SQL commit was atomic. Future cutover must hold its writer
and owner fences and compare this exact manifest before making Plane authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata

from .user_skills import (
    ALWAYS, MAX_APPLIES_TO, MAX_INSTRUCTIONS_CHARS, MAX_NAME_CHARS, MAX_SKILLS,
    Skill, render_markdown,
)

MAX_LEGACY_MARKDOWN_BYTES = 32768
MAX_RESERVED_ALIASES = 128
_MAX_LEGACY_TIME = 2**63 - 1
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-?")
_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,23}")
_AGENT = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_INTEGER = re.compile(r"0|[1-9][0-9]*")
_FIELDS = frozenset({"name", "type", "owner", "slug", "command", "applies_to", "enabled", "updated_at"})


class LegacySkillMaterializationError(ValueError):
    """Closed conflict without owner, filename, alias or instruction text."""

    def __init__(self):
        super().__init__("legacy_skill_materialization_conflict")


def _text(value, minimum, maximum, *, multiline=False):
    try:
        if (type(value) is not str or not minimum <= len(value) <= maximum
                or value != value.strip()
                or any(unicodedata.category(c) in {"Cc", "Cs"}
                       and not (multiline and c in "\n\t") for c in value)):
            raise LegacySkillMaterializationError()
        value.encode("utf-8")
    except UnicodeError:
        raise LegacySkillMaterializationError() from None


def _owner(value):
    _text(value, 1, 256)
    if len(value.encode("utf-8")) > 1024:
        raise LegacySkillMaterializationError()


def _slug(value):
    if type(value) is not str or not 1 <= len(value) <= 48 or not _SLUG.fullmatch(value):
        raise LegacySkillMaterializationError()


@dataclass(frozen=True, slots=True)
class LegacySkillFile:
    """Supplied regular-file bytes, with a filename only, never an arbitrary path."""

    filename: str
    markdown: bytes = field(repr=False)

    def __post_init__(self):
        if type(self.filename) is not str or not self.filename.endswith(".md"):
            raise LegacySkillMaterializationError()
        _slug(self.filename[:-3])
        if type(self.markdown) is not bytes or not 1 <= len(self.markdown) <= MAX_LEGACY_MARKDOWN_BYTES:
            raise LegacySkillMaterializationError()


@dataclass(frozen=True, slots=True)
class LegacySkillDefinition:
    """Validated source interpretation, shaped for the forthcoming Plane definition."""

    format_version: int
    name: str
    instructions: str = field(repr=False)
    applies_to: tuple[str, ...]
    alias: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class LegacySkillSnapshot:
    """Exact original bytes and interpretation; it creates no historical revision."""

    slug: str
    definition: LegacySkillDefinition
    markdown: bytes = field(repr=False)
    legacy_updated_at: int
    format: str = "deep_owner_markdown_v1"


@dataclass(frozen=True, slots=True)
class PreparedLegacySkills:
    """Immutable materialization input; no UUID allocation, writes or authority."""

    owner_id: str
    entries: tuple[LegacySkillSnapshot, ...]
    manifest_bytes: bytes = field(repr=False)
    manifest_digest: str


def _quoted_string(value):
    parsed = json.loads(value)
    if type(parsed) is not str:
        # Objects (including duplicate-key objects), arrays and coercions are
        # never metadata values in the historical Markdown format.
        raise LegacySkillMaterializationError()
    return parsed


def _snapshot(owner, file, reserved):
    file.__post_init__()
    try:
        text = file.markdown.decode("utf-8")
        if "\r" in text:
            if re.search(r"(?<!\r)\n|\r(?!\n)", text):
                raise LegacySkillMaterializationError()
            text = text.replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise LegacySkillMaterializationError()
        header, separator, body = text[4:].partition("\n---\n")
        if not separator:
            raise LegacySkillMaterializationError()
        fields = {}
        for line in header.split("\n"):
            key, colon, value = line.partition(": ")
            if not colon or key not in _FIELDS or key in fields:
                raise LegacySkillMaterializationError()
            fields[key] = value
        if set(fields) != _FIELDS or fields["type"] != "user_skill":
            raise LegacySkillMaterializationError()
        if _quoted_string(fields["owner"]) != owner:
            raise LegacySkillMaterializationError()
        slug = fields["slug"]
        _slug(slug)
        if file.filename != slug + ".md":
            raise LegacySkillMaterializationError()
        name = _quoted_string(fields["name"])
        _text(name, 2, MAX_NAME_CHARS)
        prefix = "\n## Instructions\n\n"
        if not body.startswith(prefix) or not body.endswith("\n"):
            raise LegacySkillMaterializationError()
        instructions = body[len(prefix):-1]
        _text(instructions, 10, MAX_INSTRUCTIONS_CHARS, multiline=True)
        alias = fields["command"]
        if alias and (not _ALIAS.fullmatch(alias) or alias in reserved):
            raise LegacySkillMaterializationError()
        if fields["enabled"] not in {"true", "false"}:
            raise LegacySkillMaterializationError()
        enabled = fields["enabled"] == "true"
        applies_raw = fields["applies_to"]
        if not applies_raw.startswith("[") or not applies_raw.endswith("]"):
            raise LegacySkillMaterializationError()
        applies = tuple(applies_raw[1:-1].split(", "))
        if (not 1 <= len(applies) <= MAX_APPLIES_TO or len(set(applies)) != len(applies)
                or any(not _AGENT.fullmatch(agent) for agent in applies)
                or (ALWAYS in applies and applies != (ALWAYS,))):
            raise LegacySkillMaterializationError()
        raw_time = fields["updated_at"]
        if not _INTEGER.fullmatch(raw_time) or len(raw_time) > 19:
            raise LegacySkillMaterializationError()
        updated = int(raw_time)
        if updated > _MAX_LEGACY_TIME:
            raise LegacySkillMaterializationError()
        skill = Skill(slug, name, instructions, applies, alias, enabled, updated)
        if render_markdown(skill, owner) != text:
            # Validate the actual historical writer format without rewriting
            # captured bytes or normalizing away malformed/ambiguous metadata.
            raise LegacySkillMaterializationError()
        return LegacySkillSnapshot(slug, LegacySkillDefinition(
            1, name, instructions, applies, alias, enabled), file.markdown, updated)
    except (UnicodeError, ValueError, RecursionError):
        raise LegacySkillMaterializationError() from None


def prepare_legacy_skill_materialization(
    owner_id: str, *, files: tuple[LegacySkillFile, ...], reserved_aliases: tuple[str, ...],
) -> PreparedLegacySkills:
    """Prepare a complete owner manifest, rejecting conflicting or lossy inputs.

    Empty input has a real owner-bound manifest. UUIDs and database timestamps
    are deliberately absent: Plane's eventual idempotent marker must return its
    original mappings on retry, never import this snapshot over later edits.
    The caller must verify filesystem eligibility and freshness separately.
    """
    _owner(owner_id)
    if (type(files) is not tuple or len(files) > MAX_SKILLS
            or any(type(file) is not LegacySkillFile for file in files)
            or type(reserved_aliases) is not tuple or len(reserved_aliases) > MAX_RESERVED_ALIASES
            or any(type(alias) is not str or not _ALIAS.fullmatch(alias) for alias in reserved_aliases)
            or len(set(reserved_aliases)) != len(reserved_aliases)):
        raise LegacySkillMaterializationError()
    if len({file.filename for file in files}) != len(files):
        raise LegacySkillMaterializationError()
    ordered = sorted(files, key=lambda file: file.filename)
    entries = tuple(_snapshot(owner_id, file, reserved_aliases) for file in ordered)
    aliases = [item.definition.alias for item in entries if item.definition.alias]
    if len(set(aliases)) != len(aliases):
        raise LegacySkillMaterializationError()
    manifest = {"version": 1, "kind": "owner_skill_legacy_manifest", "owner_id": owner_id,
                "files": [{"filename": file.filename, "sha256": hashlib.sha256(file.markdown).hexdigest()}
                          for file in ordered]}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode("utf-8")
    return PreparedLegacySkills(owner_id, entries, raw, hashlib.sha256(raw).hexdigest())
