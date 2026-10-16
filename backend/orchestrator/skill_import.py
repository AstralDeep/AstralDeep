"""Governed, instructions-only import of Anthropic Agent Skills (SKILL.md) into the
Astral user skill catalog.

This module implements GitHub issue AstralDeep/AstralDeep#334: a bounded,
standards-aware intake that maps one SKILL.md instruction body through the existing
skill catalog and settings surface.

Security and scope boundaries (issue #334 acceptance checks):
- Parses ONE explicitly supplied UTF-8 text. It NEVER scans arbitrary repositories
  or home directories, NEVER follows symlinks outside the supplied file, NEVER
  fetches referenced URLs, NEVER extracts archives, and NEVER executes content.
- YAML frontmatter is bounded: hard byte cap, flat (no nested mapping keys; flat
  lists permitted), no anchors/aliases, per-field length cap, and a capped body.
- ``allowed-tools``, ``compatibility``, command metadata, embedded role claims and
  prose are treated as UNTRUSTED. They never grant permissions, register
  tools/connectors, install scripts/hooks, or create agents.
- Resource-dependent or executable skills (``references``, ``scripts``, ``hooks`` …)
  are reported as unsupported for instructions-only intake.
- Source identity, attribution and license are preserved in the preview. Missing or
  restrictive licenses are NOT assumed permissive; import requires explicit owner
  permission.
- Saving is performed ONLY through the existing current-human authoring facade
  (``UserSkillFacade.save``). This module introduces no new catalog, schema, or
  model-authored save bypass. Imports default DISABLED until the owner enables them.

Deterministic and offline: depends only on the Python standard library.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# Limits mirror orchestrator/user_skills.py (Astral current skill constraints).
MAX_NAME_CHARS = 60
MAX_INSTRUCTIONS_CHARS = 4000

# Bounds for the bounded intake itself (issue #334 acceptance checks).
DEFAULT_MAX_BYTES = 1 << 16                 # 65 536 bytes hard cap on supplied input
DEFAULT_MAX_FIELD_CHARS = 4096              # per-frontmatter-field value length
DEFAULT_MAX_YAML_DEPTH = 0                  # flat only: no nested mapping keys
DEFAULT_MAX_BODY_CHARS = MAX_INSTRUCTIONS_CHARS

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_URL_RE = re.compile(r"https?://\S+")
_ANCHOR_ALIAS_RE = re.compile(r"(?:^|\s)[&*][A-Za-z_]")

# Frontmatter keys that map onto Astral catalog fields.
_MAPPED_KEYS = {"name", "description"}
# Keys that are explicitly untrusted and must never grant effects.
_UNTRUSTED_KEYS = {"allowed-tools", "allowed_tools", "compatibility"}
# Keys that indicate resource-dependent or executable skills (out of scope here).
_RESOURCE_OR_EXEC_KEYS = {
    "references", "scripts", "hooks", "source", "files", "attachments",
}
# Heuristic markers for possible prompt-injection / privilege-escalation attempts.
_INJECTION_MARKERS = (
    "ignore previous instructions", "ignore all previous", "disregard",
    "you are now", "system prompt", "system:", "assume you are",
    "override", "bypass", "disable safety", "ignore your",
)
# Licenses treated as permissive for default import. Anything else (Proprietary,
# GPL-family, or unknown) requires explicit owner permission.
_PERMISSIVE_LICENSES = {
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "bsd-3", "isc",
    "unlicense", "cc0-1.0", "0bsd", "mit-0",
}


class SkillImportError(ValueError):
    """Bounded-intake error carrying a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ParsedSkill:
    name: str
    description: str
    instructions: str
    license: Optional[str]
    raw_metadata: Dict[str, Any]
    untrusted_keys: Tuple[str, ...]
    resource_keys: Tuple[str, ...]
    body_resource_links: Tuple[str, ...]
    suspicious_instructions: bool
    source_identity: Dict[str, Any]


@dataclass(frozen=True)
class ImportPreview:
    name: str
    description: str
    instructions: str
    license: Optional[str]
    license_status: str                 # 'permitted' | 'missing' | 'restrictive'
    supported: Dict[str, Any]
    unsupported: Dict[str, Any]
    warnings: Tuple[str, ...]
    can_import: bool                    # False when owner permission is required


def slugify(name: str) -> str:
    """Reproduce orchestrator/user_skills.slugify semantics for preview display."""
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug[:48] or "skill"


