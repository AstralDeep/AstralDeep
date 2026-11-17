"""Qualifies node pack assembly and failure containment with tiny source and runtime archives.
Only the external download, Git export, Python package and bytecode processes are simulated.
"""

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from node_bundle_support import assemble as ap
from node_bundle_support import manifest as pm


def write(root, name, data=b"fixture", mode=0o644):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    write(runtime, "python/bin/python3.11", mode=0o755)
    for name in ("pip", "helper"):
        write(runtime, f"python/bin/{name}")
    for name in ("pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack"):
        write(runtime, f"python/lib/python3.11/site-packages/{name}/__init__.py")
    write(runtime, "python/lib/python3.11/site-packages/distutils-precedence.pth")
    for name in ("test", "idlelib", "turtledemo", "tkinter", "ensurepip"):
        write(runtime, f"python/lib/python3.11/{name}/payload")
    for name in ("lib/libtcl.dylib", "lib/libtk.dylib", "lib/thread/a", "lib/tcl/a", "lib/tk/a", "lib/itcl/a",
                 "lib/python3.11/lib-dynload/_tkinter.so", "share/a", "include/a",
                 "lib/python3.11/__pycache__/stale.pyc"):
        write(runtime, "python/" + name)
    write(runtime, "python/lib/python3.11/site-packages/pip-24.0.dist-info/METADATA")
    write(runtime, "python/lib/python3.11/site-packages/pipdeptree-1.0.dist-info/METADATA")
    write(runtime, "python/lib/python3.11/site-packages/kept/__init__.py")
    write(runtime, "python/lib/python3.11/site-packages/kept-1.0.dist-info/direct_url.json", b"{}")
    write(runtime, "python/lib/python3.11/site-packages/kept-1.0.dist-info/RECORD",
          b"kept/__init__.py,,\nkept-1.0.dist-info/direct_url.json,,\n../../../bin/pip3,sha256=zz,9\n")
    archive = tmp_path / "python.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(runtime / "python", arcname="python")
    commit = "1" * 40
    settings = {
        "pack_format": 1, "kernel": {"repository": "https://example.invalid/kernel", "commit": commit},
        "deep": {"repository": "https://example.invalid/deep"},
        "platforms": {"macos-arm64": {
            "python": {"version": "3.11.18", "sha256": pm.sha256_file(archive),
                       "url": ap.PYTHON_HOST_PREFIX + "fixture.tar.gz"},
            "node_lock": "node-requirements.macos-arm64.lock",
            "kernel_artifacts": {"kind": "macos-python", "target": "aarch64-apple-darwin",
                                 "manifest": "receipt.json", "wheel_pattern": "astral_kernel-*-macosx_*_arm64.whl"},
        }},
        "backend_exclusions": [{"path": "data"}, {"path": "requirements.txt"}],
        "excluded_packages": [{"name": "forbidden"}], "kept_server_parts": ["orchestrator"],
    }
    components = {key: {"repository": f"https://example.invalid/{key}", "commit": "3" * 40,
                        "path": f"components/{key}"} for key in ap.COMPONENT_BUILD_ORDER}
    kernel = tmp_path / "kernel"
    wheel = write(kernel, "astral_kernel-0.1.0-py3-none-macosx_14_0_arm64.whl")
    write(kernel, "receipt.json", json.dumps({
        "format": ap.KERNEL_ARTIFACTS_FORMAT, "kind": "macos-python", "target": "aarch64-apple-darwin",
        "source_commit": commit, "version": "0.1.0",
        "artifacts": [{"path": wheel.name, "bytes": 7, "sha256": pm.sha256_file(wheel)}],
    }).encode())
    mailbox = write(tmp_path, "mailbox/astral-mailbox-macos-aarch64", mode=0o755)
    monkeypatch.setattr(ap, "load_pins", lambda *a: settings)
    monkeypatch.setattr(ap, "download_python", lambda *a: archive)
    monkeypatch.setattr(ap, "git", lambda *a: "1700000000")

    def export_deep(repo, destination):
        destination.mkdir(parents=True)
        write(destination, "config/astral-composition.json", json.dumps({"components": components}).encode())
        write(destination, "scripts/node_bundle/node-requirements.macos-arm64.lock", b"node-lock")
        write(destination, "scripts/node_bundle/build-requirements.lock", b"build-lock")
        for path in ("backend/orchestrator/orchestrator.py", "backend/data/private.bin", "backend/requirements.txt",
                     "backend/tests/test_x.py", "backend/agents/tests/test_y.py", "backend/__pycache__/stale.pyc",
                     "backend/astral_exec/run.py", "scripts/node/node.py", "pyproject.toml", ".gitmodules",
                     "scripts/node_bundle/launcher/astral_node_launch.py", "scripts/node_bundle/launcher/node_logging.py"):
            write(destination, path)
        return "2" * 40

    def export_component(name, repository, commit, destination, work):
        destination.mkdir(parents=True, exist_ok=True)
        return commit

    monkeypatch.setattr(ap, "export_deep", export_deep)
    monkeypatch.setattr(ap, "export_component", export_component)
    commands = []
    failure = {"step": None}
    installed = []

    def external(command, **kwargs):
        commands.append((command, kwargs.get("env")))
        if failure["step"] is not None and failure["step"] in command:
            raise subprocess.CalledProcessError(1, command)
        if "importlib.metadata" in " ".join(command):
            site = Path(command[0]).parent.parent / "lib" / "python3.11" / "site-packages"
            listing = [info.name[: -len(".dist-info")].rsplit("-", 1) for info in site.glob("*.dist-info")]
            return SimpleNamespace(stdout=json.dumps(sorted(listing + installed)))
        if "wheel" in command:
            write(Path(command[command.index("-w") + 1]), "external-1.0-py3-none-any.whl")
        if "build" in command:
            directory = Path(command[command.index("--wheel-dir") + 1])
            entries = []
            for key, component in components.items():
                built = write(directory, key + "-0.1.0-py3-none-any.whl", key.encode())
                entries.append({"component": key, "wheel": built.name, "wheel_sha256": pm.sha256_file(built),
                                "source_path": component["path"],
                                "source_sha256": hashlib.sha256(key.encode()).hexdigest(),
                                "version": "0.1.0", "distribution": key})
            receipt = {"format": "astraldeep.component-wheel-lock/v1", "installer": "pip-wheel/v1",
                       "components": entries}
            write(directory, "astral-component-wheels.lock.json", json.dumps(receipt).encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ap.subprocess, "run", external)
    return SimpleNamespace(root=tmp_path, kernel=kernel, settings=settings, components=components,
                           commands=commands, failure=failure, installed=installed, mailbox=mailbox)


