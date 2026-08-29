"""Contract tests for the isolated Android next-major compatibility driver."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTION_ROOT = REPO_ROOT / "components" / "AstralProjection"
SCRIPT = REPO_ROOT / "scripts" / "run_android_next_major_canary.py"
PINS = PROJECTION_ROOT / "android-client" / "gradle" / "next-major-canary.properties"

if not (REPO_ROOT / "scripts").is_dir():  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )


def _load_driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("android_next_major_060", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


def _android_fixture(root: Path, *, agp: str = "9.3.0", gradle: str = "9.6.1") -> Path:
    source = root / "android-client"
    (source / "gradle" / "wrapper").mkdir(parents=True)
    (source / "app").mkdir()
    (source / "gradle" / "libs.versions.toml").write_text(
        f'[versions]\nagp = "{agp}"\n', encoding="utf-8"
    )
    (source / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
        "distributionUrl=https\\://services.gradle.org/distributions/"
        f"gradle-{gradle}-bin.zip\n",
        encoding="utf-8",
    )
    (source / "gradle.properties").write_text(
        "org.gradle.configuration-cache=true\n", encoding="utf-8"
    )
    (source / "settings.gradle.kts").write_text(
        'enableFeaturePreview("TYPESAFE_PROJECT_ACCESSORS")\n'
        'include(":core", ":app")\n',
        encoding="utf-8",
    )
    (source / "build.gradle.kts").write_text(
        "plugins { alias(libs.plugins.android.application) apply false }\n",
        encoding="utf-8",
    )
    (source / "app" / "build.gradle.kts").write_text(
        "plugins { alias(libs.plugins.android.application) }\n"
        "dependencies { implementation(projects.core) }\n",
        encoding="utf-8",
    )
    (source / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    return source


def _pins(
    path: Path,
    *,
    status: str = "available",
    agp: str = "10.0.0",
    gradle: str = "10.0.1",
) -> Path:
    unavailable = status == "unreleased"
    agp_value = "UNRELEASED" if unavailable else agp
    gradle_value = "UNRELEASED" if unavailable else gradle
    checksum = "UNRELEASED" if unavailable else "a" * 64
    distribution = (
        "UNRELEASED"
        if unavailable
        else f"https://services.gradle.org/distributions/gradle-{gradle}-bin.zip"
    )
    path.write_text(
        "\n".join(
            (
                "schema_version=1",
                f"availability={status}",
                "availability_checked_on=2026-07-16",
                "agp_major=10",
                f"agp_version={agp_value}",
                "gradle_major=10",
                f"gradle_version={gradle_value}",
                f"gradle_distribution_url={distribution}",
                f"gradle_distribution_sha256={checksum}",
                "agp_metadata_url=https://dl.google.com/dl/android/maven2/com/android/tools/build/gradle/maven-metadata.xml",
                "gradle_versions_url=https://services.gradle.org/versions/all",
                "roadmap_url=https://developer.android.com/build/releases/gradle-plugin-roadmap",
                "tasks=help,ktlintCheck,:app:lintDebug,:core:test,:app:testDebugUnitTest,:app:assembleDebug",
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def _completed(command: tuple[str, ...], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def _replace_property(path: Path, key: str, value: str) -> Path:
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(
            f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_driver_is_stdlib_only_and_documents_public_contracts() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    imported: set[str] = set()
    public: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            public[node.name] = node
    imported.discard("__future__")
    assert imported <= sys.stdlib_module_names
    expected = {
        "load_pins",
        "inspect_shipping_toolchain",
        "verify_migration_blockers_removed",
        "probe_official_availability",
        "run_canary",
        "main",
    }
    assert expected <= set(public)
    assert all(ast.get_docstring(public[name]) for name in expected)


@pytest.mark.skipif(
    not PINS.is_file(),
    reason="Projection's Android pins are unavailable in the source-free checkout",
)
def test_repository_pins_truthfully_declare_both_major_ten_tools_unreleased() -> None:
    pins = driver.load_pins(PINS)
    assert (pins.agp_major, pins.gradle_major) == (10, 10)
    assert pins.availability == "unreleased"
    assert pins.agp_version is None
    assert pins.gradle_version is None
    assert pins.gradle_distribution_sha256 is None


def test_unreleased_tools_fail_closed_without_creating_a_checkout(tmp_path: Path) -> None:
    source = _android_fixture(tmp_path)
    properties = _pins(tmp_path / "pins.properties", status="unreleased")
    temp_parent = tmp_path / "isolated"
    temp_parent.mkdir()

    with pytest.raises(driver.CanaryUnavailable) as unavailable:
        driver.run_canary(properties, source_root=source, temp_parent=temp_parent)

    assert unavailable.value.code == "toolchain_unreleased"
    assert list(temp_parent.iterdir()) == []


@pytest.mark.parametrize(
    ("agp", "gradle", "expected_code"),
    [
        ("9.3.0", "10.0.1", "wrong_agp_major"),
        ("10.0.0", "9.6.1", "wrong_gradle_major"),
    ],
)
def test_major_nine_pin_cannot_masquerade_as_the_canary(
    tmp_path: Path, agp: str, gradle: str, expected_code: str
) -> None:
    properties = _pins(tmp_path / "pins.properties", agp=agp, gradle=gradle)
    with pytest.raises(driver.CanaryConfigError) as invalid:
        driver.load_pins(properties)
    assert invalid.value.code == expected_code


def test_shipping_toolchain_cannot_be_reused_as_the_separate_canary(tmp_path: Path) -> None:
    source = _android_fixture(
        tmp_path, agp="10.0.0", gradle="10.0.1"
    )
    properties = _pins(tmp_path / "pins.properties")
    with pytest.raises(driver.CanaryConfigError) as reused:
        driver.run_canary(properties, source_root=source)
    assert reused.value.code == "shipping_toolchain_reused"


def test_isolated_run_asserts_resolved_versions_and_warnings_as_errors(
    tmp_path: Path,
) -> None:
    source = _android_fixture(tmp_path)
    properties = _pins(tmp_path / "pins.properties")
    temp_parent = tmp_path / "isolated"
    temp_parent.mkdir()
    observed_checkout: list[Path] = []
    commands: list[tuple[str, ...]] = []

    def fake_runner(command: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        observed_checkout.append(cwd)
        assert cwd.is_dir() and cwd != source
        if any(item.endswith("astralCanaryResolvedVersions") for item in command):
            return _completed(
                command,
                "ASTRAL_RESOLVED_AGP=10.0.0\n"
                "ASTRAL_RESOLVED_GRADLE=10.0.1\n",
            )
        return _completed(command, "BUILD SUCCESSFUL\n")

    report = driver.run_canary(
        properties,
        source_root=source,
        temp_parent=temp_parent,
        command_runner=fake_runner,
    )

    assert report["status"] == "passed"
    assert report["resolved"] == {
        "agp": "10.0.0",
        "gradle": "10.0.1",
    }
    assert observed_checkout
    assert list(temp_parent.iterdir()) == []
    assert all("--warning-mode=fail" in command for command in commands)
    assert any(":app:lintDebug" in command for command in commands)
    assert any(":app:assembleDebug" in command for command in commands)


def test_checkout_is_cleaned_when_gradle_fails(tmp_path: Path) -> None:
    source = _android_fixture(tmp_path)
    properties = _pins(tmp_path / "pins.properties")
    temp_parent = tmp_path / "isolated"
    temp_parent.mkdir()
    seen: list[Path] = []

    def failing_runner(command: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        seen.append(cwd)
        raise driver.CanaryExecutionError("gradle_failed", "synthetic failure")

    with pytest.raises(driver.CanaryExecutionError):
        driver.run_canary(
            properties,
            source_root=source,
            temp_parent=temp_parent,
            command_runner=failing_runner,
        )
    assert seen
    assert list(temp_parent.iterdir()) == []


def test_source_preflight_rejects_each_known_removal_blocker(tmp_path: Path) -> None:
    source = _android_fixture(tmp_path)
    cases = [
        ("gradle.properties", "android.builtInKotlin=false\n"),
        ("gradle.properties", "android.dependency.useConstraints=false\n"),
        ("build.gradle.kts", "alias(libs.plugins.kotlin.android) apply false\n"),
        (
            "app/build.gradle.kts",
            "dependencies { implementation(project(\":core\")) }\n",
        ),
    ]
    for relative, blocker in cases:
        target = source / relative
        original = target.read_text(encoding="utf-8")
        target.write_text(original + blocker, encoding="utf-8")
        with pytest.raises(driver.CanaryConfigError) as invalid:
            driver.verify_migration_blockers_removed(source)
        assert invalid.value.code == "known_removal_blocker"
        target.write_text(original, encoding="utf-8")


def test_official_probe_rejects_stale_unreleased_declaration(tmp_path: Path) -> None:
    pins = driver.load_pins(_pins(tmp_path / "pins.properties", status="unreleased"))

    def fake_fetch(url: str) -> bytes:
        if "maven-metadata" in url:
            return b"<metadata><versioning><versions><version>10.0.0</version></versions></versioning></metadata>"
        return json.dumps([{"version": "10.0.1"}]).encode()

    availability = driver.probe_official_availability(pins, fetcher=fake_fetch)
    assert availability == {
        "agp_major_available": True,
        "gradle_major_available": True,
    }
    with pytest.raises(driver.CanaryConfigError) as stale:
        driver.validate_unreleased_declaration(pins, availability)
    assert stale.value.code == "unreleased_declaration_stale"


@pytest.mark.parametrize(
    ("agp_versions", "gradle_versions", "expected"),
    [
        (
            ["10.0.0-alpha01", "10.0.0-rc01"],
            ["10.0.0-milestone-1", "10.0.0-rc-1"],
            {"agp_major_available": False, "gradle_major_available": False},
        ),
        (
            ["10.0.0"],
            ["10.0.0-milestone-1"],
            {"agp_major_available": True, "gradle_major_available": False},
        ),
        (
            ["10.0.0-alpha01"],
            ["10.0.1"],
            {"agp_major_available": False, "gradle_major_available": True},
        ),
    ],
)
def test_official_probe_requires_both_stable_public_releases(
    tmp_path: Path,
    agp_versions: list[str],
    gradle_versions: list[str],
    expected: dict[str, bool],
) -> None:
    pins = driver.load_pins(_pins(tmp_path / "pins.properties", status="unreleased"))

    def fake_fetch(url: str) -> bytes:
        if "maven-metadata" in url:
            versions = "".join(f"<version>{version}</version>" for version in agp_versions)
            return f"<metadata><versioning><versions>{versions}</versions></versioning></metadata>".encode()
        return json.dumps([{"version": version} for version in gradle_versions]).encode()

    availability = driver.probe_official_availability(pins, fetcher=fake_fetch)
    assert availability == expected
    driver.validate_unreleased_declaration(pins, availability)


@pytest.mark.parametrize(
    ("key", "value", "expected_code"),
    [
        ("schema_version", "2", "unsupported_schema"),
        ("availability_checked_on", "not-a-date", "invalid_checked_on"),
        ("agp_major", "ten", "invalid_integer"),
        ("agp_major", "0", "invalid_integer"),
        ("agp_major", "11", "wrong_agp_major"),
        ("gradle_major", "11", "wrong_gradle_major"),
        ("agp_metadata_url", "http://dl.google.com/metadata.xml", "invalid_official_url"),
        ("tasks", "help,help", "invalid_tasks"),
        ("availability", "pending", "invalid_availability"),
        ("agp_version", "latest", "invalid_version"),
        ("agp_version", "10.0.0-alpha01", "invalid_version"),
        ("gradle_version", "10.0.1-milestone-1", "invalid_version"),
        (
            "gradle_distribution_url",
            "https://services.gradle.org/distributions/gradle-10.0.2-bin.zip",
            "distribution_version_mismatch",
        ),
        ("gradle_distribution_sha256", "A" * 64, "invalid_distribution_sha256"),
    ],
)
def test_pin_declaration_rejects_invalid_values(
    tmp_path: Path, key: str, value: str, expected_code: str
) -> None:
    properties = _replace_property(_pins(tmp_path / "pins.properties"), key, value)

    with pytest.raises(driver.CanaryConfigError) as invalid:
        driver.load_pins(properties)

    assert invalid.value.code == expected_code


def test_unreleased_declaration_rejects_fabricated_exact_pin(tmp_path: Path) -> None:
    properties = _replace_property(
        _pins(tmp_path / "pins.properties", status="unreleased"),
        "agp_version",
        "10.0.0",
    )

    with pytest.raises(driver.CanaryConfigError) as invalid:
        driver.load_pins(properties)

    assert invalid.value.code == "fabricated_unreleased_pin"


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ("not-a-property\n", "invalid_property"),
        ("=value\n", "invalid_property"),
        ("schema_version=1\nschema_version=1\n", "duplicate_property"),
        ("schema_version=1\n", "property_set_mismatch"),
    ],
)
def test_property_file_structure_is_fail_closed(
    tmp_path: Path, content: str, expected_code: str
) -> None:
    properties = tmp_path / "pins.properties"
    properties.write_text(content, encoding="utf-8")

    with pytest.raises(driver.CanaryConfigError) as invalid:
        driver.load_pins(properties)

    assert invalid.value.code == expected_code


def test_missing_property_file_is_reported_as_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(driver.CanaryConfigError) as invalid:
        driver.load_pins(tmp_path / "missing.properties")

    assert invalid.value.code == "properties_unreadable"


def test_shipping_and_migration_source_errors_are_stable(tmp_path: Path) -> None:
    with pytest.raises(driver.CanaryConfigError) as unreadable:
        driver.inspect_shipping_toolchain(tmp_path)
    assert unreadable.value.code == "shipping_toolchain_unreadable"

    source = _android_fixture(tmp_path)
    catalog = source / "gradle" / "libs.versions.toml"
    catalog.write_text(catalog.read_text(encoding="utf-8") + 'agp = "9.4.0"\n')
    with pytest.raises(driver.CanaryConfigError) as ambiguous:
        driver.inspect_shipping_toolchain(source)
    assert ambiguous.value.code == "shipping_toolchain_ambiguous"

    catalog.write_text('[versions]\nagp = "9.3.0"\n', encoding="utf-8")
    (source / "gradle.properties").unlink()
    with pytest.raises(driver.CanaryConfigError) as build_unreadable:
        driver.verify_migration_blockers_removed(source)
    assert build_unreadable.value.code == "build_source_unreadable"


@pytest.mark.parametrize(
    ("relative", "replacement"),
    [
        ("settings.gradle.kts", 'include(":core", ":app")\n'),
        (
            "app/build.gradle.kts",
            "plugins { alias(libs.plugins.android.application) }\n",
        ),
    ],
)
def test_source_preflight_requires_type_safe_project_access(
    tmp_path: Path, relative: str, replacement: str
) -> None:
    source = _android_fixture(tmp_path)
    (source / relative).write_text(replacement, encoding="utf-8")

    with pytest.raises(driver.CanaryConfigError) as invalid:
        driver.verify_migration_blockers_removed(source)

    assert invalid.value.code == "known_removal_blocker"


@pytest.mark.parametrize(
    ("agp_payload", "gradle_payload", "expected_code"),
    [
        (b"<not-xml", b"[]", "invalid_agp_metadata"),
        (b"<metadata />", b"not-json", "invalid_gradle_metadata"),
        (b"<metadata />", b"{}", "invalid_gradle_metadata"),
        (b"<metadata />", b"[{}]", "invalid_gradle_metadata"),
    ],
)
def test_official_probe_rejects_malformed_metadata(
    tmp_path: Path,
    agp_payload: bytes,
    gradle_payload: bytes,
    expected_code: str,
) -> None:
    pins = driver.load_pins(_pins(tmp_path / "pins.properties", status="unreleased"))

    def fake_fetch(url: str) -> bytes:
        return agp_payload if "maven-metadata" in url else gradle_payload

    with pytest.raises(driver.CanaryConfigError) as invalid:
        driver.probe_official_availability(pins, fetcher=fake_fetch)

    assert invalid.value.code == expected_code


def test_official_probe_reports_absent_majors_and_requires_unreleased_state(
    tmp_path: Path,
) -> None:
    pins = driver.load_pins(_pins(tmp_path / "pins.properties", status="unreleased"))

    def fake_fetch(url: str) -> bytes:
        if "maven-metadata" in url:
            return b"<metadata><versioning><versions><version>9.9.0</version><version>invalid</version></versions></versioning></metadata>"
        return json.dumps([{"version": "9.9.0"}]).encode()

    availability = driver.probe_official_availability(pins, fetcher=fake_fetch)
    assert availability == {
        "agp_major_available": False,
        "gradle_major_available": False,
    }
    driver.validate_unreleased_declaration(pins, availability)

    available = driver.load_pins(_pins(tmp_path / "available.properties"))
    with pytest.raises(driver.CanaryConfigError) as invalid:
        driver.validate_unreleased_declaration(available, availability)
    assert invalid.value.code == "availability_state_mismatch"


class _MetadataResponse:
    def __init__(self, payload: bytes, content_length: str | None = None) -> None:
        self.payload = payload
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self) -> _MetadataResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


def test_metadata_fetch_is_bounded_and_maps_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        driver.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _MetadataResponse(b"metadata", "8"),
    )
    assert driver._fetch_official_metadata("https://dl.google.com/metadata") == b"metadata"

    monkeypatch.setattr(
        driver.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _MetadataResponse(
            b"", str(driver.MAX_METADATA_BYTES + 1)
        ),
    )
    with pytest.raises(driver.CanaryConfigError) as declared_large:
        driver._fetch_official_metadata("https://dl.google.com/metadata")
    assert declared_large.value.code == "metadata_too_large"

    monkeypatch.setattr(
        driver.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _MetadataResponse(
            b"x" * (driver.MAX_METADATA_BYTES + 1)
        ),
    )
    with pytest.raises(driver.CanaryConfigError) as received_large:
        driver._fetch_official_metadata("https://dl.google.com/metadata")
    assert received_large.value.code == "metadata_too_large"

    def unavailable(*_args: object, **_kwargs: object) -> _MetadataResponse:
        raise OSError("offline")

    monkeypatch.setattr(driver.urllib.request, "urlopen", unavailable)
    with pytest.raises(driver.CanaryConfigError) as transport:
        driver._fetch_official_metadata("https://dl.google.com/metadata")
    assert transport.value.code == "metadata_unavailable"


def test_isolated_patch_and_probe_contract_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(driver.CanaryConfigError) as patch_mismatch:
        driver._replace_once("unchanged", driver.AGP_PIN, "replacement", "AGP pin")
    assert patch_mismatch.value.code == "isolated_patch_mismatch"

    source = _android_fixture(tmp_path)
    app_build = source / "app" / "build.gradle.kts"
    app_build.write_text(
        app_build.read_text(encoding="utf-8") + "\nastralCanaryResolvedVersions\n",
        encoding="utf-8",
    )
    with pytest.raises(driver.CanaryConfigError) as collision:
        driver.run_canary(
            _pins(tmp_path / "pins.properties"),
            source_root=source,
            command_runner=lambda command, _cwd: _completed(command),
        )
    assert collision.value.code == "isolated_task_collision"


def test_existing_wrapper_checksum_is_replaced_in_isolated_checkout(tmp_path: Path) -> None:
    source = _android_fixture(tmp_path)
    wrapper = source / "gradle" / "wrapper" / "gradle-wrapper.properties"
    wrapper.write_text(
        wrapper.read_text(encoding="utf-8") + "distributionSha256Sum=" + "b" * 64 + "\n",
        encoding="utf-8",
    )

    def fake_runner(command: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        isolated_wrapper = cwd / "gradle" / "wrapper" / "gradle-wrapper.properties"
        assert "distributionSha256Sum=" + "a" * 64 in isolated_wrapper.read_text(
            encoding="utf-8"
        )
        return _completed(
            command,
            "ASTRAL_RESOLVED_AGP=10.0.0\n"
            "ASTRAL_RESOLVED_GRADLE=10.0.1\n",
        )

    assert driver.run_canary(
        _pins(tmp_path / "pins.properties"),
        source_root=source,
        command_runner=fake_runner,
    )["status"] == "passed"


def test_default_command_runner_maps_gradle_exit_and_preserves_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = ("sh", "./gradlew", "help")
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="BUILD SUCCESSFUL", stderr=None
        ),
    )
    assert driver._default_command_runner(command, tmp_path).stdout == "BUILD SUCCESSFUL"

    monkeypatch.setattr(
        driver.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            command, 1, stdout="failed output", stderr=None
        ),
    )
    with pytest.raises(driver.CanaryExecutionError) as failed:
        driver._default_command_runner(command, tmp_path)
    assert failed.value.code == "gradle_failed"
    assert "failed output" in failed.value.message


@pytest.mark.parametrize(
    ("probe_output", "expected_code"),
    [
        ("BUILD SUCCESSFUL\n", "resolved_version_missing"),
        (
            "ASTRAL_RESOLVED_AGP=10.0.2\n"
            "ASTRAL_RESOLVED_GRADLE=10.0.1\n",
            "resolved_version_mismatch",
        ),
    ],
)
def test_resolved_version_proof_is_exact(
    tmp_path: Path, probe_output: str, expected_code: str
) -> None:
    source = _android_fixture(tmp_path)

    with pytest.raises(driver.CanaryExecutionError) as invalid:
        driver.run_canary(
            _pins(tmp_path / "pins.properties"),
            source_root=source,
            command_runner=lambda command, _cwd: _completed(command, probe_output),
        )

    assert invalid.value.code == expected_code


def test_canary_rejects_missing_temp_parent(tmp_path: Path) -> None:
    source = _android_fixture(tmp_path)
    with pytest.raises(driver.CanaryConfigError) as invalid:
        driver.run_canary(
            _pins(tmp_path / "pins.properties"),
            source_root=source,
            temp_parent=tmp_path / "missing",
        )
    assert invalid.value.code == "temp_parent_missing"


def test_main_reports_unreleased_and_requires_verified_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    properties = _pins(tmp_path / "pins.properties", status="unreleased")
    assert driver.main([str(properties)]) == driver.EX_UNAVAILABLE
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "unavailable"
    assert report["canary_passed"] is False

    assert driver.main([str(properties), "--allow-unreleased"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "code": "unverified_unreleased_override",
        "schema_version": 1,
        "status": "failed",
    }


def test_main_writes_verified_unreleased_report_atomically(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    properties = _pins(tmp_path / "pins.properties", status="unreleased")
    output = tmp_path / "reports" / "canary.json"
    monkeypatch.setattr(
        driver,
        "probe_official_availability",
        lambda _pins: {"agp_major_available": False, "gradle_major_available": False},
    )

    assert (
        driver.main(
            [
                str(properties),
                "--allow-unreleased",
                "--verify-official-availability",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    stdout_report = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == stdout_report
    assert list(output.parent.iterdir()) == [output]


@pytest.mark.parametrize(
    ("failure", "expected_exit", "expected_status"),
    [
        (driver.CanaryUnavailable("not_published", "unavailable"), 69, "unavailable"),
        (driver.CanaryConfigError("bad_config", "invalid"), 2, "failed"),
        (driver.CanaryExecutionError("gradle_failed", "failed"), 1, "failed"),
    ],
)
def test_main_maps_canary_failures_to_stable_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_exit: int,
    expected_status: str,
) -> None:
    properties = _pins(tmp_path / "pins.properties")

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise failure

    monkeypatch.setattr(driver, "run_canary", fail)
    assert driver.main([str(properties)]) == expected_exit
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == expected_status
    assert report["code"] == getattr(failure, "code")


def test_main_emits_success_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    properties = _pins(tmp_path / "pins.properties")
    monkeypatch.setattr(
        driver,
        "run_canary",
        lambda *_args, **_kwargs: {"schema_version": 1, "status": "passed"},
    )

    assert driver.main([str(properties)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
