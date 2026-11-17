"""Checks the assembler's refusals: a dirty or moved source, an unofficial or altered Python download, mismatched kernel artifacts, excluded packages that slipped in, and the prune and bytecode steps."""

import hashlib
import json
import subprocess
import sys

import node_bundle_support  # noqa: F401
import assemble as ap
import pytest


def repository(path, files=None):
    path.mkdir()
    subprocess.run(["git", "init", "--quiet", path], check=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "Pack Test"], check=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "pack@example.test"], check=True)
    for name, text in (files or {"input.txt": "pinned source\n"}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", path, "commit", "--quiet", "-m", "Source"], check=True)
    return subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True).strip()


@pytest.mark.parametrize("dirty", ["tracked", "untracked"])
def test_deep_export_refuses_a_dirty_tree(tmp_path, dirty):
    source = tmp_path / "source"
    repository(source)
    (source / ("input.txt" if dirty == "tracked" else "untracked.txt")).write_text("not committed")
    with pytest.raises(ap.PackError, match="clean"):
        ap.export_deep(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_deep_export_takes_the_committed_bytes_and_no_git_metadata(tmp_path):
    source = tmp_path / "source"
    commit = repository(source)
    assert ap.export_deep(source, tmp_path / "out") == commit
    assert (tmp_path / "out" / "input.txt").read_text() == "pinned source\n"
    assert not (tmp_path / "out" / ".git").exists()


def test_deep_export_refuses_a_path_that_is_not_the_checkout_root(tmp_path):
    source = tmp_path / "source"
    repository(source, {"nested/input.txt": "x"})
    with pytest.raises(ap.PackError, match="checkout root"):
        ap.export_deep(source / "nested", tmp_path / "out")


def test_component_export_refuses_a_dirty_override_and_a_checkout_at_another_revision(tmp_path, monkeypatch):
    source = tmp_path / "source"
    commit = repository(source)
    monkeypatch.setenv("NODE_BUNDLE_SOURCE_ASTRALPLANE", str(source))
    (source / "input.txt").write_text("changed")
    with pytest.raises(ap.PackError, match="clean"):
        ap.export_component("AstralPlane", "https://example.test/repo", commit, tmp_path / "dirty", tmp_path / "work")
    subprocess.run(["git", "-C", source, "commit", "--quiet", "-am", "Next"], check=True)
    with pytest.raises(ap.PackError, match="expected"):
        ap.export_component("AstralPlane", "https://example.test/repo", commit, tmp_path / "old", tmp_path / "work")


def test_component_export_refuses_an_abbreviated_pin(tmp_path):
    with pytest.raises(ap.PackError, match="full"):
        ap.export_component("AstralPlane", "https://example.test/repo", "abc1234", tmp_path / "out", tmp_path / "work")


def test_component_export_from_an_explicit_clean_checkout_keeps_the_committed_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    commit = repository(source)
    monkeypatch.setenv("NODE_BUNDLE_SOURCE_ASTRALPLANE", str(source))
    assert ap.export_component("AstralPlane", str(tmp_path / "unused"), commit, tmp_path / "out", tmp_path / "w") == commit
    assert (tmp_path / "out" / "input.txt").read_text() == "pinned source\n"


def test_component_export_uses_a_private_mirror_by_default(tmp_path):
    source = tmp_path / "source"
    commit = repository(source)
    assert ap.export_component("default", str(source), commit, tmp_path / "out", tmp_path / "work") == commit
    assert (tmp_path / "work" / "sources" / "default.git").is_dir()
    assert (tmp_path / "out" / "input.txt").read_text() == "pinned source\n"


def test_a_commit_the_mirror_does_not_hold_is_fetched_or_refused(tmp_path):
    source = tmp_path / "source"
    repository(source)
    with pytest.raises(ap.PackError, match="could not be exported"):
        ap.export_component("default", str(source), "f" * 40, tmp_path / "out", tmp_path / "work")


def test_python_must_come_from_the_official_release_host(tmp_path):
    with pytest.raises(ap.PackError, match="python-build-standalone"):
        ap.download_python({"url": "https://example.com/py.tar.gz", "sha256": "0" * 64}, tmp_path)


def test_a_cached_python_archive_with_the_wrong_digest_is_deleted_and_refused(tmp_path):
    cached = tmp_path / "cpython-test.tar.gz"
    cached.write_bytes(b"tampered")
    with pytest.raises(ap.PackError, match="SHA-256"):
        ap.download_python({"url": ap.PYTHON_HOST_PREFIX + "20261110/cpython-test.tar.gz", "sha256": "0" * 64}, tmp_path)
    assert not cached.exists()


def test_a_cached_python_archive_with_the_pinned_digest_is_used_without_a_download(tmp_path):
    cached = tmp_path / "cpython+test.tar.gz"
    cached.write_bytes(b"genuine")
    pin = {"url": ap.PYTHON_HOST_PREFIX + "20261110/cpython%2Btest.tar.gz", "sha256": hashlib.sha256(b"genuine").hexdigest()}
    assert ap.download_python(pin, tmp_path) == cached


def test_present_excluded_packages_are_refused_with_normalised_names():
    installed = [{"name": "SimpleITK", "version": "1"}, {"name": "python_magic", "version": "1"}]
    excluded = [{"name": "simpleitk"}, {"name": "python-magic"}, {"name": "spacy"}]
    with pytest.raises(ap.PackError, match="SimpleITK|simpleitk") as raised:
        ap.verify_exclusions(installed, excluded)
    assert "spacy" not in str(raised.value)
    ap.verify_exclusions([{"name": "numpy", "version": "1"}], excluded)


def test_backend_prune_removes_exclusions_nested_tests_and_bytecode_and_keeps_astral_exec(tmp_path):
    backend = tmp_path / "backend"
    for path in ("voice_agent/x.py", "tests/t.py", "agents/general/tests/t.py", "agents/general/a.py",
                 "orchestrator/__pycache__/x.pyc", "orchestrator/o.py", "astral_exec/run.py",
                 "astral_exec/conformance/cases.py"):
        (backend / path).parent.mkdir(parents=True, exist_ok=True)
        (backend / path).write_text("x")
    (backend / "requirements.txt").write_text("x")
    ap.prune_backend(backend, [{"path": "voice_agent"}, {"path": "requirements.txt"}])
    remaining = sorted(p.relative_to(backend).as_posix() for p in backend.rglob("*") if p.is_file())
    assert remaining == ["agents/general/a.py", "astral_exec/conformance/cases.py", "astral_exec/run.py",
                         "orchestrator/o.py"]


def test_install_origins_are_removed_and_dropped_from_the_record(tmp_path):
    dist = tmp_path / "pkg-1.0.dist-info"
    dist.mkdir()
    (dist / "direct_url.json").write_text('{"url": "file:///work/pkg.whl"}')
    (dist / "RECORD").write_text("pkg/__init__.py,sha256=x,1\npkg-1.0.dist-info/direct_url.json,sha256=y,2\n")
    (tmp_path / "plain-1.0.dist-info").mkdir()
    ap.strip_install_origins(tmp_path)
    assert not (dist / "direct_url.json").exists()
    assert (dist / "RECORD").read_text() == "pkg/__init__.py,sha256=x,1\n"


def test_every_launcher_module_is_copied_into_the_pack(tmp_path):
    ap.copy_launcher(tmp_path, ap.HERE / "launcher")
    copied = sorted(path.name for path in (tmp_path / "launcher").iterdir())
    assert copied == sorted(path.name for path in (ap.HERE / "launcher").glob("*.py"))
    assert "node_logging.py" in copied and "astral_node_launch.py" in copied


def test_a_pack_that_names_the_build_directory_is_refused(tmp_path):
    stage, work = tmp_path / "stage", tmp_path / "work"
    stage.mkdir()
    (stage / "clean.py").write_text("print(1)\n")
    ap.scan_for_leaks(stage, [work])
    (stage / "leaky.pyc").write_bytes(b"\0" * 5000000 + str(work).encode() + b"/x")
    with pytest.raises(ap.PackError, match="leaky.pyc"):
        ap.scan_for_leaks(stage, [work])


def test_a_path_split_across_read_blocks_is_still_found(tmp_path):
    stage, work = tmp_path / "stage", tmp_path / "work"
    stage.mkdir()
    needle = str(work).encode()
    (stage / "edge.bin").write_bytes(b"\0" * ((1 << 20) - 3) + needle)
    with pytest.raises(ap.PackError, match="edge.bin"):
        ap.scan_for_leaks(stage, [work])


def test_baked_bytecode_is_hash_checked_relocatable_and_leaves_no_cache_on_a_no_write_run(tmp_path):
    stage = tmp_path / "stage"
    for root in ("python/lib/python3.11", "app/backend", "app/scripts", "launcher"):
        (stage / root).mkdir(parents=True)
        (stage / root / "mod.py").write_text("VALUE = 1\n")
    ap.bake_bytecode(sys.executable, stage)
    caches = sorted(stage.rglob("*.pyc"))
    assert len(caches) == 4
    for cache in caches:
        flags = int.from_bytes(cache.read_bytes()[4:8], "little")
        assert flags == 0b11
        assert str(stage).encode() not in cache.read_bytes()
        assert ap.BYTECODE_PREFIX.encode() in cache.read_bytes()
    (stage / "app" / "backend" / "extra.py").write_text("OTHER = 2\n")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import mod, extra",
         str(stage / "app" / "backend")], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not (stage / "app" / "backend" / "__pycache__" / "extra.cpython-311.pyc").exists() or sys.version_info < (3, 11)
    assert len(sorted(stage.rglob("*.pyc"))) == 4