def build(assembly, name="out", work="work", mailbox=None):
    return ap.assemble("macos-arm64", assembly.root / name, assembly.kernel, assembly.root / work, mailbox)


def test_assembly_seals_only_verified_runtime_and_source_inputs(assembly):
    output, work = assembly.root / "out", assembly.root / "work"
    descriptor = json.loads(build(assembly, mailbox=assembly.mailbox).read_text())
    manifest = json.loads((output / "node-pack-manifest.json").read_text())
    archive = output / descriptor["archive"]["file"]
    assert descriptor["archive"]["sha256"] == pm.sha256_file(archive)
    assert descriptor["manifest_sha256"] == pm.sha256_file(output / "node-pack-manifest.json")
    assert manifest["files"] and manifest["deep"]["commit"] == "2" * 40
    assert set(manifest["components"]) == set(ap.COMPONENT_BUILD_ORDER)
    assert manifest["installed_distributions"] == [{"name": "kept", "version": "1.0"},
                                                   {"name": "pipdeptree", "version": "1.0"}]
    assert manifest["kernel"]["commit"] == assembly.settings["kernel"]["commit"]
    assert manifest["kernel"]["mailbox"]["sha256"] == pm.sha256_file(assembly.mailbox)
    assert manifest["version"] == "3.11.18-22222222-1111111"
    with tarfile.open(archive) as bundle:
        names = bundle.getnames()
    assert "app/backend/orchestrator/orchestrator.py" in names and "app/backend/astral_exec/run.py" in names
    assert "bin/astral-mailbox" in names and "launcher/astral_node_launch.py" in names
    assert not any("/tests/" in path or "/data/" in path or "__pycache__" in path for path in names)
    site = "python/lib/python3.11/site-packages/"
    assert not any(path.startswith((site + "pip/", site + "pip-")) for path in names)
    assert site + "pipdeptree-1.0.dist-info/METADATA" in names
    assert not any(path.endswith("direct_url.json") for path in names)
    assert "python/bin/helper" not in names
    record = (work / "stage" / "python/lib/python3.11/site-packages/kept-1.0.dist-info/RECORD").read_text()
    assert record == "kept/__init__.py,,\n"
    commands = [command for command, _ in assembly.commands]
    build_step = next(i for i, command in enumerate(commands) if "build" in command)
    install = next(i for i, command in enumerate(commands) if "install" in command and "--lock" in command)
    verify = next(i for i, command in enumerate(commands) if "verify" in command and "--lock" in command)
    bake = next(i for i, command in enumerate(commands) if "compileall" in command)
    assert build_step < install < verify < bake
    assert "checked-hash" in commands[bake] and str(work / "stage" / "launcher") in map(str, commands[bake])
    assert all("--require-hashes" in command for command in commands if "pip" in command and "--no-index" not in command
               and "ensurepip" not in command)
    assert all(env and env["SOURCE_DATE_EPOCH"] == "1700000000" for command, env in assembly.commands
               if "compileall" not in command and "importlib.metadata" not in " ".join(command))


