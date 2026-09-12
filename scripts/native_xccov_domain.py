#!/usr/bin/env python3
"""Bind native iOS coverage extents to the tested compiler mapping.

LLVM 21 JSON export 3.0.1 supplies function region geometry from the binary.
Only that geometry is used: empty-profile counters are never observations.
The original xccov line arrays remain the sole source of execution counts.
This does not prove that a raw profile came from the retained executable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import struct
import stat
from typing import Any


DOMAIN_FORMAT = "astral.xccov-domain/v1"
REPORT_FORMAT = "astral.xccov-native/v1"
LLVM_FORMAT = "3.0.1"
XCODE_BUILD = "17F113"
LLVM_VERSION = "Apple LLVM version 21.0.0"
MAX_FILES = 10_000
MAX_REGIONS = 1_000_000
MAX_LINES = 1_000_000
MAX_TEXT = 16_384
MAX_MAPPING_BYTES = 64 * 1024 * 1024
SOURCE_PREFIXES = ("", "components/AstralProjection/")
CORE_ROOT = "apple-clients/AstralCore/Sources/"
APP_ROOT = "apple-clients/AstralApp/AstralApp/"
LANES = ("core", "unit", "ui", "staging")
BINARY_MEMBERS = {
    "core": "Products/Debug-iphonesimulator/AstralCoreTests.xctest/AstralCoreTests",
    "app": "Products/Debug-iphonesimulator/AstralDeep.app/AstralDeep.debug.dylib",
}


class DomainError(ValueError):
    """Data-free native mapping refusal shared by producer and protected policy."""


def require(condition: Any) -> None:
    """Reject an invalid domain without returning source or tool output."""
    if not condition:
        raise DomainError("native_xccov_domain_invalid")


def canonical(value: Any) -> bytes:
    """Serialize deterministic data for geometry and semantic identities."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(value: bytes) -> str:
    """Hash exact bytes without interpreting them as executable input."""
    return hashlib.sha256(value).hexdigest()


def strict_json(raw: bytes) -> Any:
    """Decode bounded UTF-8 JSON with unique keys and finite numbers."""
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_MAPPING_BYTES)

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    def finite(value: str) -> float:
        result = float(value)
        require(math.isfinite(result))
        return result

    try:
        return json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=unique,
            parse_constant=lambda _value: require(False),
            parse_float=finite,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise DomainError("native_xccov_domain_invalid") from exc


def _integer(value: Any, *, maximum: int = MAX_LINES, minimum: int = 0) -> int:
    require(type(value) is int and minimum <= value <= maximum)
    return value


def _text(value: Any) -> str:
    require(isinstance(value, str))
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeError as exc:
        raise DomainError("native_xccov_domain_invalid") from exc
    require(
        0 < size <= MAX_TEXT
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )
    return value


def _digest(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value))
    return value


def source_path(path: Any, prefix: str) -> str:
    """Accept only one declared root spelling for maintained iOS Swift inputs."""
    require(prefix in SOURCE_PREFIXES)
    path = _text(path)
    require(path.startswith(prefix) and "\\" not in path)
    pure = PurePosixPath(path)
    require(
        not pure.is_absolute() and pure.as_posix() == path and ".." not in pure.parts
    )
    local = path.removeprefix(prefix)
    require(local.endswith(".swift") and local.startswith((CORE_ROOT, APP_ROOT)))
    return path


def source_facts(raw: bytes) -> dict[str, Any]:
    """Record exact source bytes and physical length, never lexical execution guesses."""
    require(isinstance(raw, bytes) and 0 < len(raw) <= 16 * 1024 * 1024)
    lines = raw.count(b"\n") + int(not raw.endswith(b"\n"))
    _integer(lines, minimum=1)
    return {"source_sha256": sha256(raw), "physical_lines": lines}


