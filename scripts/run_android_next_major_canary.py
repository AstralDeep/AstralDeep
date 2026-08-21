#!/usr/bin/env python3
"""Run Android's separately pinned next-major toolchain in an isolated copy.

The declaration may truthfully say that the requested major is not published
yet. That state is never a passing canary: the default command exits with
``EX_UNAVAILABLE``. CI may explicitly request an availability diagnostic, which
re-queries the official AGP and Gradle metadata and succeeds until stable public
releases exist for both majors. Prereleases never activate the canary. Once
exact stable artifacts are declared by a separately authorized future change,
the runner replaces only the isolated copy's AGP/wrapper pins, proves the
versions resolved by Gradle itself, and runs configuration, lint, tests, and
assembly with warnings as errors.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


EX_UNAVAILABLE = 69
MAX_METADATA_BYTES = 4 * 1024 * 1024
VERSION = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)
STABLE_VERSION = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
AGP_PIN = re.compile(r'(?m)^(agp\s*=\s*")([^"\r\n]+)("\s*)$')
WRAPPER_URL = re.compile(r"(?m)^distributionUrl=.*$")
WRAPPER_SHA = re.compile(r"(?m)^distributionSha256Sum=.*$")
RESOLVED_AGP = re.compile(r"(?m)^ASTRAL_RESOLVED_AGP=(\S+)$")
RESOLVED_GRADLE = re.compile(r"(?m)^ASTRAL_RESOLVED_GRADLE=(\S+)$")
UNRELEASED = "UNRELEASED"
EXPECTED_KEYS = frozenset(
    {
        "schema_version",
        "availability",
        "availability_checked_on",
        "agp_major",
        "agp_version",
        "gradle_major",
        "gradle_version",
        "gradle_distribution_url",
        "gradle_distribution_sha256",
        "agp_metadata_url",
        "gradle_versions_url",
        "roadmap_url",
        "tasks",
    }
)
KNOWN_BLOCKERS = (
    ("gradle.properties", re.compile(r"(?m)^android\.builtInKotlin\s*=\s*false\s*$")),
    ("gradle.properties", re.compile(r"(?m)^android\.newDsl\s*=\s*false\s*$")),
    (
        "gradle.properties",
        re.compile(r"(?m)^android\.defaults\.buildfeatures\.resvalues\s*="),
    ),
    (
        "gradle.properties",
        re.compile(r"(?m)^android\.sdk\.defaultTargetSdkToCompileSdkIfUnset\s*="),
    ),
    (
        "gradle.properties",
        re.compile(r"(?m)^android\.enableAppCompileTimeRClass\s*="),
    ),
    (
        "gradle.properties",
        re.compile(r"(?m)^android\.usesSdkInManifest\.disallowed\s*="),
    ),
    (
        "gradle.properties",
        re.compile(r"(?m)^android\.r8\.optimizedResourceShrinking\s*="),
    ),
    (
        "gradle.properties",
        re.compile(r"(?m)^android\.dependency\.useConstraints\s*="),
    ),
    ("build.gradle.kts", re.compile(r"libs\.plugins\.kotlin\.android")),
    ("app/build.gradle.kts", re.compile(r"libs\.plugins\.kotlin\.android")),
    ("app/build.gradle.kts", re.compile(r"org\.jetbrains\.kotlin\.android")),
    (
        "app/build.gradle.kts",
        re.compile(
            r"(?:implementation|api|compileOnly|runtimeOnly)\s*"
            r"\(\s*project\s*\("
        ),
    ),
    (
        "app/build.gradle.kts",
        re.compile(r"\b(?:applicationVariants|libraryVariants|variantFilter)\b"),
    ),
)


class CanaryError(RuntimeError):
    """Base class for stable next-major canary failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CanaryConfigError(CanaryError):
    """Raised when pins or source configuration cannot prove the contract."""


class CanaryUnavailable(CanaryError):
    """Raised when both stable official next-major artifacts are not published."""


class CanaryExecutionError(CanaryError):
    """Raised when the isolated next-major Gradle execution fails."""


