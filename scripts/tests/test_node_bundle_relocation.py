"""Checks the relocation harness unpacks into a path with a space, runs the pack's interpreter with a minimal environment and reports caches, launcher failures and tree drift."""

import json

import pytest
from node_bundle_support import manifest as pm
from node_bundle_support import relocation_check as rc

PYTHON = """#!/bin/sh
case "$1 $2" in
  "-I -B") ;;
  *) echo "interpreter flags missing" >&2; exit 3 ;;
esac
if [ "$PATH" != "/usr/bin:/bin" ] || [ -n "$HOME" ]; then echo "environment leaked" >&2; exit 4; fi
case "$PWD" in *"relocated copy"*) ;; *) echo "not run from the spaced path" >&2; exit 5 ;; esac
%s
"""


def fake_pack(tmp_path, behaviour="echo PASS child; exit 0", launcher_ok=True, flags=3, baked=True):
    stage = tmp_path / "stage"
    (stage / "python" / "bin").mkdir(parents=True)
    python = stage / "python" / "bin" / "python3.11"
    python.write_text(PYTHON % behaviour)
    python.chmod(0o755)
    (stage / "launcher").mkdir()
    (stage / "launcher" / "astral_node_launch.py").write_text("ok" if launcher_ok else "fail")
    (stage / "launcher" / "__pycache__").mkdir()
    if baked:
        (stage / "launcher" / "__pycache__" / "astral_node_launch.cpython-311.pyc").write_bytes(
            b"\xa7\r\r\n" + flags.to_bytes(4, "little") + b"body")
    manifest = pm.write_manifest(stage, {"version": "1.0", "platform": "macos-arm64"})
    out = tmp_path / "out"
    out.mkdir()
    archive = pm.write_archive(stage, out / "pack.tar.gz")
    (out / "node-pack.json").write_text(json.dumps(pm.descriptor(manifest, archive, pm.sha256_file(stage / "manifest.json"))))
    return out, stage


@pytest.fixture(autouse=True)
def launcher_probe(monkeypatch):
    def probe(root):
        script = (root / "launcher" / "astral_node_launch.py").read_text()
        return rc.subprocess.CompletedProcess([], 0 if script == "ok" else 1, "", "launcher broke")

    monkeypatch.setattr(rc, "run_launcher_help", probe)


def test_a_healthy_pack_passes_from_a_spaced_path_with_a_minimal_environment(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out, _ = fake_pack(tmp_path)
    assert rc.check_pack(out) == []
    assert "PASS child" in capsys.readouterr().out


def test_an_unpacked_pack_root_is_accepted(tmp_path):
    _, stage = fake_pack(tmp_path)
    assert rc.check_pack(stage) == []


def test_a_failing_child_is_reported_with_its_error(tmp_path):
    out, _ = fake_pack(tmp_path, "echo boom >&2; exit 7")
    failures = rc.check_pack(out)
    assert failures and "child exited 7" in failures[0] and "boom" in failures[0]


def test_a_launcher_that_fails_to_start_is_reported(tmp_path):
    out, _ = fake_pack(tmp_path, launcher_ok=False)
    assert any("launcher --help exited 1" in failure for failure in rc.check_pack(out))


def test_bytecode_written_during_the_run_is_reported(tmp_path):
    out, _ = fake_pack(tmp_path, 'mkdir "$(dirname "$0")/../../launcher/__pycache__"; echo x > "$(dirname "$0")/../../launcher/__pycache__/m.pyc"; exit 0')
    assert any("bytecode caches appeared" in failure for failure in rc.check_pack(out))


def test_a_file_modified_during_the_run_is_reported(tmp_path):
    out, _ = fake_pack(tmp_path, 'echo tampered > "$(dirname "$0")/../../launcher/astral_node_launch.py"; exit 0')
    assert any("content changed launcher/astral_node_launch.py" in failure for failure in rc.check_pack(out))


def test_a_folder_that_is_not_a_pack_is_refused(tmp_path):
    with pytest.raises(rc.RelocationError, match="neither"):
        rc.check_pack(tmp_path)


def test_the_command_line_exits_with_the_failures_or_a_pass_line(tmp_path, capsys):
    out, _ = fake_pack(tmp_path)
    rc.main([str(out)])
    assert "ALL CHECKS PASSED" in capsys.readouterr().out
    bad, _ = fake_pack(tmp_path / "bad", "exit 9")
    with pytest.raises(SystemExit, match="child exited 9"):
        rc.main([str(bad)])
    with pytest.raises(SystemExit, match="neither"):
        rc.main([str(tmp_path / "missing-dir-with-nothing")])


def test_the_real_launcher_probe_runs_the_launcher_with_isolated_flags(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.undo()
    monkeypatch.setattr(rc.subprocess, "run", lambda command, **kw: seen.update(command=command, env=kw["env"]))
    rc.run_launcher_help(tmp_path)
    assert seen["command"][1:3] == ["-I", "-B"] and seen["command"][-1] == "--help"
    assert seen["env"] == rc.CHILD_ENV


def test_the_child_script_checks_the_interpreter_and_every_pack_module():
    assert "inside(sys.executable)" in rc.CHILD_SCRIPT and "sys.dont_write_bytecode" in rc.CHILD_SCRIPT
    assert {"astral_kernel", "rote", "astral_exec"} <= set(rc.MODULES + rc.BACKEND_MODULES)


def test_a_pack_without_baked_or_with_unchecked_bytecode_is_reported(tmp_path):
    out, _ = fake_pack(tmp_path / "none", baked=False)
    assert any("no baked bytecode under launcher" in failure for failure in rc.check_pack(out))
    out, _ = fake_pack(tmp_path / "timestamp", flags=0)
    assert any("not checked-hash" in failure for failure in rc.check_pack(out))
