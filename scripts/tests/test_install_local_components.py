"""Focused tests for exact local component wheel installation."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "install_local_components.py"

spec = importlib.util.spec_from_file_location("install_local_components_074", SCRIPT)
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)


def _write(path: Path, content: str = "fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


def _component(key: str, distribution: str, import_name: str) -> Any:
    return installer.ComponentSpec(
        key=key,
        distribution=distribution,
        version="1.2.3",
        relative_path=f"components/{key}",
        contract=f"{key}.contract/v1",
        availability="required-embedded",
        import_name=import_name,
        extras=(),
        build_inputs=("pyproject.toml", f"src/{import_name}"),
        required_wheel_paths=(f"{import_name}/__init__.py",),
    )


def _contract(tmp_path: Path) -> Any:
    components = (
        _component("dependency", "dependency-package", "dependency_package"),
        _component("consumer", "consumer-package", "consumer_package"),
    )
    for component in components:
        root = tmp_path / component.relative_path
        dependencies = (
            "['dependency-package==1.2.3']"
            if component.key == "consumer"
            else "[]"
        )
        _write(
            root / "pyproject.toml",
            "[project]\n"
            f"name = '{component.distribution}'\n"
            f"version = '{component.version}'\n"
            f"dependencies = {dependencies}\n",
        )
        _write(root / f"src/{component.import_name}/__init__.py")
    _write(tmp_path / "manifest.json", "{}\n")
    return installer.LocalContract(
        repository_root=tmp_path,
        manifest_path=tmp_path / "manifest.json",
        components=components,
    )


def _fake_wheel(path: Path, component: Any, *, include_required: bool = True) -> None:
    dist_info = (
        f"{component.distribution.replace('-', '_')}-{component.version}.dist-info"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.3\n"
            f"Name: {component.distribution}\n"
            f"Version: {component.version}\n",
        )
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: feature-074-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        if include_required:
            for required in component.required_wheel_paths:
                wheel.writestr(required, "__all__ = []\n")


def test_real_contract_declarations_are_exact_without_private_checkout() -> None:
    contract = installer.load_contract(
        REPOSITORY_ROOT,
        require_sources=False,
        require_gitlinks=True,
    )
    assert [component.key for component in contract.components] == [
        "astral-primitives",
        "astral-projection",
        "astral-plane",
        "lets",
    ]
    assert all(component.build_inputs for component in contract.components)
    assert all(component.required_wheel_paths for component in contract.components)


def test_real_initialized_sources_match_the_declarations() -> None:
    if not all(
        (REPOSITORY_ROOT / relative / "pyproject.toml").is_file()
        for relative in (
            "components/AstralProjection",
            "components/AstralPlane",
            "components/AstralPrimitives",
            "components/LETS",
        )
    ):
        pytest.skip("private component worktrees are intentionally uninitialized")
    contract = installer.load_contract(
        REPOSITORY_ROOT,
        require_sources=True,
        require_gitlinks=True,
    )
    assert len(contract.components) == 4


def test_build_and_install_use_offline_no_resolver_commands_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract(tmp_path)
    installer._validate_component_sources(contract)
    wheel_directory = tmp_path / "wheels"
    lock_path = wheel_directory / "astral-component-wheels.lock.json"
    build_commands: list[tuple[str, ...]] = []

    def fake_build(arguments: Any, *, cwd: Path) -> None:
        command = tuple(arguments)
        build_commands.append(command)
        assert cwd == tmp_path
        component_root = Path(command[-1])
        component = next(
            item for item in contract.components if item.key == component_root.name
        )
        output = Path(command[command.index("--wheel-dir") + 1])
        wheel_name = (
            f"{component.distribution.replace('-', '_')}-{component.version}"
            "-py3-none-any.whl"
        )
        _fake_wheel(output / wheel_name, component)

    monkeypatch.setattr(installer, "_run", fake_build)
    installer.build_wheels(contract, wheel_directory, lock_path)

    assert [Path(command[-1]).name for command in build_commands] == [
        "dependency",
        "consumer",
    ]
    for command in build_commands:
        assert command[2:4] == ("pip", "wheel")
        assert "--no-deps" in command
        assert "--no-index" in command
        assert "--no-build-isolation" in command
        assert "--no-cache-dir" in command

    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["format"] == installer.LOCK_FORMAT
    assert lock["manifest_sha256"] == installer._sha256(contract.manifest_path)
    assert [entry["component"] for entry in lock["components"]] == [
        "dependency",
        "consumer",
    ]
    assert all(len(entry["source_sha256"]) == 64 for entry in lock["components"])
    assert all(len(entry["wheel_sha256"]) == 64 for entry in lock["components"])

    install_commands: list[tuple[str, ...]] = []

    def fake_install(arguments: Any, *, cwd: Path) -> None:
        assert cwd == tmp_path
        install_commands.append(tuple(arguments))

    monkeypatch.setattr(installer, "_run", fake_install)
    installer.install_wheels(contract, lock_path)
    assert [Path(command[-1]).name for command in install_commands] == [
        lock["components"][0]["wheel"],
        lock["components"][1]["wheel"],
    ]
    for command in install_commands:
        assert command[2:4] == ("pip", "install")
        assert "--no-deps" in command
        assert "--no-index" in command
        assert "--force-reinstall" in command

    reversed_contract = installer.LocalContract(
        contract.repository_root,
        contract.manifest_path,
        tuple(reversed(contract.components)),
    )
    with pytest.raises(installer.ComponentInstallError, match="before local dependency"):
        installer._validate_component_sources(reversed_contract)


def test_changed_or_incomplete_wheel_is_rejected_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract(tmp_path)
    wheel_directory = tmp_path / "wheels"
    lock_path = wheel_directory / "astral-component-wheels.lock.json"

    def fake_build(arguments: Any, *, cwd: Path) -> None:
        del cwd
        command = tuple(arguments)
        component = next(
            item for item in contract.components if item.key == Path(command[-1]).name
        )
        output = Path(command[command.index("--wheel-dir") + 1])
        _fake_wheel(
            output
            / f"{component.distribution}-{component.version}-py3-none-any.whl",
            component,
        )

    monkeypatch.setattr(installer, "_run", fake_build)
    installer.build_wheels(contract, wheel_directory, lock_path)
    first_wheel = wheel_directory / json.loads(lock_path.read_text("utf-8"))["components"][0][
        "wheel"
    ]
    with first_wheel.open("ab") as stream:
        stream.write(b"tampered")

    calls: list[Any] = []
    monkeypatch.setattr(installer, "_run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(installer.ComponentInstallError, match="does not match the lock"):
        installer.install_wheels(contract, lock_path)
    assert calls == []

    missing = tmp_path / "missing.whl"
    _fake_wheel(missing, contract.components[0], include_required=False)
    with pytest.raises(installer.ComponentInstallError, match="missing required package data"):
        installer._validate_wheel(missing, contract.components[0])


def test_pip_environment_cannot_inherit_index_or_find_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_FIND_LINKS",
        "PIP_TRUSTED_HOST",
        "UV_INDEX_URL",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "https://credentials.invalid/example")
    environment = installer._pip_environment()
    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert environment["PYTHONHASHSEED"] == "0"
    assert environment["SOURCE_DATE_EPOCH"] == "315532800"
    assert environment["TZ"] == "UTC"
    assert environment["PIP_CONFIG_FILE"]
    assert all("credentials.invalid" not in value for value in environment.values())


def test_source_digest_refuses_sensitive_build_input_without_reading_it(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)
    sensitive = (
        tmp_path
        / contract.components[0].relative_path
        / f"src/{contract.components[0].import_name}/.env"
    )
    _write(sensitive, "SECRET=must-not-be-consumed\n")
    with pytest.raises(installer.ComponentInstallError, match="sensitive/runtime"):
        installer.source_digest(contract.components[0], tmp_path)


def test_installed_archive_and_required_record_are_digest_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component = _component("demo", "demo-package", "demo_package")
    contract = installer.LocalContract(tmp_path, tmp_path / "manifest.json", (component,))
    _write(contract.manifest_path, "{}\n")
    wheel_digest = "a" * 64
    lock_path = tmp_path / "astral-component-wheels.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "format": installer.LOCK_FORMAT,
                "installer": installer.INSTALLER_FORMAT,
                "manifest_sha256": installer._sha256(contract.manifest_path),
                "components": [
                    {
                        "component": component.key,
                        "distribution": component.distribution,
                        "version": component.version,
                        "source_path": component.relative_path,
                        "source_sha256": "b" * 64,
                        "wheel": "demo_package-1.2.3-py3-none-any.whl",
                        "wheel_sha256": wheel_digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    site = tmp_path / "site"
    package = site / "demo_package/__init__.py"
    _write(package, "VALUE = 1\n")
    dist_info = site / "demo_package-1.2.3.dist-info"
    _write(
        dist_info / "METADATA",
        "Metadata-Version: 2.3\nName: demo-package\nVersion: 1.2.3\n",
    )
    _write(
        dist_info / "direct_url.json",
        json.dumps(
            {
                "archive_info": {"hashes": {"sha256": wheel_digest}},
                "url": "file:///tmp/demo_package-1.2.3-py3-none-any.whl",
            }
        ),
    )
    record_hash = installer._record_digest(package)
    _write(
        dist_info / "RECORD",
        f"demo_package/__init__.py,sha256={record_hash},{package.stat().st_size}\n",
    )
    distribution = importlib.metadata.PathDistribution(dist_info)
    monkeypatch.setattr(
        installer.importlib.metadata,
        "distribution",
        lambda _name: distribution,
    )
    monkeypatch.setattr(installer, "_verify_import_origin", lambda _component: None)

    installer.verify_install(contract, lock_path)
    _write(
        dist_info / "direct_url.json",
        json.dumps(
            {
                "archive_info": {"hashes": {"sha256": "c" * 64}},
                "url": "file:///tmp/demo_package-1.2.3-py3-none-any.whl",
            }
        ),
    )
    with pytest.raises(installer.ComponentInstallError, match="does not match the lock"):
        installer.verify_install(contract, lock_path)


def _minimal_declaration(tmp_path: Path, *, installer_name: str = "pip-wheel/v1") -> None:
    _write(
        tmp_path / "pyproject.toml",
        "[project]\n"
        "name = 'fixture'\n"
        "version = '0.1.0'\n"
        "dependencies = []\n"
        "[tool.astraldeep.local-components]\n"
        "format = 'astraldeep.local-components/v1'\n"
        "manifest = 'config/astral-composition.json'\n"
        f"installer = '{installer_name}'\n"
        "wheel-lock-format = 'astraldeep.component-wheel-lock/v1'\n"
        "install-order = ['demo']\n"
        "build-tools = ['setuptools==80.9.0', 'wheel==0.45.1', "
        "'hatchling==1.27.0', 'uv_build==0.11.21']\n"
        "[tool.astraldeep.local-components.demo]\n"
        "distribution = 'demo-package'\n"
        "version = '1.2.3'\n"
        "path = 'components/Demo'\n"
        "contract = 'demo.contract/v1'\n"
        "availability = 'required-embedded'\n"
        "import = 'demo_package'\n"
        "extras = []\n"
        "build-inputs = ['pyproject.toml', 'src/demo_package']\n"
        "required-wheel-paths = ['demo_package/__init__.py']\n",
    )
    _write(
        tmp_path / ".gitmodules",
        "[submodule \"Demo\"]\n"
        "\tpath = components/Demo\n"
        "\turl = https://github.com/AstralDeep/Demo.git\n",
    )
    _write(
        tmp_path / "config/astral-composition.json",
        json.dumps(
            {
                "components": {
                    "demo": {
                        "path": "components/Demo",
                        "repository": "https://github.com/AstralDeep/Demo.git",
                        "commit": "0" * 40,
                        "contract_version": "demo.contract/v1",
                    }
                },
                "availability": {"demo": "required-embedded"},
            }
        ),
    )


def test_contract_mutations_fail_closed(tmp_path: Path) -> None:
    _minimal_declaration(tmp_path, installer_name="resolver/v1")
    with pytest.raises(installer.ComponentInstallError, match="installer"):
        installer.load_contract(tmp_path, require_sources=False)

    metadata = installer._read_toml(REPOSITORY_ROOT / "pyproject.toml")
    projection = metadata["tool"]["astraldeep"]["local-components"][
        "astral-projection"
    ].copy()
    projection["path"] = "../AstralProjection"
    with pytest.raises(installer.ComponentInstallError, match="relative POSIX path"):
        installer._component_spec("astral-projection", projection)

    with pytest.raises(installer.ComponentInstallError, match="duplicate"):
        installer._string_list(["demo", "demo"], field="install-order")


@pytest.mark.parametrize(
    "value",
    ("", "../escape", "/absolute", "C:/drive", "back\\slash", "line\nbreak"),
)
def test_relative_path_validation_rejects_ambiguous_values(value: str) -> None:
    with pytest.raises(installer.ComponentInstallError):
        installer._safe_relative_path(value, field="fixture")


def test_gitlink_and_command_failures_are_typed_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="100644 deadbeef 0\tcomponents/Demo\n", stderr=""
        ),
    )
    with pytest.raises(installer.ComponentInstallError, match="stage-0 gitlink"):
        installer._gitlink(tmp_path, "components/Demo")

    observed: dict[str, Any] = {}

    def successful_run(*args: Any, **kwargs: Any) -> Any:
        observed["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

    monkeypatch.setattr(installer.subprocess, "run", successful_run)
    installer._run((sys.executable, "-m", "pip", "check"), cwd=tmp_path)
    assert observed["environment"]["PIP_NO_INDEX"] == "1"
    assert "PIP_INDEX_URL" not in observed["environment"]

    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 9, stdout="", stderr="bounded failure"
        ),
    )
    with pytest.raises(installer.ComponentInstallError, match="bounded failure"):
        installer._run((sys.executable, "-m", "pip", "check"), cwd=tmp_path)

    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private detail")),
    )
    with pytest.raises(installer.ComponentInstallError, match="could not execute") as error:
        installer._run((sys.executable, "-m", "pip", "check"), cwd=tmp_path)
    assert "private detail" not in str(error.value)


def test_gitmodules_rejects_floating_or_extra_configuration(tmp_path: Path) -> None:
    path = tmp_path / ".gitmodules"
    _write(
        path,
        "[submodule \"Demo\"]\n"
        "path = components/Demo\n"
        "url = https://github.com/AstralDeep/Demo.git\n"
        "branch = main\n",
    )
    with pytest.raises(installer.ComponentInstallError, match="floating branch"):
        installer._parse_gitmodules(path)
    _write(
        path,
        "[submodule \"Demo\"]\n"
        "path = components/Demo\n"
        "url = https://github.com/AstralDeep/Demo.git\n"
        "update = checkout\n",
    )
    with pytest.raises(installer.ComponentInstallError, match="only path and url"):
        installer._parse_gitmodules(path)


def test_lock_rejects_manifest_and_component_order_tampering(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    lock_path = tmp_path / "lock.json"
    entries = [
        {
            "component": component.key,
            "distribution": component.distribution,
            "version": component.version,
            "source_path": component.relative_path,
            "source_sha256": "1" * 64,
            "wheel": f"{component.key}-1.2.3-py3-none-any.whl",
            "wheel_sha256": "2" * 64,
        }
        for component in contract.components
    ]
    document = installer._lock_document(contract, entries)
    lock_path.write_text(json.dumps(document), encoding="utf-8")
    assert len(installer._load_lock(contract, lock_path)) == 2

    document["manifest_sha256"] = "3" * 64
    lock_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(installer.ComponentInstallError, match="manifest digest"):
        installer._load_lock(contract, lock_path)

    document = installer._lock_document(contract, list(reversed(entries)))
    lock_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(installer.ComponentInstallError, match="does not match the contract"):
        installer._load_lock(contract, lock_path)


def test_main_routes_commands_and_reports_typed_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = _contract(tmp_path)
    lock = tmp_path / "lock.json"
    _write(lock, "{}\n")
    calls: list[str] = []
    monkeypatch.setattr(installer, "load_contract", lambda *args, **kwargs: contract)
    monkeypatch.setattr(
        installer,
        "build_wheels",
        lambda *args, **kwargs: calls.append("build"),
    )
    monkeypatch.setattr(
        installer,
        "install_wheels",
        lambda *args, **kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        installer,
        "verify_install",
        lambda *args, **kwargs: calls.append("verify"),
    )
    monkeypatch.setattr(
        installer,
        "sync_components",
        lambda *args, **kwargs: calls.append("sync"),
    )

    assert installer.main(["validate", "--root", str(tmp_path)]) == 0
    assert (
        installer.main(
            [
                "build",
                "--root",
                str(tmp_path),
                "--wheel-dir",
                str(tmp_path / "wheels"),
            ]
        )
        == 0
    )
    assert installer.main(["install", "--root", str(tmp_path), "--lock", str(lock)]) == 0
    assert installer.main(["verify", "--root", str(tmp_path), "--lock", str(lock)]) == 0
    assert installer.main(["sync", "--root", str(tmp_path)]) == 0
    assert calls == ["build", "install", "verify", "sync"]
    assert "exact local" in capsys.readouterr().out

    monkeypatch.setattr(
        installer,
        "load_contract",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            installer.ComponentInstallError("typed failure")
        ),
    )
    assert installer.main(["validate", "--root", str(tmp_path)]) == 2
    assert "typed failure" in capsys.readouterr().err