def ios_macho(raw: bytes, lane: str) -> None:
    """Require the qualified thin arm64 iOS-simulator image and its load commands.

    LC_BUILD_VERSION platform 7 is iOS Simulator. An arm64 device or macOS
    image is not interchangeable, and fat images need separate qualification.
    """
    require(lane in LANES and isinstance(raw, bytes) and len(raw) >= 32)
    magic, cpu, subtype, kind, commands, command_bytes, _flags, reserved = (
        struct.unpack_from("<8I", raw)
    )
    require(
        magic == 0xFEEDFACF
        and cpu == 0x0100000C
        and subtype == 0
        and kind == (8 if lane == "core" else 6)
        and reserved == 0
        and 0 < commands <= 4096
        and commands * 8 <= command_bytes <= len(raw) - 32
    )
    offset = 32
    versions = []
    for _ in range(commands):
        require(offset + 8 <= 32 + command_bytes)
        command, size = struct.unpack_from("<II", raw, offset)
        require(size >= 8 and size % 8 == 0 and offset + size <= 32 + command_bytes)
        if command == 0x32:  # LC_BUILD_VERSION
            require(size >= 24)
            platform, minimum, sdk, tools = struct.unpack_from("<4I", raw, offset + 8)
            require(
                size == 24 + tools * 8 and platform == 7 and minimum > 0 and sdk > 0
            )
            versions.append(platform)
        require(
            command not in {0x24, 0x25, 0x2F, 0x30}
        )  # older ambiguous platform commands
        offset += size
    require(offset == 32 + command_bytes and versions == [7])


def mapping_geometry(
    raw: bytes,
    *,
    archive_root: str,
    prefix: str,
    tracked: set[str],
    lane: str,
) -> dict[str, dict[str, Any]]:
    """Extract complete per-source function regions from fixed-version LLVM JSON.

    Segments depend on coalesced counter values, and summary line totals count
    nested functions twice. Neither is used to infer the source domain here.
    """
    require(lane in LANES and prefix in SOURCE_PREFIXES)
    archive_root = _text(archive_root)
    require(
        archive_root.startswith("/")
        and not archive_root.endswith("/")
        and PurePosixPath(archive_root).as_posix() == archive_root
        and ".." not in PurePosixPath(archive_root).parts
        and "\\" not in archive_root
    )
    require(0 < len(tracked) <= MAX_FILES)
    for path in tracked:
        source_path(path, prefix)
    document = strict_json(raw)
    require(
        isinstance(document, dict)
        and set(document) == {"data", "type", "version"}
        and document["type"] == "llvm.coverage.json.export"
        and document["version"] == LLVM_FORMAT
        and isinstance(document["data"], list)
        and len(document["data"]) == 1
    )
    data = document["data"][0]
    require(isinstance(data, dict) and set(data) == {"files", "functions", "totals"})
    functions, files = data["functions"], data["files"]
    require(isinstance(functions, list) and 0 < len(functions) <= MAX_REGIONS)
    require(isinstance(files, list) and 0 < len(files) <= MAX_FILES)
    absolute = {archive_root + "/" + path: path for path in tracked}
    suffixes: dict[str, Any] = {}
    for path in tracked:
        node = suffixes
        for part in reversed(path.removeprefix(prefix).split("/")):
            node = node.setdefault(part, {})
        node[""] = True
    selected_cache: dict[str, str | None] = {}

    def selected(value: Any) -> str | None:
        name = _text(value)
        if name in selected_cache:
            return selected_cache[name]
        if name in absolute:
            selected_cache[name] = absolute[name]
            return absolute[name]
        # An alternate checkout/root spelling must not silently become an
        # ignored external file when it names a maintained source suffix.
        node = suffixes
        for part in reversed(name.split("/")):
            if part not in node:
                break
            node = node[part]
            require("" not in node)
        selected_cache[name] = None
        return None

    listed: set[str] = set()
    all_names: set[str] = set()
    for entry in files:
        require(isinstance(entry, dict) and isinstance(entry.get("segments"), list))
        name = _text(entry.get("filename"))
        require(name not in all_names)
        all_names.add(name)
        path = selected(name)
        if path is not None:
            listed.add(path)
    require(listed)
    regions: dict[str, list[Any]] = {path: [] for path in listed}
    seen_functions: set[str] = set()
    total_regions = 0
    for function in functions:
        require(
            isinstance(function, dict)
            and set(function)
            == {"branches", "count", "filenames", "mcdc_records", "name", "regions"}
        )
        name = _text(function["name"])
        require(name not in seen_functions)
        seen_functions.add(name)
        require(function["count"] == 0 and type(function["count"]) is int)
        names, values = function["filenames"], function["regions"]
        require(isinstance(names, list) and 0 < len(names) <= MAX_FILES)
        require(isinstance(values, list) and values)
        total_regions += len(values)
        require(total_regions <= MAX_REGIONS)
        require(all(isinstance(value, str) and value in all_names for value in names))
        source_names = [selected(value) for value in names]
        require(len(set(names)) == len(names))
        for region in values:
            require(isinstance(region, list) and len(region) == 8)
            for value in region:
                _integer(value, maximum=(1 << 63) - 1)
            line, column, end_line, end_column, count, file_id, expansion, kind = region
            require(
                count == 0
                and file_id < len(names)
                and expansion < len(names)
                and 1 <= line <= end_line <= MAX_LINES
                and 1 <= column <= MAX_LINES
                and 1 <= end_column <= MAX_LINES
                and (line < end_line or column <= end_column)
            )
            path = source_names[file_id]
            if path is None:
                continue
            require(path in regions and kind in {0, 1, 2, 3})
            # Expansion identities, when present, are source-root normalized;
            # no foreign header/path can alias a maintained Swift expansion.
            expansion_path = None
            if kind == 1:
                expansion_path = source_names[expansion]
                require(expansion_path is not None)
            regions[path].append(
                [name, line, column, end_line, end_column, kind, expansion_path]
            )
    result = {}
    for path in sorted(regions):
        values = sorted(regions[path], key=canonical)
        require(values)
        if lane == "core":
            require(path.removeprefix(prefix).startswith(CORE_ROOT))
        result[path] = {
            "native_last_line": max(value[3] for value in values),
            "geometry_sha256": sha256(canonical(values)),
        }
    required_root = CORE_ROOT if lane == "core" else APP_ROOT
    require(any(path.removeprefix(prefix).startswith(required_root) for path in result))
    return result