def test_the_pack_has_no_mailbox_unless_one_is_supplied(assembly):
    descriptor = json.loads(build(assembly).read_text())
    manifest = json.loads((assembly.root / "out" / "node-pack-manifest.json").read_text())
    assert manifest["kernel"]["mailbox"] is None
    with tarfile.open(assembly.root / "out" / descriptor["archive"]["file"]) as bundle:
        assert "bin/astral-mailbox" not in bundle.getnames()


def test_two_assemblies_from_the_same_inputs_give_the_same_manifest_and_archive(assembly):
    first = json.loads(build(assembly, "one", "work-one", assembly.mailbox).read_text())
    second = json.loads(build(assembly, "two", "work-two", assembly.mailbox).read_text())
    assert first == second
    assert (assembly.root / "one" / "node-pack-manifest.json").read_bytes() == (
        assembly.root / "two" / "node-pack-manifest.json").read_bytes()
    assert first["archive"]["sha256"] == second["archive"]["sha256"]


def test_failed_component_verification_never_produces_a_candidate(assembly):
    assembly.failure["step"] = "verify"
    with pytest.raises(subprocess.CalledProcessError):
        build(assembly)
    assert not (assembly.root / "out").exists()
    assert not any("compileall" in command for command, _ in assembly.commands)


def test_assembler_cli_reports_the_sealed_artifact_and_its_failures(assembly, capsys, monkeypatch):
    monkeypatch.delenv(ap.KERNEL_ARTIFACTS_ENV, raising=False)
    arguments = ["--out", str(assembly.root / "out"), "--work", str(assembly.root / "work")]
    with pytest.raises(SystemExit, match="--kernel-dir"):
        ap.main(arguments)
    ap.main([*arguments, "--kernel-dir", str(assembly.kernel)])
    assert "archive " in capsys.readouterr().out
    assembly.failure["step"] = "verify"
    with pytest.raises(SystemExit, match="node pack"):
        ap.main([*arguments, "--kernel-dir", str(assembly.kernel)])


def test_the_kernel_directory_may_come_from_the_environment(assembly, capsys, monkeypatch):
    monkeypatch.setenv(ap.KERNEL_ARTIFACTS_ENV, str(assembly.kernel))
    ap.main(["--out", str(assembly.root / "out"), "--work", str(assembly.root / "work")])
    assert "archive " in capsys.readouterr().out


def test_a_platform_without_pins_is_refused_before_any_work(assembly):
    with pytest.raises(ap.PackError, match="not built yet"):
        ap.assemble("linux-x86_64", assembly.root / "out", assembly.kernel, assembly.root / "work")
    assert not assembly.commands


def test_a_missing_mailbox_file_is_refused(assembly):
    with pytest.raises(ap.PackError, match="not a file"):
        build(assembly, mailbox=assembly.root / "absent")


def test_kernel_artifacts_from_another_commit_are_refused(assembly):
    assembly.settings["kernel"]["commit"] = "9" * 40
    with pytest.raises(ap.PackError, match="pins.json"):
        build(assembly)


