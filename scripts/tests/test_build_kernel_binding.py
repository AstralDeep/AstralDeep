"""Exercises source-bound Kernel artifact preparation without invoking Docker or fetching source."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from scripts import install_kernel_binding as binding

SCRIPT = Path(__file__).resolve().parents[1] / "build_kernel_binding.py"


@pytest.fixture
def builder(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scripts.build_kernel_binding", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "deep"
    source = root / "components" / "AstralKernel"
    source.mkdir(parents=True)
    output = root / "build" / "kernel-binding"
    payloads = {"k.whl": b"wheel", "mailbox": b"mailbox"}
    manifest = {
        "format": binding.FORMAT,
        "source": {"commit": "1" * 40},
        "artifacts": {"linux-aarch64": {
            name: {"file": filename, "sha256": binding.hashlib.sha256(payloads[filename]).hexdigest()}
            for name, filename in (("wheel", "k.whl"), ("mailbox", "mailbox"))
        }},
    }
    (root / "config").mkdir()
    (root / "config" / "astral-kernel-binding.json").write_text(json.dumps(manifest))
    calls = []
    state = {"head": "1" * 40, "dirty": "", "payloads": payloads}

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "git":
            value = state["head"] if "rev-parse" in command else state["dirty"]
            return subprocess.CompletedProcess(command, 0, stdout=value, stderr="")
        for name, value in state["payloads"].items():
            (output / name).write_bytes(value)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.binding, "platform_key", lambda **kwargs: "linux-aarch64")
    return module, root, output, calls, state


def test_prepares_exact_source_and_verifies_built_outputs(builder):
    module, root, output, calls, _ = builder
    module.prepare(root, output)
    assert calls[-1] == [
        "bash", str(root / "components/AstralKernel/scripts/build-python.sh"),
        "--platform", "linux/arm64", "--out", str(output),
    ]
    assert (output / "k.whl").read_bytes() == b"wheel"
    assert all(call[0] == "git" for call in calls[:-1])


def test_verified_cache_still_checks_source_but_never_builds(builder):
    module, root, output, calls, state = builder
    output.mkdir(parents=True)
    for name, value in state["payloads"].items():
        (output / name).write_bytes(value)
    module.prepare(root, output)
    assert len(calls) == 2 and all(call[0] == "git" for call in calls)


@pytest.mark.parametrize("field,value,reason", [
    ("head", "2" * 40, "source does not match"),
    ("dirty", " M crates/engine.rs", "source is dirty"),
])
def test_source_mismatch_never_invokes_builder(builder, field, value, reason):
    module, root, output, calls, state = builder
    state[field] = value
    with pytest.raises(binding.BindingError, match=reason):
        module.prepare(root, output)
    assert all(call[0] == "git" for call in calls)
    assert not output.exists()


def test_invalid_source_pin_is_refused_before_any_process(builder):
    module, root, output, calls, _ = builder
    path = root / "config/astral-kernel-binding.json"
    manifest = json.loads(path.read_text())
    manifest["source"]["commit"] = "main"
    path.write_text(json.dumps(manifest))
    with pytest.raises(binding.BindingError, match="immutable source commit"):
        module.prepare(root, output)
    assert calls == []


@pytest.mark.parametrize("key", ["linux-riscv64", "linux-x86_64"])
def test_unpinned_platform_never_builds(builder, key):
    module, root, output, calls, _ = builder
    with pytest.raises(binding.BindingError):
        module.prepare(root, output, key=key)
    assert calls == []


def test_x86_build_uses_native_docker_platform(builder):
    module, root, output, calls, _ = builder
    path = root / "config/astral-kernel-binding.json"
    manifest = json.loads(path.read_text())
    manifest["artifacts"]["linux-x86_64"] = manifest["artifacts"].pop("linux-aarch64")
    path.write_text(json.dumps(manifest))
    module.prepare(root, output, key="linux-x86_64")
    assert calls[-1][-3:] == ["linux/amd64", "--out", str(output)]


def test_corrupt_cache_is_not_silently_replaced(builder):
    module, root, output, calls, _ = builder
    output.mkdir(parents=True)
    (output / "k.whl").write_bytes(b"corrupt")
    with pytest.raises(binding.BindingError, match="digest mismatch"):
        module.prepare(root, output)
    assert all(call[0] == "git" for call in calls)


def test_corrupt_builder_result_fails_closed(builder):
    module, root, output, _, state = builder
    state["payloads"] = {"k.whl": b"corrupt", "mailbox": b"mailbox"}
    with pytest.raises(binding.BindingError, match="digest mismatch"):
        module.prepare(root, output)


def test_cli_reports_success_and_failure(builder, monkeypatch, capsys):
    module, root, output, _, _ = builder
    assert module.main(["--root", str(root), "--output", str(output)]) == 0
    assert "verified" in capsys.readouterr().out
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["docker"])
    monkeypatch.setattr(module, "prepare", fail)
    assert module.main(["--root", str(root), "--output", str(output)]) == 1
    assert "kernel binding" in capsys.readouterr().err
