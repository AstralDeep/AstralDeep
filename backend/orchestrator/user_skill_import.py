"""Bounded parser for one explicitly supplied Agent Skills SKILL.md body: it maps the
frontmatter name and instruction text onto the governed owner skill catalog and reports
every unsupported field for the authoring surface's import preview. Parsing refuses any
skill Astral cannot represent completely. It is pure and offline; saving happens only
through the current-human facade in user_skill_catalog.py.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import unicodedata

import yaml

from orchestrator import user_skills as legacy

MAX_SOURCE_BYTES = 64 * 1024
MAX_FRONTMATTER_CHARS = 16384
MAX_YAML_NODES = 512
MAX_YAML_ALIASES = 64
MAX_YAML_DEPTH = 16
MAX_NAME_CHARS = legacy.MAX_NAME_CHARS
MAX_INSTRUCTIONS_CHARS = legacy.MAX_INSTRUCTIONS_CHARS
MAX_UNSUPPORTED = 12
MAX_UNSUPPORTED_CHARS = 120
MAX_LICENSE_CHARS = 128
MAX_DESCRIPTION_CHARS = 400
MAX_LISTED_RESOURCES = 5

MAPPED_FIELDS = frozenset({"name", "description", "license", "allowed-tools", "metadata",
                           "compatibility"})

LICENSE_REFUSAL_MESSAGE = (
    "This skill's license is missing or is not recognized as permitting reuse, so it was "
    "not imported. If you have permission to reuse this material under its terms, add it "
    "with the Add-a-skill form instead.")

_PERMISSIVE_LICENSES = frozenset({
    "apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause", "isc", "unlicense",
    "cc0-1.0", "cc-by-2.0", "cc-by-3.0", "cc-by-4.0", "cc-by-sa-2.0", "cc-by-sa-3.0",
    "cc-by-sa-4.0",
})

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n)?(.*)\Z", re.DOTALL)
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_SCHEME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9+.\-]*:")


class SkillImportError(ValueError):
    def __init__(self, message, code="skill_import_invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SkillImportPreview:
    name: str
    instructions: str
    slug: str
    description: str
    license: str
    license_requires_confirmation: bool
    unsupported: tuple
    source_sha256: str
    source_bytes: int


class _BoundedParseError(ValueError):
    pass


class _BoundedLoader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self._nodes = 0
        self._aliases = 0
        self._depth = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            self._aliases += 1
            if self._aliases > MAX_YAML_ALIASES:
                raise _BoundedParseError(f"more than {MAX_YAML_ALIASES} YAML aliases")
        self._nodes += 1
        if self._nodes > MAX_YAML_NODES:
            raise _BoundedParseError(f"more than {MAX_YAML_NODES} YAML nodes")
        return super().compose_node(parent, index)

    def compose_mapping_node(self, anchor):
        self._depth += 1
        if self._depth > MAX_YAML_DEPTH:
            raise _BoundedParseError(f"YAML nesting deeper than {MAX_YAML_DEPTH} levels")
        try:
            return super().compose_mapping_node(anchor)
        finally:
            self._depth -= 1

    def compose_sequence_node(self, anchor):
        self._depth += 1
        if self._depth > MAX_YAML_DEPTH:
            raise _BoundedParseError(f"YAML nesting deeper than {MAX_YAML_DEPTH} levels")
        try:
            return super().compose_sequence_node(anchor)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag != "tag:yaml.org,2002:str":
                raise _BoundedParseError("frontmatter keys must be text")
            if key_node.value in seen:
                raise _BoundedParseError(f"duplicate frontmatter field {key_node.value!r}")
            seen.add(key_node.value)
        return super().construct_mapping(node, deep=deep)


def _bounded_bytes(source):
    if isinstance(source, bytes):
        raw = source
    elif isinstance(source, str):
        try:
            raw = source.encode("utf-8")
        except UnicodeEncodeError:
            raise SkillImportError("The supplied skill file is not valid UTF-8 text.",
                                   "skill_import_not_utf8") from None
    else:
        raise SkillImportError("Supply the SKILL.md contents as text or UTF-8 bytes.")
    if len(raw) == 0:
        raise SkillImportError("The supplied skill file is empty.")
    if len(raw) > MAX_SOURCE_BYTES:
        raise SkillImportError(f"The supplied skill file is {len(raw)} bytes; the import "
                               f"limit is {MAX_SOURCE_BYTES} bytes.", "skill_import_too_large")
    return raw


def _decode(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillImportError("The supplied skill file is not valid UTF-8.",
                               "skill_import_not_utf8") from None
    return text.removeprefix("\ufeff")


def _split_frontmatter(text):
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillImportError("The file must start with a YAML frontmatter block delimited "
                               "by --- lines, as the Agent Skills format requires.")
    frontmatter, body = match.group(1), match.group(2)
    if len(frontmatter) > MAX_FRONTMATTER_CHARS:
        raise SkillImportError(f"The YAML frontmatter is {len(frontmatter)} characters; the "
                               f"import limit is {MAX_FRONTMATTER_CHARS}.",
                               "skill_import_too_large")
    return frontmatter, body


def _load_frontmatter(frontmatter):
    try:
        data = yaml.load(frontmatter, Loader=_BoundedLoader)
    except _BoundedParseError as error:
        raise SkillImportError(f"The YAML frontmatter was refused: {error}.") from None
    except (yaml.YAMLError, RecursionError):
        raise SkillImportError("The YAML frontmatter could not be parsed within the "
                               "import bounds.") from None
    if type(data) is not dict:
        raise SkillImportError("The frontmatter must be a YAML mapping of field names to "
                               "values.")
    return data


def _refuse_control_characters(value, label, *, multiline):
    for char in value:
        if char in "\n\t\r" and multiline:
            continue
        if unicodedata.category(char) in {"Cc", "Cs"}:
            raise SkillImportError(f"The {label} contain control characters and cannot be "
                                   "imported. Remove them and import again.")


def _mapped_name(fields):
    value = fields.get("name")
    if value is None:
        raise SkillImportError("The frontmatter must set a name field.")
    if type(value) is not str:
        raise SkillImportError("The name field must be text.")
    name = value.strip()
    _refuse_control_characters(name, "name", multiline=False)
    if not 2 <= len(name) <= MAX_NAME_CHARS:
        raise SkillImportError(f"The name is {len(name)} characters; Astral skill names are "
                               f"2-{MAX_NAME_CHARS} characters and are never truncated. Edit "
                               "the file and import again.")
    return name


def _mapped_instructions(body):
    instructions = body.strip()
    if len(instructions) < 10:
        raise SkillImportError("The skill body below the frontmatter is shorter than the 10 "
                               "characters Astral requires for instructions.")
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise SkillImportError(f"The skill body is {len(instructions)} characters; Astral "
                               f"allows {MAX_INSTRUCTIONS_CHARS} and never truncates. Edit the "
                               "file and import again, or paste trimmed text into the "
                               "Add-a-skill form.")
    _refuse_control_characters(instructions, "instructions", multiline=True)
    return instructions


def _refuse_linked_resources(body):
    found = []
    for target in _LINK_RE.findall(body):
        if _SCHEME_RE.match(target) or target.startswith("#"):
            continue
        path = target.split("#", 1)[0].split("?", 1)[0]
        if path:
            found.append(path[:MAX_UNSUPPORTED_CHARS])
        if len(found) >= MAX_LISTED_RESOURCES:
            break
    if found:
        listed = ", ".join(f"'{path}'" for path in found)
        raise SkillImportError(f"The instructions link to bundled resources ({listed}); an "
                               "instructions-only import cannot include them, so this skill "
                               "is not imported. Bundle-dependent skills are unsupported.",
                               "skill_import_unsupported")


def _mapped_description(fields):
    value = fields.get("description")
    if value is None or type(value) is not str:
        return ""
    return value.strip()[:MAX_DESCRIPTION_CHARS]


def _mapped_license(fields):
    value = fields.get("license")
    if value is None:
        return "", True
    if type(value) is not str:
        return "", True
    license_value = value.strip()[:MAX_LICENSE_CHARS]
    return license_value, license_value.lower() not in _PERMISSIVE_LICENSES


def _metadata_reports(value):
    if type(value) is not dict:
        return ["metadata: unsupported value is not imported"]
    reports = [f"metadata.{key}: extra metadata is not imported" for key in list(value)[:8]]
    if len(value) > 8:
        reports.append(f"metadata: {len(value) - 8} more entries are not imported")
    return reports


def _bounded_reports(reports):
    bounded = [report[:MAX_UNSUPPORTED_CHARS] for report in reports]
    if len(bounded) > MAX_UNSUPPORTED:
        bounded = bounded[:MAX_UNSUPPORTED]
        bounded.append(f"{len(reports) - MAX_UNSUPPORTED} more unsupported fields are not "
                       "imported")
    return tuple(bounded)


def _unsupported_reports(fields, *, license_type_unsupported):
    reports = []
    if license_type_unsupported:
        reports.append("license: unsupported value type is not recorded")
    if "description" in fields:
        reports.append("description: previewed only; it is not saved with the skill")
    if "allowed-tools" in fields:
        reports.append("allowed-tools: tool grants are never imported; Astral permission "
                       "gates stay in force")
    if "compatibility" in fields:
        reports.append("compatibility: runtime requirements are not imported")
    if "metadata" in fields:
        reports.extend(_metadata_reports(fields["metadata"]))
    reports.extend(f"{key}: unsupported field is not imported"
                   for key in fields if key not in MAPPED_FIELDS)
    return _bounded_reports(reports)


def parse_skill_import(source):
    raw = _bounded_bytes(source)
    text = _decode(raw)
    frontmatter, body = _split_frontmatter(text)
    fields = _load_frontmatter(frontmatter)
    name = _mapped_name(fields)
    instructions = _mapped_instructions(body)
    _refuse_linked_resources(body)
    license_value, needs_confirmation = _mapped_license(fields)
    description = _mapped_description(fields)
    unsupported = _unsupported_reports(fields, license_type_unsupported=(
        fields.get("license") is not None and type(fields.get("license")) is not str))
    return SkillImportPreview(
        name=name, instructions=instructions, slug=legacy.slugify(name),
        description=description, license=license_value,
        license_requires_confirmation=needs_confirmation, unsupported=unsupported,
        source_sha256=hashlib.sha256(raw).hexdigest(), source_bytes=len(raw))


def audit_representation(preview):
    return {
        "source_sha256": preview.source_sha256,
        "source_bytes": preview.source_bytes,
        "claimed_name": preview.name[:MAX_NAME_CHARS],
        "derived_slug": preview.slug[:48],
        "license": (preview.license or "not stated")[:MAX_LICENSE_CHARS],
        "description_chars": len(preview.description),
        "instructions_chars": len(preview.instructions),
        "unsupported": list(preview.unsupported),
    }