def test_kernel_receipts_are_checked_for_commit_kind_target_and_wheel_digest(tmp_path):
    pins = ap.load_pins()
    spec = pins["platforms"]["macos-arm64"]["kernel_artifacts"]
    wheel = tmp_path / "astral_kernel-0.2.0-py3-none-macosx_14_0_arm64.whl"
    wheel.write_bytes(b"wheel")
    receipt = {
        "format": ap.KERNEL_ARTIFACTS_FORMAT, "kind": "macos-python", "target": "aarch64-apple-darwin",
        "source_commit": pins["kernel"]["commit"], "version": "0.2.0",
        "artifacts": [{"path": wheel.name, "bytes": 5, "sha256": hashlib.sha256(b"wheel").hexdigest()}],
    }
    path = tmp_path / spec["manifest"]
    path.write_text(json.dumps(receipt))
    assert ap.read_kernel_artifacts(tmp_path, pins, spec)["wheel"] == wheel
    for change, message in (
        ({"source_commit": "deadbeef"}, "pins.json"), ({"kind": "linux"}, "receipt"),
        ({"target": "x86_64-apple-darwin"}, "target"), ({"format": "other"}, "receipt"),
        ({"artifacts": []}, "exactly one wheel"),
        ({"artifacts": [{"path": wheel.name, "sha256": "0" * 64}]}, "does not match"),
    ):
        path.write_text(json.dumps({**receipt, **change}))
        with pytest.raises(ap.PackError, match=message):
            ap.read_kernel_artifacts(tmp_path, pins, spec)
    path.unlink()
    with pytest.raises(ap.PackError, match="readable"):
        ap.read_kernel_artifacts(tmp_path, pins, spec)