def validate_domain(value: Any) -> dict[str, Any]:
    """Validate one closed lane witness without trusting its asserted identities."""
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "format",
            "platform",
            "lane",
            "architecture",
            "source_prefix",
            "llvm_version",
            "llvm_format",
            "xcode_build",
            "binary",
            "sources",
            "observed_sources",
        }
        and value["format"] == DOMAIN_FORMAT
        and value["platform"] == "ios"
        and value["architecture"] == "arm64"
        and value["lane"] in LANES
        and value["source_prefix"] in SOURCE_PREFIXES
        and value["llvm_version"] == LLVM_VERSION
        and value["llvm_format"] == LLVM_FORMAT
        and value["xcode_build"] == XCODE_BUILD
    )
    lane, prefix = value["lane"], value["source_prefix"]
    binary = value["binary"]
    require(
        isinstance(binary, dict)
        and set(binary) == {"member", "sha256", "artifact_member", "artifact_sha256"}
        and binary["member"] == BINARY_MEMBERS["core" if lane == "core" else "app"]
    )
    _digest(binary["sha256"])
    _digest(binary["artifact_sha256"])
    artifact = _text(binary["artifact_member"])
    require(
        artifact
        in {
            "coverage/raw/apple-ios-core_products.zip"
            if lane == "core"
            else "coverage/raw/apple-ios-products.zip",
            f"coverage/native-binaries/apple-ios-{lane}.zip",
        }
    )
    sources, observed = value["sources"], value["observed_sources"]
    require(isinstance(sources, dict) and 0 < len(sources) <= MAX_FILES)
    require(
        isinstance(observed, list)
        and observed
        and all(isinstance(path, str) for path in observed)
    )
    require(observed == sorted(set(observed)))
    require(set(observed) <= set(sources))
    for path, facts in sources.items():
        source_path(path, prefix)
        require(
            isinstance(facts, dict)
            and set(facts)
            == {
                "source_sha256",
                "physical_lines",
                "native_last_line",
                "geometry_sha256",
            }
        )
        _digest(facts["source_sha256"])
        _digest(facts["geometry_sha256"])
        physical = _integer(facts["physical_lines"], minimum=1)
        _integer(facts["native_last_line"], minimum=1, maximum=physical)
        if lane == "core":
            require(path.removeprefix(prefix).startswith(CORE_ROOT))
    required_root = CORE_ROOT if lane == "core" else APP_ROOT
    required = {
        path for path in sources if path.removeprefix(prefix).startswith(required_root)
    }
    require(required and required <= set(observed))
    return value


