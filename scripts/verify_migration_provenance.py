#!/usr/bin/env python3
"""Verify feature-074 repository identity and extraction provenance.

The verifier is intentionally read-only.  It checks local Git metadata and
queries each canonical GitHub URL with redirects disabled.  A renamed or
wrong-cased URL that happens to redirect is therefore not accepted as proof of
repository identity.

Plane and Projection carry extraction manifests.  For those components the
tool additionally verifies the manifest schema and canonical digest, the
immutable AstralDeep source tree and every selected blob, the retained legacy
``master`` ref, ordinary ancestry, the pushed feature ref, and the extraction
commit trailers.  Later ordinary commits may advance a component: the trailer
proof may appear on any commit between the recorded baseline and the pinned
component commit.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

CONTRACT = "astral.migration-provenance-verification/v1"
MANIFEST_FORMAT = "astral.extraction-provenance/v1"
SCHEMA_ID = (
    "https://github.com/AstralDeep/AstralDeep/"
    "contracts/extraction-provenance.schema.json"
)
SOURCE_REPOSITORY = "https://github.com/AstralDeep/AstralDeep.git"
DEFAULT_SCHEMA = Path("contracts/extraction-provenance.schema.json")
DEFAULT_COMPOSITION = Path("config/astral-composition.json")

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SUBMODULE_SECTION = re.compile(r'^submodule "(?P<name>[^"\r\n]+)"$')
DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    """One component whose repository identity is part of the composition."""

    name: str
    path: str
    repository: str
    manifest_path: str | None = None


DEFAULT_COMPONENTS = (
    ComponentSpec(
        "astral-projection",
        "components/AstralProjection",
        "https://github.com/AstralDeep/AstralProjection.git",
        "provenance/extraction.json",
    ),
    ComponentSpec(
        "astral-plane",
        "components/AstralPlane",
        "https://github.com/AstralDeep/AstralPlane.git",
        "provenance/extraction.json",
    ),
    ComponentSpec(
        "astral-primitives",
        "components/AstralPrimitives",
        "https://github.com/AstralDeep/AstralPrimitives.git",
    ),
    ComponentSpec(
        "lets",
        "components/LETS",
        "https://github.com/AstralDeep/LETS.git",
    ),
)


@dataclass(frozen=True, slots=True)
class RemoteState:
    """Direct, non-redirected ``git ls-remote --symref`` result."""

    repository: str
    head: str
    refs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One stable, machine-readable verification failure."""

    code: str
    repository: str
    message: str
    remediation: str


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Deterministic feature-074 provenance verification result."""

    verified_repositories: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": CONTRACT,
            "ok": self.ok,
            "verifiedRepositories": list(self.verified_repositories),
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }


class VerificationError(RuntimeError):
    """A verification input could not be interpreted safely."""


class RemoteProbeError(VerificationError):
    """A canonical remote could not be inspected without ambiguity."""

    def __init__(self, message: str, *, redirect_only: bool = False) -> None:
        super().__init__(message)
        self.redirect_only = redirect_only


RemoteProbe = Callable[[str], RemoteState]


def _add(
    diagnostics: list[Diagnostic],
    code: str,
    repository: str,
    message: str,
    remediation: str,
) -> None:
    diagnostics.append(Diagnostic(code, repository, message, remediation))


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value {value!r} is forbidden")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _parse_json_bytes(payload: bytes, *, label: str) -> object:
    try:
        return json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise VerificationError(f"could not read strict JSON {label}: {exc}") from exc


def _read_json(path: Path) -> object:
    if path.is_symlink():
        raise VerificationError(f"JSON input must not be a symbolic link: {path}")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise VerificationError(f"could not read JSON {path}: {exc}") from exc
    return _parse_json_bytes(payload, label=os.fspath(path))


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise VerificationError(f"value is not canonical JSON: {exc}") from exc


def compute_manifest_sha256(document: Mapping[str, object]) -> str:
    """Return the canonical digest after removing only ``manifestSha256``."""

    projected = dict(document)
    projected.pop("manifestSha256", None)
    return hashlib.sha256(_canonical_json_bytes(projected)).hexdigest()


def _json_type_matches(instance: object, declared: str) -> bool:
    return {
        "null": instance is None,
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
        "boolean": isinstance(instance, bool),
    }.get(declared, False)


def _resolve_schema_ref(root: dict[str, object], reference: str) -> object:
    if not reference.startswith("#/"):
        raise VerificationError(f"non-local schema reference is forbidden: {reference}")
    current: object = root
    for encoded in reference[2:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise VerificationError(f"schema reference cannot be resolved: {reference}")
        current = current[token]
    return current


def _date_time_valid(value: str) -> bool:
    if DATE_TIME.fullmatch(value) is None:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _schema_errors(
    instance: object,
    schema: object,
    root: dict[str, object],
    *,
    path: str = "$",
) -> list[str]:
    """Validate the assertion vocabulary used by the extraction schema."""

    if not isinstance(schema, dict):
        raise VerificationError(f"schema node at {path} is not an object")
    errors: list[str] = []

    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise VerificationError(f"schema reference at {path} is not a string")
        errors.extend(
            _schema_errors(instance, _resolve_schema_ref(root, reference), root, path=path)
        )

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: value does not equal the required constant")
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise VerificationError(f"enum at {path} is not an array")
        if instance not in enum:
            errors.append(f"{path}: value is not in the permitted enumeration")

    declared = schema.get("type")
    if declared is not None:
        declared_types = [declared] if isinstance(declared, str) else declared
        if not (
            isinstance(declared_types, list)
            and declared_types
            and all(isinstance(item, str) for item in declared_types)
        ):
            raise VerificationError(f"type at {path} is invalid")
        if not any(_json_type_matches(instance, item) for item in declared_types):
            errors.append(f"{path}: value has the wrong JSON type")
            return errors

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise VerificationError(f"object schema at {path} is invalid")
        if any(not isinstance(item, str) for item in required):
            raise VerificationError(f"required at {path} contains a non-string")
        for key in required:
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            for key in sorted(set(instance) - set(properties)):
                errors.append(f"{path}: additional property {key!r} is forbidden")
        for key in sorted(set(instance) & set(properties)):
            errors.extend(
                _schema_errors(
                    instance[key], properties[key], root, path=f"{path}.{key}"
                )
            )

    if isinstance(instance, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(instance) < minimum:
            errors.append(f"{path}: array has fewer than minItems")
        if isinstance(maximum, int) and len(instance) > maximum:
            errors.append(f"{path}: array has more than maxItems")
        if schema.get("uniqueItems") is True:
            encoded = [_canonical_json_bytes(item) for item in instance]
            if len(encoded) != len(set(encoded)):
                errors.append(f"{path}: array items are not unique")
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(instance):
                errors.extend(
                    _schema_errors(item, items, root, path=f"{path}[{index}]")
                )

    if isinstance(instance, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(instance) < minimum:
            errors.append(f"{path}: string is shorter than minLength")
        if isinstance(maximum, int) and len(instance) > maximum:
            errors.append(f"{path}: string is longer than maxLength")
        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise VerificationError(f"pattern at {path} is not a string")
            try:
                matches = re.search(pattern, instance) is not None
            except re.error as exc:
                raise VerificationError(f"invalid schema pattern at {path}: {exc}") from exc
            if not matches:
                errors.append(f"{path}: string does not match the required pattern")
        if schema.get("format") == "date-time" and not _date_time_valid(instance):
            errors.append(f"{path}: string is not an RFC 3339 date-time")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            errors.append(f"{path}: number is below minimum")
        if isinstance(maximum, (int, float)) and instance > maximum:
            errors.append(f"{path}: number is above maximum")
    return errors


def validate_manifest_schema(
    manifest: object, schema: object
) -> tuple[str, ...]:
    """Return deterministic errors for the committed extraction schema."""

    if not isinstance(schema, dict):
        raise VerificationError("extraction schema root must be an object")
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise VerificationError("extraction schema must declare Draft 2020-12")
    if schema.get("$id") != SCHEMA_ID:
        raise VerificationError("extraction schema has a non-canonical $id")
    return tuple(sorted(set(_schema_errors(manifest, schema, schema))))


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _run_git(
    repo: Path | None,
    arguments: Sequence[str],
    *,
    timeout: int = 30,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = ["git"]
    if repo is not None:
        command.extend(("-C", os.fspath(repo)))
    command.extend(arguments)
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            timeout=timeout,
            env=_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationError(f"could not execute local Git safely: {exc}") from exc


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    completed = _run_git(repo, arguments)
    if completed.returncode != 0:
        raise VerificationError(
            f"Git metadata unavailable for {' '.join(arguments[:2])}"
        )
    return completed.stdout


def _git_text(repo: Path, *arguments: str) -> str:
    try:
        return _git_bytes(repo, *arguments).decode("utf-8", "strict").strip()
    except UnicodeError as exc:
        raise VerificationError("Git metadata is not valid UTF-8") from exc


def _exact_git_root(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise VerificationError(f"repository does not resolve: {path}: {exc}") from exc
    root = Path(_git_text(resolved, "rev-parse", "--show-toplevel"))
    try:
        git_root = root.resolve(strict=True)
    except OSError as exc:
        raise VerificationError(f"Git root does not resolve: {root}: {exc}") from exc
    if git_root != resolved:
        raise VerificationError(
            f"exact-root mismatch: requested {resolved}, Git reports {git_root}"
        )
    return resolved


def _origin_urls(repo: Path, *, push: bool) -> tuple[str, ...]:
    arguments = ["remote", "get-url", "--all"]
    if push:
        arguments.append("--push")
    arguments.append("origin")
    output = _git_text(repo, *arguments)
    return tuple(line for line in output.splitlines() if line)


def _check_origin(
    repo: Path,
    expected: str,
    name: str,
    diagnostics: list[Diagnostic],
) -> None:
    try:
        fetch = _origin_urls(repo, push=False)
        push = _origin_urls(repo, push=True)
    except VerificationError as exc:
        _add(
            diagnostics,
            "E_ORIGIN_UNAVAILABLE",
            name,
            str(exc),
            "configure one exact canonical origin fetch and push URL",
        )
        return
    if fetch != (expected,) or push != (expected,):
        _add(
            diagnostics,
            "E_CANONICAL_URL",
            name,
            f"origin URLs are fetch={fetch!r}, push={push!r}; expected {expected!r}",
            "set both origin directions to the exact case-sensitive HTTPS URL",
        )


def _parse_gitmodules(path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.RawConfigParser(strict=True, interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise VerificationError(f"could not read .gitmodules: {exc}") from exc
    modules: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        match = SUBMODULE_SECTION.fullmatch(section)
        if match is None:
            raise VerificationError(f"invalid .gitmodules section {section!r}")
        values = {key.lower(): value.strip() for key, value in parser.items(section)}
        module_path = values.get("path")
        if not module_path or module_path in modules:
            raise VerificationError(f"invalid or duplicate submodule path {module_path!r}")
        values["name"] = match.group("name")
        modules[module_path] = values
    return modules


def _gitlink(repo: Path, relative_path: str) -> tuple[str, str] | None:
    output = _git_text(repo, "ls-files", "--stage", "--", relative_path)
    if not output:
        return None
    lines = output.splitlines()
    if len(lines) != 1 or "\t" not in lines[0]:
        raise VerificationError(f"ambiguous index entry for {relative_path}")
    metadata, indexed_path = lines[0].split("\t", 1)
    fields = metadata.split()
    if (
        len(fields) != 3
        or indexed_path.replace("\\", "/") != relative_path
        or fields[2] != "0"
    ):
        raise VerificationError(f"malformed or unmerged index entry for {relative_path}")
    return fields[0], fields[1]


def _parse_ls_remote(repository: str, payload: bytes) -> RemoteState:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise RemoteProbeError("remote advertisement is not UTF-8") from exc
    head: str | None = None
    refs: dict[str, str] = {}
    for line in text.splitlines():
        if "\t" not in line:
            raise RemoteProbeError("remote advertisement contains a malformed line")
        value, reference = line.split("\t", 1)
        if value.startswith("ref: "):
            if reference != "HEAD" or head is not None:
                raise RemoteProbeError("remote advertisement has an invalid symref")
            head = value.removeprefix("ref: ")
            continue
        if SHA1.fullmatch(value) is None or not reference:
            raise RemoteProbeError("remote advertisement contains an invalid object ID")
        previous = refs.setdefault(reference, value)
        if previous != value:
            raise RemoteProbeError(f"remote ref {reference!r} is ambiguous")
    if head is None:
        raise RemoteProbeError("remote did not advertise a symbolic HEAD")
    return RemoteState(repository=repository, head=head, refs=refs)


def probe_direct_remote(repository: str) -> RemoteState:
    """Query one canonical URL, proving it works with redirects disabled."""

    direct = _run_git(
        None,
        (
            "-c",
            "http.followRedirects=false",
            "ls-remote",
            "--symref",
            repository,
        ),
        timeout=45,
    )
    if direct.returncode == 0:
        return _parse_ls_remote(repository, direct.stdout)

    redirected = _run_git(
        None,
        (
            "-c",
            "http.followRedirects=true",
            "ls-remote",
            "--symref",
            repository,
        ),
        timeout=45,
    )
    if redirected.returncode == 0:
        raise RemoteProbeError(
            "repository is reachable only when HTTP redirects are enabled",
            redirect_only=True,
        )
    raise RemoteProbeError(
        "canonical repository could not be queried non-interactively"
    )


def _archive_ref(reference: str) -> bool:
    if not reference.startswith(("refs/heads/", "refs/tags/")):
        return False
    short = reference.split("/", 2)[2].casefold()
    tokens = {token for token in re.split(r"[\W_.-]+", short) if token}
    return (
        short.startswith(("archive/", "archives/"))
        or "archive" in tokens
        or "archives" in tokens
        or (
            bool(tokens & {"backup", "snapshot"})
            and bool(tokens & {"074", "legacy", "migration", "pre"})
        )
        or "pre-migration" in short
    )


def _local_refs(repo: Path) -> dict[str, str]:
    output = _git_text(
        repo,
        "for-each-ref",
        "--format=%(objectname) %(refname)",
        "refs/heads",
        "refs/tags",
    )
    refs: dict[str, str] = {}
    for line in output.splitlines():
        object_id, separator, reference = line.partition(" ")
        if not separator or SHA1.fullmatch(object_id) is None:
            raise VerificationError("local ref inventory is malformed")
        refs[reference] = object_id
    return refs


def _check_archive_refs(
    name: str,
    local: Mapping[str, str],
    remote: Mapping[str, str] | None,
    diagnostics: list[Diagnostic],
) -> None:
    prohibited = sorted(
        {reference for reference in local if _archive_ref(reference)}
        | ({reference for reference in remote if _archive_ref(reference)} if remote else set())
    )
    if prohibited:
        _add(
            diagnostics,
            "E_ARCHIVE_REF",
            name,
            f"feature-074 archive/backup refs are prohibited: {prohibited!r}",
            "remove no ref automatically; obtain owner direction and record the conflict",
        )


def _source_tree_inventory(
    source_repo: Path, commit: str
) -> dict[str, tuple[str, str, str]]:
    raw = _git_bytes(source_repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries: dict[str, tuple[str, str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        if b"\t" not in record:
            raise VerificationError("source tree inventory contains malformed metadata")
        metadata, raw_path = record.split(b"\t", 1)
        try:
            mode, kind, object_id = metadata.decode("ascii", "strict").split(" ")
            path = raw_path.decode("utf-8", "strict")
        except (UnicodeError, ValueError) as exc:
            raise VerificationError("source tree inventory is not canonical UTF-8") from exc
        if (
            path in entries
            or kind != "blob"
            or SHA1.fullmatch(object_id) is None
        ):
            raise VerificationError(f"source tree has an invalid blob entry: {path!r}")
        entries[path] = (mode, kind, object_id)
    return entries


def _source_blob_sizes(
    source_repo: Path, object_ids: Sequence[str]
) -> dict[str, int | None]:
    unique = tuple(dict.fromkeys(object_ids))
    payload = "".join(f"{object_id}\n" for object_id in unique).encode("ascii")
    result = _run_git(
        source_repo,
        ("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"),
        input_bytes=payload,
    )
    if result.returncode != 0:
        raise VerificationError("source blob batch verification failed")
    try:
        lines = result.stdout.decode("ascii", "strict").splitlines()
    except UnicodeError as exc:
        raise VerificationError("source blob metadata is not ASCII") from exc
    if len(lines) != len(unique):
        raise VerificationError("source blob batch result count is ambiguous")
    sizes: dict[str, int | None] = {}
    for expected, line in zip(unique, lines, strict=True):
        fields = line.split(" ")
        if fields == [expected, "missing"]:
            sizes[expected] = None
            continue
        if (
            len(fields) != 3
            or fields[0] != expected
            or fields[1] != "blob"
            or not fields[2].isdigit()
        ):
            raise VerificationError(
                f"source blob batch result is malformed for {expected}"
            )
        sizes[expected] = int(fields[2])
    return sizes


def _matching_trailer_commit(
    repo: Path,
    baseline: str,
    target: str,
    source_commit: str,
    manifest_digest: str,
) -> str | None:
    revision_output = _git_text(repo, "rev-list", "--reverse", f"{baseline}..{target}")
    expected = {
        "Source-Repository": SOURCE_REPOSITORY,
        "Source-Commit": source_commit,
        "Source-Manifest-SHA256": manifest_digest,
    }
    for commit in revision_output.splitlines():
        body = _git_text(repo, "show", "-s", "--format=%B", commit)
        found: dict[str, list[str]] = {key: [] for key in expected}
        for line in body.splitlines():
            for key in expected:
                prefix = f"{key}:"
                if line.startswith(prefix):
                    found[key].append(line.removeprefix(prefix).strip())
        if all(found[key] == [value] for key, value in expected.items()):
            return commit
    return None


def _verify_manifest(
    *,
    deep_repo: Path,
    component_repo: Path,
    spec: ComponentSpec,
    target_commit: str,
    manifest: object,
    schema: object,
    remote: RemoteState | None,
    diagnostics: list[Diagnostic],
) -> None:
    schema_errors = validate_manifest_schema(manifest, schema)
    if schema_errors:
        _add(
            diagnostics,
            "E_MANIFEST_SCHEMA",
            spec.name,
            "; ".join(schema_errors[:8]),
            "restore a schema-valid extraction provenance manifest",
        )
        return
    assert isinstance(manifest, dict)

    recorded_digest = manifest.get("manifestSha256")
    computed_digest = compute_manifest_sha256(manifest)
    if recorded_digest != computed_digest:
        _add(
            diagnostics,
            "E_MANIFEST_DIGEST",
            spec.name,
            f"recorded manifest digest {recorded_digest!r} != {computed_digest}",
            "rebuild the manifest from the immutable source; do not bless tampered bytes",
        )
        return

    source = manifest["source"]
    destination = manifest["destination"]
    entries = manifest["entries"]
    assert isinstance(source, dict)
    assert isinstance(destination, dict)
    assert isinstance(entries, list)
    source_commit = source["commit"]
    baseline = destination["legacyBaseline"]
    assert isinstance(source_commit, str)
    assert isinstance(baseline, dict)
    baseline_commit = baseline["commit"]
    branch = destination["branch"]
    assert isinstance(baseline_commit, str)
    assert isinstance(branch, str)

    if source["repository"] != SOURCE_REPOSITORY:
        _add(
            diagnostics,
            "E_CANONICAL_URL",
            spec.name,
            "manifest source repository is not canonical AstralDeep",
            "restore the exact case-sensitive source repository URL",
        )
    if destination["repository"] != spec.repository:
        _add(
            diagnostics,
            "E_CANONICAL_URL",
            spec.name,
            "manifest destination repository does not match the component identity",
            "restore the exact case-sensitive destination repository URL",
        )

    try:
        source_kind = _git_text(deep_repo, "cat-file", "-t", source_commit)
        source_tree = _git_text(deep_repo, "show", "-s", "--format=%T", source_commit)
    except VerificationError as exc:
        _add(
            diagnostics,
            "E_SOURCE_COMMIT_MISSING",
            spec.name,
            str(exc),
            "fetch the immutable AstralDeep source history without pruning or rewriting it",
        )
        return
    if source_kind != "commit" or source_tree != source["tree"]:
        _add(
            diagnostics,
            "E_SOURCE_TREE_MISMATCH",
            spec.name,
            "manifest source commit/tree does not match local immutable Git metadata",
            "restore the recorded source revision and tree; do not rewrite provenance",
        )
        return

    try:
        source_inventory = _source_tree_inventory(deep_repo, source_commit)
        source_sizes = _source_blob_sizes(
            deep_repo,
            tuple(
                str(raw_entry["blob"])
                for raw_entry in entries
                if isinstance(raw_entry, dict)
            ),
        )
    except VerificationError as exc:
        _add(
            diagnostics,
            "E_SOURCE_INVENTORY",
            spec.name,
            str(exc),
            "restore the immutable source tree and its reachable blob objects",
        )
        return

    for index, raw_entry in enumerate(entries):
        assert isinstance(raw_entry, dict)
        source_path = raw_entry["sourcePath"]
        source_blob = raw_entry["blob"]
        assert isinstance(source_path, str)
        assert isinstance(source_blob, str)
        metadata = source_inventory.get(source_path)
        if metadata is None:
            _add(
                diagnostics,
                "E_SOURCE_PATH_MISSING",
                spec.name,
                f"entry {index} path is absent from the immutable tree: {source_path}",
                "regenerate provenance from the exact immutable source commit",
            )
            continue
        source_size = source_sizes.get(source_blob)
        if source_size is None:
            _add(
                diagnostics,
                "E_SOURCE_BLOB_MISSING",
                spec.name,
                f"source blob object is missing: {source_blob}",
                "restore the immutable source object before accepting the migration",
            )
            continue
        observed = (*metadata, source_size)
        expected = (
            raw_entry["mode"],
            "blob",
            raw_entry["blob"],
            raw_entry["bytes"],
        )
        if observed != expected:
            _add(
                diagnostics,
                "E_SOURCE_BLOB_MISMATCH",
                spec.name,
                f"entry {index} immutable tuple {observed!r} != {expected!r}",
                "regenerate provenance from the exact immutable source tree",
            )

    ancestry = _run_git(
        component_repo,
        ("merge-base", "--is-ancestor", baseline_commit, target_commit),
    )
    if ancestry.returncode == 1:
        _add(
            diagnostics,
            "E_NOT_DESCENDANT",
            spec.name,
            f"pinned commit {target_commit} is not a descendant of {baseline_commit}",
            "use an ordinary descendant commit; never orphan, reset, rebase, or force",
        )
    elif ancestry.returncode != 0:
        _add(
            diagnostics,
            "E_ANCESTRY_UNAVAILABLE",
            spec.name,
            "ordinary ancestry could not be proven from local Git objects",
            "restore full legacy and feature history before verification",
        )

    if remote is not None:
        master = remote.refs.get("refs/heads/master")
        if master is None:
            _add(
                diagnostics,
                "E_MASTER_MISSING",
                spec.name,
                "remote legacy master is absent",
                "stop: feature 074 requires retained master and does not authorize recreation",
            )
        elif master != baseline_commit:
            _add(
                diagnostics,
                "E_MASTER_CHANGED",
                spec.name,
                f"remote master {master} != recorded baseline {baseline_commit}",
                "stop and perform a fresh remote audit; never force the legacy ref",
            )
        feature_ref = f"refs/heads/{branch}"
        feature_commit = remote.refs.get(feature_ref)
        if feature_commit is None:
            _add(
                diagnostics,
                "E_REMOTE_FEATURE_MISSING",
                spec.name,
                f"remote feature ref {feature_ref} is absent",
                "push only the named ordinary feature branch, without force",
            )
        elif feature_commit != target_commit:
            _add(
                diagnostics,
                "E_REMOTE_FEATURE_DIVERGED",
                spec.name,
                f"remote feature {feature_commit} != pinned commit {target_commit}",
                "stop and reconcile with ordinary commits; never force the feature ref",
            )

    try:
        trailer_commit = _matching_trailer_commit(
            component_repo,
            baseline_commit,
            target_commit,
            source_commit,
            str(recorded_digest),
        )
    except VerificationError:
        trailer_commit = None
    if trailer_commit is None:
        _add(
            diagnostics,
            "E_PROVENANCE_TRAILERS",
            spec.name,
            "no ordinary descendant commit carries the exact extraction trailers",
            "retain the original extraction commit with all three exact provenance trailers",
        )


def verify_migration_provenance(
    deep_repository: str | Path,
    *,
    schema_path: str | Path | None = None,
    composition_path: str | Path | None = None,
    components: Sequence[ComponentSpec] = DEFAULT_COMPONENTS,
    remote_probe: RemoteProbe | None = None,
) -> VerificationReport:
    """Verify the local composition and live, non-redirected remote identities."""

    diagnostics: list[Diagnostic] = []
    probe = remote_probe or probe_direct_remote
    try:
        deep_repo = _exact_git_root(Path(deep_repository))
    except VerificationError as exc:
        return VerificationReport(
            (),
            (
                Diagnostic(
                    "E_DEEP_REPOSITORY",
                    "astraldeep",
                    str(exc),
                    "provide the exact AstralDeep Git worktree root",
                ),
            ),
        )

    schema_file = Path(schema_path) if schema_path else deep_repo / DEFAULT_SCHEMA
    composition_file = (
        Path(composition_path)
        if composition_path
        else deep_repo / DEFAULT_COMPOSITION
    )
    try:
        schema = _read_json(schema_file)
        composition = _read_json(composition_file)
        gitmodules = _parse_gitmodules(deep_repo / ".gitmodules")
    except VerificationError as exc:
        return VerificationReport(
            (),
            (
                Diagnostic(
                    "E_INPUT",
                    "astraldeep",
                    str(exc),
                    "restore the committed schema, composition, and .gitmodules inputs",
                ),
            ),
        )
    if not isinstance(composition, dict) or not isinstance(
        composition.get("components"), dict
    ):
        return VerificationReport(
            (),
            (
                Diagnostic(
                    "E_COMPOSITION",
                    "astraldeep",
                    "composition components are missing or malformed",
                    "restore config/astral-composition.json",
                ),
            ),
        )
    composition_components = composition["components"]
    assert isinstance(composition_components, dict)

    _check_origin(deep_repo, SOURCE_REPOSITORY, "astraldeep", diagnostics)
    remotes: dict[str, RemoteState | None] = {}
    repositories = (("astraldeep", SOURCE_REPOSITORY),) + tuple(
        (spec.name, spec.repository) for spec in components
    )
    for name, repository in repositories:
        try:
            state = probe(repository)
        except RemoteProbeError as exc:
            state = None
            code = "E_REDIRECT_DEPENDENCE" if exc.redirect_only else "E_REMOTE_UNAVAILABLE"
            _add(
                diagnostics,
                code,
                name,
                str(exc),
                "verify the exact canonical URL directly with non-interactive credentials",
            )
        except Exception as exc:  # defensive boundary for injected probes
            state = None
            _add(
                diagnostics,
                "E_REMOTE_UNAVAILABLE",
                name,
                f"remote probe failed: {type(exc).__name__}",
                "repair the read-only remote probe and retry",
            )
        remotes[name] = state
        if state is not None:
            if state.repository != repository:
                _add(
                    diagnostics,
                    "E_CANONICAL_URL",
                    name,
                    f"remote probe identity {state.repository!r} != {repository!r}",
                    "query only the exact canonical URL",
                )
            if state.head != "refs/heads/main":
                _add(
                    diagnostics,
                    "E_DEFAULT_BRANCH",
                    name,
                    f"remote HEAD targets {state.head!r}, not refs/heads/main",
                    "stop and reconcile the owner-selected main default branch",
                )
            if "refs/heads/main" not in state.refs:
                _add(
                    diagnostics,
                    "E_MAIN_MISSING",
                    name,
                    "remote main ref is absent",
                    "stop and audit remote branch state without creating replacement refs",
                )

    try:
        _check_archive_refs(
            "astraldeep",
            _local_refs(deep_repo),
            remotes["astraldeep"].refs if remotes["astraldeep"] else None,
            diagnostics,
        )
    except VerificationError as exc:
        _add(
            diagnostics,
            "E_REF_INVENTORY",
            "astraldeep",
            str(exc),
            "restore readable local ref metadata",
        )

    verified: list[str] = ["astraldeep"]
    for spec in components:
        configured = composition_components.get(spec.name)
        module = gitmodules.get(spec.path)
        if not isinstance(configured, dict):
            _add(
                diagnostics,
                "E_COMPONENT_CONFIG",
                spec.name,
                "component is absent from the composition manifest",
                "restore the exact component composition entry",
            )
            continue
        if configured.get("repository") != spec.repository:
            _add(
                diagnostics,
                "E_CANONICAL_URL",
                spec.name,
                "composition repository URL is not the exact canonical identity",
                "restore exact case-sensitive HTTPS component metadata",
            )
        if configured.get("path") != spec.path:
            _add(
                diagnostics,
                "E_COMPONENT_PATH",
                spec.name,
                "composition path does not match the governed component path",
                "restore the exact component path",
            )
        if module is None:
            _add(
                diagnostics,
                "E_GITMODULE_MISSING",
                spec.name,
                "component is absent from .gitmodules",
                "restore the exact submodule entry",
            )
        else:
            if module.get("url") != spec.repository:
                _add(
                    diagnostics,
                    "E_CANONICAL_URL",
                    spec.name,
                    ".gitmodules URL is not the exact canonical identity",
                    "restore the exact case-sensitive HTTPS URL",
                )
            if "branch" in module:
                _add(
                    diagnostics,
                    "E_FLOATING_BRANCH",
                    spec.name,
                    ".gitmodules must not contain a floating branch option",
                    "remove branch tracking and retain an exact Gitlink",
                )

        component_path = deep_repo / spec.path
        try:
            component_repo = _exact_git_root(component_path)
        except VerificationError as exc:
            _add(
                diagnostics,
                "E_COMPONENT_REPOSITORY",
                spec.name,
                str(exc),
                "initialize the exact pinned component worktree",
            )
            continue
        _check_origin(component_repo, spec.repository, spec.name, diagnostics)
        try:
            local_refs = _local_refs(component_repo)
            remote_refs = remotes[spec.name].refs if remotes[spec.name] else None
            _check_archive_refs(spec.name, local_refs, remote_refs, diagnostics)
        except VerificationError as exc:
            _add(
                diagnostics,
                "E_REF_INVENTORY",
                spec.name,
                str(exc),
                "restore readable local ref metadata",
            )

        try:
            indexed = _gitlink(deep_repo, spec.path)
        except VerificationError as exc:
            _add(
                diagnostics,
                "E_GITLINK",
                spec.name,
                str(exc),
                "restore one stage-0 mode-160000 Gitlink",
            )
            continue
        configured_commit = configured.get("commit")
        if (
            indexed is None
            or indexed[0] != "160000"
            or indexed[1] != configured_commit
            or not isinstance(configured_commit, str)
            or SHA1.fullmatch(configured_commit) is None
        ):
            _add(
                diagnostics,
                "E_GITLINK",
                spec.name,
                f"index {indexed!r} does not equal configured commit {configured_commit!r}",
                "pin the exact component commit in both index and composition",
            )
            continue
        try:
            component_head = _git_text(component_repo, "rev-parse", "HEAD")
        except VerificationError as exc:
            _add(
                diagnostics,
                "E_COMPONENT_HEAD",
                spec.name,
                str(exc),
                "check out the exact Gitlink commit",
            )
            continue
        if component_head != configured_commit:
            _add(
                diagnostics,
                "E_COMPONENT_HEAD",
                spec.name,
                f"component HEAD {component_head} != Gitlink {configured_commit}",
                "check out the exact pinned component commit",
            )

        if spec.name == "lets":
            ref = configured.get("ref")
            state = remotes[spec.name]
            if isinstance(ref, str) and state is not None:
                tag = state.refs.get(f"refs/tags/{ref}^{{}}") or state.refs.get(
                    f"refs/tags/{ref}"
                )
                if tag != configured_commit:
                    _add(
                        diagnostics,
                        "E_SIGNED_REF_PIN",
                        spec.name,
                        f"remote tag {ref!r} does not peel to the configured commit",
                        "restore the exact signed LETS release pin",
                    )

        if spec.manifest_path:
            manifest_file = component_repo / spec.manifest_path
            try:
                if manifest_file.is_symlink():
                    raise VerificationError(
                        f"manifest must not be a symbolic link: {manifest_file}"
                    )
                tracked_manifest = _git_bytes(
                    component_repo,
                    "show",
                    f"{configured_commit}:{spec.manifest_path}",
                )
                try:
                    worktree_manifest = manifest_file.read_bytes()
                except OSError as exc:
                    raise VerificationError(
                        f"could not read manifest worktree bytes: {exc}"
                    ) from exc
                if worktree_manifest != tracked_manifest:
                    _add(
                        diagnostics,
                        "E_MANIFEST_WORKTREE",
                        spec.name,
                        "worktree manifest bytes differ from the exact Gitlink commit",
                        "restore or commit reviewed manifest bytes before verification",
                    )
                manifest = _parse_json_bytes(
                    tracked_manifest,
                    label=f"{configured_commit}:{spec.manifest_path}",
                )
                _verify_manifest(
                    deep_repo=deep_repo,
                    component_repo=component_repo,
                    spec=spec,
                    target_commit=configured_commit,
                    manifest=manifest,
                    schema=schema,
                    remote=remotes[spec.name],
                    diagnostics=diagnostics,
                )
            except VerificationError as exc:
                _add(
                    diagnostics,
                    "E_MANIFEST_READ",
                    spec.name,
                    str(exc),
                    "restore the tracked strict-JSON extraction manifest",
                )
        verified.append(spec.name)

    return VerificationReport(
        tuple(verified),
        tuple(
            sorted(
                diagnostics,
                key=lambda item: (item.repository, item.code, item.message),
            )
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deep-repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="exact AstralDeep Git worktree root",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        help="extraction provenance schema (defaults inside --deep-repo)",
    )
    parser.add_argument(
        "--composition",
        type=Path,
        help="composition manifest (defaults inside --deep-repo)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional report path; stdout is always emitted",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    report = verify_migration_provenance(
        arguments.deep_repo,
        schema_path=arguments.schema,
        composition_path=arguments.composition,
    )
    payload = json.dumps(report.to_dict(), sort_keys=True, indent=2) + "\n"
    sys.stdout.write(payload)
    if arguments.output:
        try:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(payload, encoding="utf-8", newline="")
        except OSError as exc:
            sys.stderr.write(f"could not write report: {exc}\n")
            return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