@dataclass(frozen=True)
class CanaryPins:
    """Strict, separately declared Android next-major toolchain inputs."""

    availability: str
    availability_checked_on: str
    agp_major: int
    agp_version: str | None
    gradle_major: int
    gradle_version: str | None
    gradle_distribution_url: str | None
    gradle_distribution_sha256: str | None
    agp_metadata_url: str
    gradle_versions_url: str
    roadmap_url: str
    tasks: tuple[str, ...]


@dataclass(frozen=True)
class ShippingToolchain:
    """Exact AGP and Gradle versions declared by the shipping Android source."""

    agp_version: str
    gradle_version: str


CommandRunner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]
MetadataFetcher = Callable[[str], bytes]


def _read_properties(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CanaryConfigError("properties_unreadable", str(exc)) from exc
    values: dict[str, str] = {}
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "=" not in line:
            raise CanaryConfigError(
                "invalid_property", f"{path}:{line_number}: expected key=value"
            )
        key, value = (item.strip() for item in line.split("=", 1))
        if not key or not value:
            raise CanaryConfigError(
                "invalid_property", f"{path}:{line_number}: empty key or value"
            )
        if key in values:
            raise CanaryConfigError(
                "duplicate_property", f"{path}:{line_number}: duplicate {key}"
            )
        values[key] = value
    missing = EXPECTED_KEYS - values.keys()
    extra = values.keys() - EXPECTED_KEYS
    if missing or extra:
        raise CanaryConfigError(
            "property_set_mismatch",
            f"missing={sorted(missing)} extra={sorted(extra)}",
        )
    return values


def _parse_positive_int(values: Mapping[str, str], key: str) -> int:
    try:
        parsed = int(values[key])
    except ValueError as exc:
        raise CanaryConfigError("invalid_integer", f"{key} must be an integer") from exc
    if parsed <= 0:
        raise CanaryConfigError("invalid_integer", f"{key} must be positive")
    return parsed


def _version_major(version: str, *, key: str, stable: bool = False) -> int:
    match = (STABLE_VERSION if stable else VERSION).fullmatch(version)
    if match is None:
        qualifier = "stable " if stable else ""
        raise CanaryConfigError(
            "invalid_version", f"{key} is not an exact {qualifier}version"
        )
    return int(match.group("major"))


def _official_https_url(value: str, *, key: str, hosts: frozenset[str]) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CanaryConfigError("invalid_official_url", f"invalid {key}")
    return value


def load_pins(path: Path | str) -> CanaryPins:
    """Load and fail-closed validate one exact canary declaration.

    ``unreleased`` declarations must use the literal ``UNRELEASED`` instead of
    a guessed version/checksum. ``available`` declarations require exact
    stable versions whose majors match their independent major pins and an
    official Gradle distribution URL plus lowercase SHA-256.
    """

    path = Path(path).resolve()
    values = _read_properties(path)
    if values["schema_version"] != "1":
        raise CanaryConfigError("unsupported_schema", "schema_version must be 1")
    try:
        date.fromisoformat(values["availability_checked_on"])
    except ValueError as exc:
        raise CanaryConfigError(
            "invalid_checked_on", "availability_checked_on must be YYYY-MM-DD"
        ) from exc
    agp_major = _parse_positive_int(values, "agp_major")
    gradle_major = _parse_positive_int(values, "gradle_major")
    if agp_major != 10:
        raise CanaryConfigError("wrong_agp_major", "AGP canary must target major 10")
    if gradle_major != 10:
        raise CanaryConfigError(
            "wrong_gradle_major", "Gradle canary must target major 10"
        )

    agp_metadata_url = _official_https_url(
        values["agp_metadata_url"], key="agp_metadata_url", hosts=frozenset({"dl.google.com"})
    )
    gradle_versions_url = _official_https_url(
        values["gradle_versions_url"],
        key="gradle_versions_url",
        hosts=frozenset({"services.gradle.org"}),
    )
    roadmap_url = _official_https_url(
        values["roadmap_url"],
        key="roadmap_url",
        hosts=frozenset({"developer.android.com"}),
    )
    tasks = tuple(item.strip() for item in values["tasks"].split(",") if item.strip())
    mandatory_tasks = {
        "help",
        "ktlintCheck",
        ":app:lintDebug",
        ":core:test",
        ":app:testDebugUnitTest",
        ":app:assembleDebug",
    }
    if len(tasks) != len(set(tasks)) or not mandatory_tasks <= set(tasks):
        raise CanaryConfigError(
            "invalid_tasks", "tasks must uniquely include configuration/lint/test/assemble"
        )

    availability = values["availability"]
    if availability == "unreleased":
        unavailable_keys = (
            "agp_version",
            "gradle_version",
            "gradle_distribution_url",
            "gradle_distribution_sha256",
        )
        if any(values[key] != UNRELEASED for key in unavailable_keys):
            raise CanaryConfigError(
                "fabricated_unreleased_pin",
                "unreleased tools must not declare versions, URL, or checksum",
            )
        return CanaryPins(
            availability,
            values["availability_checked_on"],
            agp_major,
            None,
            gradle_major,
            None,
            None,
            None,
            agp_metadata_url,
            gradle_versions_url,
            roadmap_url,
            tasks,
        )
    if availability != "available":
        raise CanaryConfigError(
            "invalid_availability", "availability must be available or unreleased"
        )

    agp_version = values["agp_version"]
    gradle_version = values["gradle_version"]
    if _version_major(agp_version, key="agp_version", stable=True) != agp_major:
        raise CanaryConfigError("wrong_agp_major", "AGP version does not match major pin")
    if _version_major(gradle_version, key="gradle_version", stable=True) != gradle_major:
        raise CanaryConfigError(
            "wrong_gradle_major", "Gradle version does not match major pin"
        )
    distribution_url = _official_https_url(
        values["gradle_distribution_url"],
        key="gradle_distribution_url",
        hosts=frozenset({"services.gradle.org"}),
    )
    expected_suffix = f"/gradle-{gradle_version}-bin.zip"
    if not urllib.parse.urlsplit(distribution_url).path.endswith(expected_suffix):
        raise CanaryConfigError(
            "distribution_version_mismatch", "Gradle URL does not bind the exact pin"
        )
    distribution_sha256 = values["gradle_distribution_sha256"]
    if SHA256.fullmatch(distribution_sha256) is None:
        raise CanaryConfigError(
            "invalid_distribution_sha256", "Gradle distribution needs lowercase SHA-256"
        )
    return CanaryPins(
        availability,
        values["availability_checked_on"],
        agp_major,
        agp_version,
        gradle_major,
        gradle_version,
        distribution_url,
        distribution_sha256,
        agp_metadata_url,
        gradle_versions_url,
        roadmap_url,
        tasks,
    )


def inspect_shipping_toolchain(source_root: Path | str) -> ShippingToolchain:
    """Read exact shipping AGP and Gradle pins from the Android source tree."""

    source_root = Path(source_root).resolve()
    try:
        catalog = (source_root / "gradle" / "libs.versions.toml").read_text(
            encoding="utf-8"
        )
        wrapper = (source_root / "gradle" / "wrapper" / "gradle-wrapper.properties").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        raise CanaryConfigError("shipping_toolchain_unreadable", str(exc)) from exc
    agp_matches = AGP_PIN.findall(catalog)
    gradle_matches = re.findall(r"gradle-([^/\\]+)-bin\.zip", wrapper)
    if len(agp_matches) != 1 or len(gradle_matches) != 1:
        raise CanaryConfigError(
            "shipping_toolchain_ambiguous", "expected one shipping AGP and Gradle pin"
        )
    agp_version = agp_matches[0][1]
    gradle_version = gradle_matches[0]
    _version_major(agp_version, key="shipping_agp")
    _version_major(gradle_version, key="shipping_gradle")
    return ShippingToolchain(agp_version, gradle_version)


def verify_migration_blockers_removed(source_root: Path | str) -> None:
    """Reject known AGP/Gradle-10 removal blockers in shipping build logic."""

    source_root = Path(source_root).resolve()
    for relative, pattern in KNOWN_BLOCKERS:
        path = source_root / relative
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CanaryConfigError("build_source_unreadable", str(exc)) from exc
        match = pattern.search(source)
        if match is not None:
            raise CanaryConfigError(
                "known_removal_blocker",
                f"{relative} still contains {match.group(0)!r}",
            )
    settings = (source_root / "settings.gradle.kts").read_text(encoding="utf-8")
    app_build = (source_root / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    if 'enableFeaturePreview("TYPESAFE_PROJECT_ACCESSORS")' not in settings:
        raise CanaryConfigError(
            "known_removal_blocker", "type-safe project accessors are not enabled"
        )
    if "implementation(projects.core)" not in app_build:
        raise CanaryConfigError(
            "known_removal_blocker", "app dependency is not the type-safe core accessor"
        )


def _fetch_official_metadata(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AstralDeep-Android-Canary/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_METADATA_BYTES:
                raise CanaryConfigError("metadata_too_large", url)
            payload = response.read(MAX_METADATA_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise CanaryConfigError("metadata_unavailable", f"{url}: {exc}") from exc
    if len(payload) > MAX_METADATA_BYTES:
        raise CanaryConfigError("metadata_too_large", url)
    return payload


def _has_major(versions: Sequence[str], major: int) -> bool:
    """Return whether official metadata contains a stable release for ``major``."""

    for version in versions:
        match = STABLE_VERSION.fullmatch(version)
        if match is not None and int(match.group("major")) == major:
            return True
    return False


def probe_official_availability(
    pins: CanaryPins, *, fetcher: MetadataFetcher = _fetch_official_metadata
) -> dict[str, bool]:
    """Report whether each target major has a stable official release."""

    try:
        agp_root = ET.fromstring(fetcher(pins.agp_metadata_url))
        agp_versions = [
            node.text.strip()
            for node in agp_root.findall("./versioning/versions/version")
            if node.text and node.text.strip()
        ]
    except (ET.ParseError, UnicodeError) as exc:
        raise CanaryConfigError("invalid_agp_metadata", str(exc)) from exc
    try:
        gradle_payload = json.loads(fetcher(pins.gradle_versions_url))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CanaryConfigError("invalid_gradle_metadata", str(exc)) from exc
    if not isinstance(gradle_payload, list):
        raise CanaryConfigError("invalid_gradle_metadata", "expected a JSON list")
    gradle_versions: list[str] = []
    for entry in gradle_payload:
        if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
            raise CanaryConfigError("invalid_gradle_metadata", "invalid version member")
        gradle_versions.append(entry["version"])
    return {
        "agp_major_available": _has_major(agp_versions, pins.agp_major),
        "gradle_major_available": _has_major(gradle_versions, pins.gradle_major),
    }


def validate_unreleased_declaration(
    pins: CanaryPins, availability: Mapping[str, bool]
) -> None:
    """Fail when official metadata makes an unreleased declaration stale."""

    if pins.availability != "unreleased":
        raise CanaryConfigError(
            "availability_state_mismatch", "expected an unreleased declaration"
        )
    if availability.get("agp_major_available") and availability.get(
        "gradle_major_available"
    ):
        raise CanaryConfigError(
            "unreleased_declaration_stale",
            "both stable official target-major releases now exist; a future "
            "authorized change must review exact pins",
        )


def _replace_once(source: str, pattern: re.Pattern[str], replacement: str, label: str) -> str:
    updated, count = pattern.subn(replacement, source)
    if count != 1:
        raise CanaryConfigError("isolated_patch_mismatch", f"expected one {label}")
    return updated


def _prepare_isolated_checkout(source_root: Path, checkout: Path, pins: CanaryPins) -> None:
    ignored = shutil.ignore_patterns(
        ".gradle",
        ".idea",
        "build",
        "local.properties",
        "keystore.properties",
        "*.jks",
        "*.keystore",
        ".DS_Store",
    )
    shutil.copytree(source_root, checkout, symlinks=False, ignore=ignored)
    assert pins.agp_version is not None
    assert pins.gradle_distribution_url is not None
    assert pins.gradle_distribution_sha256 is not None
    catalog_path = checkout / "gradle" / "libs.versions.toml"
    catalog = catalog_path.read_text(encoding="utf-8")
    catalog = _replace_once(
        catalog,
        AGP_PIN,
        rf'\g<1>{pins.agp_version}\g<3>',
        "AGP catalog pin",
    )
    catalog_path.write_text(catalog, encoding="utf-8")

    wrapper_path = checkout / "gradle" / "wrapper" / "gradle-wrapper.properties"
    wrapper = wrapper_path.read_text(encoding="utf-8")
    escaped_url = pins.gradle_distribution_url.replace("https://", "https\\://", 1)
    wrapper = _replace_once(
        wrapper,
        WRAPPER_URL,
        f"distributionUrl={escaped_url}",
        "Gradle distribution URL",
    )
    if WRAPPER_SHA.search(wrapper):
        wrapper = _replace_once(
            wrapper,
            WRAPPER_SHA,
            f"distributionSha256Sum={pins.gradle_distribution_sha256}",
            "Gradle distribution checksum",
        )
    else:
        wrapper = wrapper.rstrip() + (
            f"\ndistributionSha256Sum={pins.gradle_distribution_sha256}\n"
        )
    wrapper_path.write_text(wrapper, encoding="utf-8")

    gradle_properties_path = checkout / "gradle.properties"
    gradle_properties = gradle_properties_path.read_text(encoding="utf-8")
    for key in ("android.newDsl", "android.builtInKotlin"):
        gradle_properties = re.sub(
            rf"(?m)^{re.escape(key)}\s*=.*\n?", "", gradle_properties
        )
        gradle_properties = gradle_properties.rstrip() + f"\n{key}=true\n"
    gradle_properties_path.write_text(gradle_properties, encoding="utf-8")

    app_build_path = checkout / "app" / "build.gradle.kts"
    app_build = app_build_path.read_text(encoding="utf-8")
    if "astralCanaryResolvedVersions" in app_build:
        raise CanaryConfigError(
            "isolated_task_collision", "source already defines canary probe task"
        )
    app_build += """

tasks.register("astralCanaryResolvedVersions") {
    doLast {
        println("ASTRAL_RESOLVED_AGP=${androidComponents.pluginVersion.version}")
        println("ASTRAL_RESOLVED_GRADLE=${gradle.gradleVersion}")
    }
}
"""
    app_build_path.write_text(app_build, encoding="utf-8")


def _default_command_runner(
    command: tuple[str, ...], cwd: Path
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=45 * 60,
    )
    if completed.returncode != 0:
        tail = completed.stdout[-8000:]
        raise CanaryExecutionError(
            "gradle_failed", f"{' '.join(command)} failed:\n{tail}"
        )
    return completed


def _one_resolved(pattern: re.Pattern[str], output: str, label: str) -> str:
    matches = pattern.findall(output)
    if len(matches) != 1:
        raise CanaryExecutionError(
            "resolved_version_missing", f"expected one resolved {label} marker"
        )
    return matches[0]


def run_canary(
    properties_path: Path | str,
    *,
    source_root: Path | str | None = None,
    temp_parent: Path | str | None = None,
    command_runner: CommandRunner = _default_command_runner,
) -> dict[str, Any]:
    """Execute the exact next-major pins in a temporary Android checkout.

    The temporary directory is removed by ``TemporaryDirectory`` after success,
    a Gradle failure, a timeout, or a version assertion failure. Every Gradle
    invocation includes ``--warning-mode=fail`` so removal warnings are fatal.
    """

    pins = load_pins(properties_path)
    if source_root is None:
        source_root = (
            Path(__file__).resolve().parents[1]
            / "components"
            / "AstralProjection"
            / "android-client"
        )
    source_root = Path(source_root).resolve()
    verify_migration_blockers_removed(source_root)
    shipping = inspect_shipping_toolchain(source_root)
    if pins.availability != "available":
        raise CanaryUnavailable(
            "toolchain_unreleased",
            "AGP 10 and Gradle 10 stable public artifacts are not declared",
        )
    assert pins.agp_version is not None and pins.gradle_version is not None
    if (
        pins.agp_version == shipping.agp_version
        or pins.gradle_version == shipping.gradle_version
    ):
        raise CanaryConfigError(
            "shipping_toolchain_reused",
            "the compatibility toolchain must be separate from shipping pins",
        )

    parent = Path(temp_parent).resolve() if temp_parent is not None else None
    if parent is not None and not parent.is_dir():
        raise CanaryConfigError("temp_parent_missing", str(parent))
    command_records: list[list[str]] = []
    resolved: dict[str, str]
    with tempfile.TemporaryDirectory(
        prefix="astraldeep-android-next-major-",
        dir=str(parent) if parent is not None else None,
    ) as temporary:
        checkout = Path(temporary) / "android-client"
        _prepare_isolated_checkout(source_root, checkout, pins)
        probe_command = (
            "sh",
            "./gradlew",
            ":app:astralCanaryResolvedVersions",
            "--warning-mode=fail",
            "--no-daemon",
            "--stacktrace",
        )
        probe = command_runner(probe_command, checkout)
        command_records.append(list(probe_command))
        resolved = {
            "agp": _one_resolved(RESOLVED_AGP, probe.stdout, "AGP"),
            "gradle": _one_resolved(RESOLVED_GRADLE, probe.stdout, "Gradle"),
        }
        if resolved != {"agp": pins.agp_version, "gradle": pins.gradle_version}:
            raise CanaryExecutionError(
                "resolved_version_mismatch",
                f"declared AGP/Gradle differ from resolved versions: {resolved}",
            )
        build_command = (
            "sh",
            "./gradlew",
            *pins.tasks,
            "--warning-mode=fail",
            "--no-daemon",
            "--stacktrace",
        )
        command_runner(build_command, checkout)
        command_records.append(list(build_command))

    return {
        "schema_version": 1,
        "status": "passed",
        "availability_checked_on": pins.availability_checked_on,
        "declared": {"agp": pins.agp_version, "gradle": pins.gradle_version},
        "resolved": resolved,
        "shipping": {
            "agp": shipping.agp_version,
            "gradle": shipping.gradle_version,
        },
        "warnings_as_errors": True,
        "isolated_checkout_cleaned": True,
        "commands": command_records,
    }


def _write_report(path: Path | None, report: Mapping[str, Any]) -> None:
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    if path is None:
        sys.stdout.write(payload)
        return
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    temporary = path.with_name(f".{path.name}.{digest}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    sys.stdout.write(payload)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the canary or an explicit official-unavailability diagnostic."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("properties", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--temp-parent", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-unreleased",
        action="store_true",
        help="emit a non-passing unavailable diagnostic instead of exit 69",
    )
    parser.add_argument(
        "--verify-official-availability",
        action="store_true",
        help="re-query official metadata; required with --allow-unreleased",
    )
    args = parser.parse_args(argv)
    try:
        pins = load_pins(args.properties)
        if args.allow_unreleased and not args.verify_official_availability:
            raise CanaryConfigError(
                "unverified_unreleased_override",
                "--allow-unreleased requires --verify-official-availability",
            )
        if pins.availability == "unreleased":
            availability: dict[str, bool] | None = None
            if args.verify_official_availability:
                availability = probe_official_availability(pins)
                validate_unreleased_declaration(pins, availability)
            report = {
                "schema_version": 1,
                "status": "unavailable",
                "reason": "toolchain_unreleased",
                "availability_checked_on": pins.availability_checked_on,
                "target_majors": {"agp": pins.agp_major, "gradle": pins.gradle_major},
                "official_probe": availability,
                "roadmap_url": pins.roadmap_url,
                "canary_passed": False,
            }
            _write_report(args.output, report)
            return 0 if args.allow_unreleased else EX_UNAVAILABLE
        report = run_canary(
            args.properties,
            source_root=args.source_root,
            temp_parent=args.temp_parent,
        )
        _write_report(args.output, report)
        return 0
    except CanaryUnavailable as exc:
        _write_report(
            args.output,
            {"schema_version": 1, "status": "unavailable", "code": exc.code},
        )
        return EX_UNAVAILABLE
    except CanaryConfigError as exc:
        _write_report(
            args.output,
            {"schema_version": 1, "status": "failed", "code": exc.code},
        )
        return 2
    except CanaryExecutionError as exc:
        _write_report(
            args.output,
            {"schema_version": 1, "status": "failed", "code": exc.code},
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