def native_report(
    coverage: Mapping[str, Any], domains: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind unchanged native arrays to exact observed lane domains."""
    require(
        isinstance(coverage, dict)
        and coverage
        and isinstance(domains, dict)
        and domains
    )
    require(set(domains) <= set(LANES))
    seen: set[str] = set()
    facts_by_path: dict[str, Any] = {}
    prefixes: set[str] = set()
    for lane, value in domains.items():
        domain = validate_domain(value)
        require(domain["lane"] == lane)
        prefixes.add(domain["source_prefix"])
        seen.update(domain["observed_sources"])
        for path, facts in domain["sources"].items():
            require(path not in facts_by_path or facts_by_path[path] == facts)
            facts_by_path[path] = facts
        for path in domain["observed_sources"]:
            rows = coverage.get(path)
            require(
                isinstance(rows, list)
                and len(rows) == domain["sources"][path]["native_last_line"]
            )
            require(
                all(
                    isinstance(row, dict) and type(row.get("line")) is int
                    for row in rows
                )
                and [row.get("line") for row in rows if isinstance(row, dict)]
                == list(range(1, len(rows) + 1))
            )
    require(len(prefixes) == 1 and seen == set(coverage))
    return {
        "format": REPORT_FORMAT,
        "platform": "ios",
        "coverage": coverage,
        "domains": domains,
    }


def parse_native_report(value: Any) -> dict[str, Any]:
    """Refuse altered envelope fields before using any native completeness fact."""
    require(
        isinstance(value, dict)
        and set(value) == {"format", "platform", "coverage", "domains"}
    )
    require(value["format"] == REPORT_FORMAT and value["platform"] == "ios")
    return native_report(value["coverage"], value["domains"])


def make_domain(
    *,
    geometry: Mapping[str, Mapping[str, Any]],
    source_bytes: Callable[[str], bytes],
    binary: dict[str, str],
    observed_sources: Sequence[str],
    prefix: str,
    lane: str,
) -> dict[str, Any]:
    """Construct a witness from independently read mapping and exact source bytes."""
    sources = {}
    for path, mapping in geometry.items():
        sources[path] = {**source_facts(source_bytes(path)), **mapping}
    return validate_domain(
        {
            "format": DOMAIN_FORMAT,
            "platform": "ios",
            "lane": lane,
            "architecture": "arm64",
            "source_prefix": prefix,
            "llvm_version": LLVM_VERSION,
            "llvm_format": LLVM_FORMAT,
            "xcode_build": XCODE_BUILD,
            "binary": binary,
            "sources": sources,
            "observed_sources": sorted(observed_sources),
        }
    )


def test_identity(summary: Any, tests: Any, lane: str) -> None:
    """Bind the mapping architecture/platform and lane to native test metadata.

    The protected artifact helper separately enforces the complete required
    suite inventory. This check is also used by unprivileged local producers.
    """
    require(lane in LANES and isinstance(summary, dict) and isinstance(tests, dict))
    require(summary.get("result") == "Passed")
    total = _integer(summary.get("totalTestCount"), minimum=1, maximum=50_000)
    require(type(summary.get("passedTests")) is int and summary["passedTests"] == total)
    for field in ("failedTests", "skippedTests", "expectedFailures"):
        require(type(summary.get(field)) is int and summary[field] == 0)
    values = summary.get("devicesAndConfigurations")
    require(
        isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict)
    )
    device = values[0].get("device")
    require(
        isinstance(device, dict)
        and device.get("platform") == "iOS Simulator"
        and device.get("architecture") == "arm64"
    )
    devices = tests.get("devices")
    require(
        isinstance(devices, list)
        and len(devices) == 1
        and isinstance(devices[0], dict)
        and devices[0].get("deviceId") == device.get("deviceId")
        and devices[0].get("platform") == "iOS Simulator"
        and devices[0].get("architecture") == "arm64"
    )
    _text(device.get("deviceId"))
    nodes = tests.get("testNodes")
    require(isinstance(nodes, list) and len(nodes) == 1 and isinstance(nodes[0], dict))
    plan = nodes[0]
    require(plan.get("name") == ("AstralCore" if lane == "core" else "AstralApp"))
    require(plan.get("nodeType") == "Test Plan" and plan.get("result") == "Passed")
    bundles = plan.get("children")
    require(
        isinstance(bundles, list) and len(bundles) == 1 and isinstance(bundles[0], dict)
    )
    expected = {"core": "AstralCoreTests", "unit": "AstralAppTests"}.get(
        lane, "AstralAppUITests"
    )
    require(bundles[0].get("name") == expected and bundles[0].get("result") == "Passed")
    cases = []
    pending = [bundles[0]]
    seen = 0
    while pending:
        node = pending.pop()
        seen += 1
        require(seen <= 50_000 and isinstance(node, dict))
        if node.get("nodeType") == "Test Case":
            require(node.get("result") == "Passed")
            cases.append(_text(node.get("nodeIdentifier")))
        children = node.get("children", [])
        require(isinstance(children, list))
        pending.extend(children)
    require(len(cases) == total and len(set(cases)) == total)
    staging = "ReleaseEvidenceUITests/testReleaseEvidenceProducesPlatformReport()"
    require(cases == [staging] if lane == "staging" else staging not in cases)


def collect_domain(
    *,
    binary_path: Path,
    binary_bytes: bytes,
    binary_identity: dict[str, str],
    archive_root: str,
    prefix: str,
    lane: str,
    tracked: set[str],
    source_bytes: Callable[[str], bytes],
    run: Callable[[Sequence[str], int], bytes],
    summary: Any,
    tests: Any,
) -> dict[str, Any]:
    """Read only the exact selected binary's mapping under the pinned toolchain.

    Callers provide a private immutable copy from the already validated retained
    artifact. There is no object-list, architecture, filter, or profile override.
    """
    test_identity(summary, tests, lane)
    ios_macho(binary_bytes, lane)
    require(sha256(binary_bytes) == binary_identity.get("sha256"))
    require(binary_path.is_file() and not binary_path.is_symlink())
    before = binary_path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_size == len(binary_bytes))
    descriptor = os.open(binary_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(stream.read(len(binary_bytes) + 1) == binary_bytes)
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    require(all(getattr(before, field) == getattr(opened, field) for field in stable))
    require(
        run(["/usr/bin/xcodebuild", "-version"], 4096).decode().strip()
        == "Xcode 26.6\nBuild version " + XCODE_BUILD
    )
    version = (
        run(["/usr/bin/xcrun", "llvm-cov", "--version"], 4096).decode().splitlines()
    )
    require(version and version[0] == LLVM_VERSION)
    raw = run(
        [
            "/usr/bin/xcrun",
            "llvm-cov",
            "export",
            "--empty-profile",
            "--arch=arm64",
            str(binary_path),
        ],
        MAX_MAPPING_BYTES,
    )
    after = binary_path.lstat()
    require(
        not binary_path.is_symlink()
        and all(getattr(before, field) == getattr(after, field) for field in stable)
    )
    geometry = mapping_geometry(
        raw, archive_root=archive_root, prefix=prefix, tracked=tracked, lane=lane
    )
    return make_domain(
        geometry=geometry,
        source_bytes=source_bytes,
        binary=binary_identity,
        observed_sources=sorted(geometry),
        prefix=prefix,
        lane=lane,
    )