def _count_depth(text: str) -> int:
    """Maximum mapping indentation level (top-level keys are level 0).

    Flat sequence items (``- value``) do NOT add mapping depth: a flat list such as
    ``allowed-tools:`` followed by ``- Read`` is permitted, while a nested mapping
    key (indented ``key:``) is rejected.
    """
    depth = 0
    for raw in text.splitlines():
        stripped = raw.lstrip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            continue
        if ":" not in stripped:
            continue
        indent = len(raw) - len(stripped)
        depth = max(depth, indent // 2)
    return depth


def _has_alias_or_anchor(text: str) -> bool:
    return bool(_ANCHOR_ALIAS_RE.search(text))


def _parse_flat_frontmatter(text: str, *, max_field_chars: int) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    current_key: Optional[str] = None
    list_buf: Optional[list] = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.lstrip().startswith("- "):
            if current_key is None:
                raise SkillImportError("yaml_malformed", "List item without a parent key.")
            item = raw.lstrip()[2:].strip()
            if len(item) > max_field_chars:
                raise SkillImportError(
                    "field_too_long",
                    f"A list item exceeds {max_field_chars} characters.",
                )
            if list_buf is None:
                list_buf = []
                meta[current_key] = list_buf
            list_buf.append(item)
            continue
        if ":" not in raw:
            raise SkillImportError("yaml_malformed", f"Unparseable line: {raw!r}")
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            raise SkillImportError("yaml_malformed", "Empty frontmatter key.")
        list_buf = None
        current_key = key
        if len(value) > max_field_chars:
            raise SkillImportError(
                "field_too_long",
                f"Field '{key}' exceeds {max_field_chars} characters.",
            )
        meta[key] = value
    return meta


def _extract_resource_links(instructions: str) -> Tuple[str, ...]:
    return tuple(sorted(set(_URL_RE.findall(instructions))))


def parse_agent_skill(
    text: Any,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
    max_yaml_depth: int = DEFAULT_MAX_YAML_DEPTH,
    max_instructions_chars: int = DEFAULT_MAX_BODY_CHARS,
) -> ParsedSkill:
    """Parse one explicitly supplied SKILL.md into a :class:`ParsedSkill`.

    Raises :class:`SkillImportError` on any bounded-intake violation. Never performs
    filesystem, network, archive, or execution side effects.
    """
    if isinstance(text, str):
        data = text.encode("utf-8")
    elif isinstance(text, (bytes, bytearray)):
        data = bytes(text)
    else:
        raise SkillImportError("invalid_input", "Input must be str or bytes.")

    if len(data) > max_bytes:
        raise SkillImportError(
            "input_too_large",
            f"Supplied input is {len(data)} bytes; the bounded intake limit is {max_bytes}.",
        )
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillImportError("invalid_encoding", "Input must be valid UTF-8.") from exc

    match = _FRONTMATTER_RE.match(decoded)
    if not match:
        raise SkillImportError(
            "no_frontmatter",
            "SKILL.md must begin with a YAML frontmatter block delimited by '---'.",
        )
    fm_text, body = match.group(1), match.group(2)

    if _has_alias_or_anchor(fm_text):
        raise SkillImportError(
            "yaml_alias_forbidden",
            "YAML anchors/aliases are not permitted during intake.",
        )
    if _count_depth(fm_text) > max_yaml_depth:
        raise SkillImportError(
            "yaml_depth_exceeded",
            f"Frontmatter nesting exceeds the allowed depth of {max_yaml_depth}.",
        )

    meta = _parse_flat_frontmatter(fm_text, max_field_chars=max_field_chars)

    name = (meta.get("name") or "").strip()
    if not name:
        raise SkillImportError("missing_name", "SKILL.md frontmatter requires a 'name'.")
    if len(name) > MAX_NAME_CHARS:
        raise SkillImportError(
            "name_too_long",
            f"Name is {len(name)} characters; Astral limits names to {MAX_NAME_CHARS}. "
            "Edit the source before import (it is not silently truncated).",
        )

    description = (meta.get("description") or "").strip()
    instructions = body.strip()
    if len(instructions) > max_instructions_chars:
        raise SkillImportError(
            "instructions_too_long",
            f"Instructions are {len(instructions)} characters; Astral limits them to "
            f"{max_instructions_chars}. Edit the source before import "
            "(it is not silently truncated).",
        )

    license_value = meta.get("license")
    if license_value is not None:
        license_value = str(license_value).strip() or None

    raw_metadata = {
        k: v for k, v in meta.items() if k not in ("name", "description", "license")
    }
    untrusted_keys = tuple(sorted(k for k in raw_metadata if k in _UNTRUSTED_KEYS))
    resource_keys = tuple(sorted(k for k in raw_metadata if k in _RESOURCE_OR_EXEC_KEYS))
    body_links = _extract_resource_links(instructions)
    suspicious = any(marker in instructions.lower() for marker in _INJECTION_MARKERS)

    source_identity = {
        "name": name,
        "description": description,
        "license": license_value,
        "upstream_format": "agent-skills",
    }

    return ParsedSkill(
        name=name,
        description=description,
        instructions=instructions,
        license=license_value,
        raw_metadata=raw_metadata,
        untrusted_keys=untrusted_keys,
        resource_keys=resource_keys,
        body_resource_links=body_links,
        suspicious_instructions=suspicious,
        source_identity=source_identity,
    )


def _license_status(license_value: Optional[str]) -> str:
    if not license_value:
        return "missing"
    if license_value.lower() in _PERMISSIVE_LICENSES:
        return "permitted"
    return "restrictive"


def build_preview(parsed: ParsedSkill) -> ImportPreview:
    """Build an explicit, human-owned preview of what would be imported.

    Unsupported metadata and warnings are surfaced verbatim so the owner can decide
    before saving a revision. ``can_import`` is False when owner permission is needed
    (missing or restrictive license).
    """
    license_status = _license_status(parsed.license)

    supported = {
        "name": parsed.name,
        "description": parsed.description,
        "instructions": parsed.instructions,
    }

    unsupported: Dict[str, Any] = {}
    if parsed.untrusted_keys:
        unsupported["untrusted_keys"] = parsed.untrusted_keys
    if parsed.resource_keys:
        unsupported["resource_dependent_keys"] = parsed.resource_keys
    if parsed.body_resource_links:
        unsupported["body_resource_links"] = parsed.body_resource_links
    if parsed.raw_metadata:
        # Anything not explicitly mapped/untrusted/resource is preserved but inert.
        preserved = {
            k: v
            for k, v in parsed.raw_metadata.items()
            if k not in parsed.untrusted_keys and k not in parsed.resource_keys
        }
        if preserved:
            unsupported["preserved_inert_metadata"] = preserved

    warnings: list = []
    if parsed.untrusted_keys:
        warnings.append(
            "Untrusted metadata (allowed-tools/compatibility/command metadata) is "
            "ignored: it grants no permissions, registers no tools, and installs nothing."
        )
    if parsed.resource_keys or parsed.body_resource_links:
        warnings.append(
            "Instructions reference resources/scripts; only the instruction body is "
            "imported. This is NOT a complete import of the upstream skill."
        )
    if parsed.suspicious_instructions:
        warnings.append(
            "Instructions contain phrases resembling prompt injection; review before enabling."
        )
    if license_status != "permitted":
        warnings.append(
            f"License status is '{license_status}'; explicit owner permission is required "
            "before import (missing or restrictive licenses are not assumed permissive)."
        )

    can_import = license_status == "permitted"
    return ImportPreview(
        name=parsed.name,
        description=parsed.description,
        instructions=parsed.instructions,
        license=parsed.license,
        license_status=license_status,
        supported=supported,
        unsupported=unsupported,
        warnings=tuple(warnings),
        can_import=can_import,
    )


async def import_instructions_only(
    facade: Any,
    caller: Any,
    parsed: ParsedSkill,
    *,
    permit_nonpermissive: bool = False,
    command_id: Optional[str] = None,
) -> ImportPreview:
    """Persist one parsed skill through the existing authoring facade.

    The save is performed ONLY via ``facade.save`` (the current-human authoring
    facade), with ``enabled=False`` (imports default disabled). Upstream exceptions
    raised by the facade (reserved name, conflict, auth) propagate unchanged.
    """
    preview = build_preview(parsed)
    if not preview.can_import and not permit_nonpermissive:
        raise SkillImportError(
            "license_requires_review",
            f"License status '{preview.license_status}' requires explicit owner "
            "permission (pass permit_nonpermissive=True).",
        )

    cid = command_id or uuid.uuid4().hex
    # New skill: expected_revision=0, empty skill_id/slug (facade derives the slug).
    await facade.save(
        caller=caller,
        skill_id="",
        command_id=cid,
        expected_revision=0,
        name=parsed.name,
        instructions=parsed.instructions,
        applies_to=("always",),
        command="",
        enabled=False,
        slug="",
    )
    return preview


async def import_skill_text(
    facade: Any,
    caller: Any,
    text: Any,
    *,
    permit_nonpermissive: bool = False,
    command_id: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
    max_yaml_depth: int = DEFAULT_MAX_YAML_DEPTH,
    max_instructions_chars: int = DEFAULT_MAX_BODY_CHARS,
) -> ImportPreview:
    """Parse, preview, and import a SKILL.md text in one call.

    Raises :class:`SkillImportError` on any bounded-intake violation or when owner
    permission is required for a non-permissive license.
    """
    parsed = parse_agent_skill(
        text,
        max_bytes=max_bytes,
        max_field_chars=max_field_chars,
        max_yaml_depth=max_yaml_depth,
        max_instructions_chars=max_instructions_chars,
    )
    return await import_instructions_only(
        facade,
        caller,
        parsed,
        permit_nonpermissive=permit_nonpermissive,
        command_id=command_id,
    )