def test_python_download_stream_is_digest_checked_before_cache_promotion(tmp_path, monkeypatch):
    import io

    payload = b"tiny runtime archive"
    calls = []

    def response(url, timeout):
        calls.append((url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(ap.urllib.request, "urlopen", response)
    pin = {"url": ap.PYTHON_HOST_PREFIX + "fixture.tar.gz", "sha256": hashlib.sha256(payload).hexdigest()}
    result = ap.download_python(pin, tmp_path / "downloads")
    assert result.read_bytes() == payload and calls == [(pin["url"], 120)]
    assert not list(result.parent.glob("*.part"))


def test_assembly_refuses_an_excluded_installed_package_before_sealing(assembly):
    assembly.installed[:] = [["forbidden", "1.0"]]
    with pytest.raises(ap.PackError, match="excluded packages"):
        build(assembly)
    assert not (assembly.root / "out").exists()


def test_assembly_refuses_a_pack_that_carries_a_build_path(assembly, monkeypatch):
    original = ap.bake_bytecode

    def leaky(python, stage):
        original(python, stage)
        write(stage, "app/backend/leak.txt", str(assembly.root / "work").encode())

    monkeypatch.setattr(ap, "bake_bytecode", leaky)
    with pytest.raises(ap.PackError, match="leak.txt"):
        build(assembly)
    assert not (assembly.root / "out").exists()


def natives_folder(root):
    folder = root / "natives-folder"
    write(folder, "bin/pdftoppm", b"tool", mode=0o755)
    write(folder, "lib/libmagic.1.dylib")
    (folder / "lib" / "libmagic.dylib").symlink_to("libmagic.1.dylib")
    return folder


@pytest.fixture
def natives_assembly(assembly, monkeypatch):
    folder = natives_folder(assembly.root)
    assembly.settings["platforms"]["macos-arm64"]["natives"] = {"sources_lock": "natives/sources.lock.json"}
    assembly.settings["spacy_model"] = {"name": "en_core_web_lg", "url": "https://example.invalid/model.whl",
                                        "size": 5, "sha256": "0" * 64}
    calls = []
    module = SimpleNamespace(
        build=lambda lock, out, cache, work: calls.append(("build", lock.name, out.name)) or folder,
        require_receipt_for=lambda natives, lock: calls.append(("receipt", natives.name, lock.name)),
        summary=lambda natives: {"lock_sha256": "f" * 64},
    )
    monkeypatch.setattr(ap, "natives_module", lambda: module)
    monkeypatch.setattr(ap, "fetch_pinned", lambda pin, cache: cache / "model.whl")
    assembly.natives, assembly.calls = folder, calls
    return assembly


def test_a_pack_with_natives_builds_them_stages_them_beside_the_mailbox_and_installs_the_model(natives_assembly):
    descriptor = build(natives_assembly, mailbox=natives_assembly.mailbox)
    manifest = json.loads((natives_assembly.root / "out" / "node-pack-manifest.json").read_text())
    assert natives_assembly.calls == [("build", "sources.lock.json", "natives")]
    paths = {entry["path"] for entry in manifest["files"]}
    assert {"bin/pdftoppm", "bin/astral-mailbox", "lib/libmagic.1.dylib", "lib/libmagic.dylib"} <= paths
    assert manifest["natives"] == {"lock_sha256": "f" * 64}
    assert manifest["spacy_model"]["name"] == "en_core_web_lg"
    installs = [command for command, _ in natives_assembly.commands if command[-1].endswith("model.whl")]
    assert len(installs) == 1 and {"--no-index", "--no-deps"} <= set(installs[0])
    assert descriptor.is_file()


def test_prebuilt_natives_must_match_the_recipe_lock(natives_assembly):
    ap.assemble("macos-arm64", natives_assembly.root / "out", natives_assembly.kernel, natives_assembly.root / "work",
                natives=natives_assembly.natives)
    assert natives_assembly.calls == [("receipt", "natives-folder", "sources.lock.json")]


def test_natives_that_fail_their_receipt_stop_the_assembly(natives_assembly, monkeypatch):
    def stale(natives, lock):
        raise ap.PackError("the natives folder was built from a different sources lock")

    monkeypatch.setattr(ap, "natives_module", lambda: SimpleNamespace(require_receipt_for=stale))
    with pytest.raises(ap.PackError, match="different sources lock"):
        ap.assemble("macos-arm64", natives_assembly.root / "out", natives_assembly.kernel,
                    natives_assembly.root / "work", natives=natives_assembly.natives)
    assert not (natives_assembly.root / "out").exists()


def test_the_cli_takes_a_natives_folder(natives_assembly, capsys):
    ap.main(["--out", str(natives_assembly.root / "out"), "--work", str(natives_assembly.root / "work"),
             "--kernel-dir", str(natives_assembly.kernel), "--natives", str(natives_assembly.natives)])
    assert "archive " in capsys.readouterr().out
    assert natives_assembly.calls[0][0] == "receipt"


def test_the_real_natives_module_loads_from_the_bundle_directory():
    assert ap.natives_module().__name__.endswith("build_macos")
