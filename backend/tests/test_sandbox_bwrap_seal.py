"""Exercises the bounded SRT mount adapter and its immutable private configuration before native execution."""

import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys

import pytest

from orchestrator import sandbox_bwrap as seal
from orchestrator import sandbox_qualification as sq


def arguments(scratch="/owned/scratch"):
    return [
        "--new-session",
        "--die-with-parent",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        scratch,
        scratch,
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home",
        "--bind",
        scratch,
        scratch,
        "--dev",
        "/dev",
        "--unshare-pid",
        "--unshare-user",
        "--cap-drop",
        "ALL",
        "--proc",
        "/proc",
        "--",
        "/bin/bash",
        "-c",
        "exec fixed-effect",
    ]


def config(path, value=None, mode=0o400):
    path.write_text(
        json.dumps(value or {"binary": "/usr/bin/bwrap", "scratch": "/owned/scratch"})
    )
    path.chmod(mode)
    return path


def test_mounts_are_sealed_after_setup_without_changing_the_command():
    original = arguments()
    result = seal.sealed_arguments(original, "/owned/scratch")
    boundary = original.index("--")
    assert original == arguments()
    assert result[:boundary] == original[:boundary]
    assert result[boundary : boundary + 6] == [
        "--remount-ro",
        "/tmp",
        "--remount-ro",
        "/home",
        "--remount-ro",
        "/dev",
    ]
    assert result[boundary + 6 :] == original[boundary:]


def test_duplicate_masks_are_sealed_once():
    original = arguments()
    boundary = original.index("--")
    original[boundary:boundary] = ["--tmpfs", "/tmp"]
    result = seal.sealed_arguments(original, "/owned/scratch")
    assert result.count("--remount-ro") == 3


@pytest.mark.parametrize("kind", ["http", "socks"])
def test_pinned_proxy_socket_bind_must_precede_the_read_only_root(kind):
    proxy = f"/tmp/claude-{kind}-0123456789abcdef.sock"
    original = arguments()
    original[0:0] = ["--bind", proxy, proxy]
    assert seal.sealed_arguments(original, "/owned/scratch")[:3] == original[:3]
    original = arguments()
    boundary = original.index("--")
    original[boundary:boundary] = ["--bind", proxy, proxy]
    with pytest.raises(ValueError):
        seal.sealed_arguments(original, "/owned/scratch")


@pytest.mark.parametrize(
    "source,target",
    [
        ("/review/outside", "/review/outside"),
        ("/review/outside", "/owned/scratch"),
        ("/owned/scratch", "/review/outside"),
        ("/owned/scratch/child", "/owned/scratch/child"),
        ("/tmp/claude-http-wrong.sock", "/tmp/claude-http-wrong.sock"),
    ],
)
@pytest.mark.parametrize("before_root", [False, True])
def test_unreviewed_writable_binds_are_refused(source, target, before_root):
    original = arguments()
    boundary = 0 if before_root else original.index("--")
    original[boundary:boundary] = ["--bind", source, target]
    with pytest.raises(ValueError):
        seal.sealed_arguments(original, "/owned/scratch")


def test_duplicate_root_bind_is_refused():
    original = arguments()
    boundary = original.index("--")
    original[boundary:boundary] = ["--ro-bind", "/", "/"]
    with pytest.raises(ValueError):
        seal.sealed_arguments(original, "/owned/scratch")


@pytest.mark.parametrize("value", ["--tmpfs", "--args", "--"])
def test_environment_values_are_opaque_and_never_logged(capsys, value):
    original = arguments()
    original[0:0] = ["--setenv", "FIXTURE_VALUE", value]
    assert seal.sealed_arguments(original, "/owned/scratch")[:3] == original[:3]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "flag",
    [
        "--new-session",
        "--die-with-parent",
        "--unshare-net",
        "--unshare-user",
        "--unshare-pid",
    ],
)
def test_missing_or_duplicate_namespace_fences_are_refused(flag):
    original = arguments()
    original.remove(flag)
    with pytest.raises(ValueError):
        seal.sealed_arguments(original, "/owned/scratch")
    original = arguments()
    original.insert(0, flag)
    with pytest.raises(ValueError):
        seal.sealed_arguments(original, "/owned/scratch")


@pytest.mark.parametrize(
    "changes",
    [
        ["--unknown"],
        ["--bind"],
        ["--setenv", "KEY"],
        ["--tmpfs"],
        ["--tmpfs", "/"],
        ["--tmpfs", "/tmp/../home"],
        ["--tmpfs", "relative"],
        ["--dev", "/other"],
        ["--proc", "/other"],
        ["--cap-add", "ALL"],
        ["--cap-drop", "NET_ADMIN"],
        ["--bind", "/", "/"],
    ],
)
def test_unknown_weakened_or_malformed_options_are_refused(changes):
    original = arguments()
    original[0:0] = changes
    with pytest.raises(ValueError):
        seal.sealed_arguments(original, "/owned/scratch")


@pytest.mark.parametrize(
    "target", ["", "/", "//owned/scratch", "/owned/../scratch", "relative", None, True]
)
def test_invalid_scratch_is_refused(target):
    with pytest.raises(ValueError):
        seal.sealed_arguments(arguments(), target)


def test_missing_scratch_binding_root_read_only_or_command_is_refused():
    for original in [
        arguments("/other"),
        arguments()[6:],
        arguments()[: arguments().index("--")],
        arguments()[: arguments().index("--") + 1],
    ]:
        with pytest.raises(ValueError):
            seal.sealed_arguments(original, "/owned/scratch")


def test_only_the_exact_fixed_dependency_probe_can_run_without_an_effect():
    probe = ["--unshare-user", "--dev-bind", "/", "/", "true"]
    assert seal.sealed_arguments(probe, "/owned/scratch") == probe
    for command in ["false", "echo", "/bin/sh"]:
        with pytest.raises(ValueError):
            seal.sealed_arguments([*probe[:-1], command], "/owned/scratch")


def test_regular_args_file_expands_without_leaking_its_descriptor():
    original = arguments()
    with tempfile.TemporaryFile() as stream:
        stream.write(b"--tmpfs\0/tmp\0")
        stream.flush()
        fd = os.dup(stream.fileno())
        original[original.index("--") : original.index("--")] = ["--args", str(fd)]
        result = seal.sealed_arguments(original, "/owned/scratch")
        assert "--args" not in result and result.count("--remount-ro") == 3
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    "payload",
    [
        b"--tmpfs\0/tmp",
        b"--args\0" + b"3\0",
        b"--\0/bin/sh\0",
        b"x" * (1024 * 1024 + 1),
    ],
)
def test_bad_args_files_are_refused_and_closed(payload):
    with tempfile.TemporaryFile() as stream:
        stream.write(payload)
        stream.flush()
        fd = os.dup(stream.fileno())
        original = arguments()
        original[0:0] = ["--args", str(fd)]
        with pytest.raises(ValueError):
            seal.sealed_arguments(original, "/owned/scratch")
        with pytest.raises(OSError):
            os.fstat(fd)


def test_pipe_standard_and_noncanonical_args_descriptors_are_refused():
    read_fd, write_fd = os.pipe()
    try:
        for value in ["0", "1", "2", "-1", "03", "x", str(read_fd)]:
            with pytest.raises((ValueError, OSError)):
                seal.sealed_arguments(["--args", value, *arguments()], "/owned/scratch")
    finally:
        for fd in [read_fd, write_fd]:
            try:
                os.close(fd)
            except OSError:
                pass


def test_argument_and_byte_limits_fail_closed():
    for original in [
        [],
        ["x"] * 8193,
        [*arguments(), "x" * 131072],
        [*arguments(), *["x" * 65536] * 17],
        [*arguments(), "bad\0word"],
        [None],
    ]:
        with pytest.raises(ValueError):
            seal.sealed_arguments(original, "/owned/scratch")


def test_final_added_seals_cannot_exceed_the_argument_bound():
    original = arguments()
    original[0:0] = [
        word for value in range(2050) for word in ["--tmpfs", f"/mask-{value}"]
    ]
    with pytest.raises(ValueError):
        seal.sealed_arguments(original, "/owned/scratch")


def test_fifo_configuration_refuses_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / "config.json"
    os.mkfifo(path, 0o400)
    script = (
        "from pathlib import Path; from orchestrator.sandbox_bwrap import load_config; "
        f"load_config(Path({str(path)!r}))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=3
        )
    except subprocess.TimeoutExpired:
        pytest.fail("private FIFO configuration blocked awaiting a writer")
    assert result.returncode != 0
    assert "configuration custody is invalid" in result.stderr


def test_private_config_has_exact_closed_shape_and_file_custody(tmp_path):
    path = config(tmp_path / "seal.json")
    assert seal.load_config(path) == ("/usr/bin/bwrap", "/owned/scratch")
    path.chmod(0o600)
    with pytest.raises(ValueError):
        seal.load_config(path)
    path.chmod(0o400)
    linked = tmp_path / "linked"
    os.link(path, linked)
    with pytest.raises(ValueError):
        seal.load_config(path)
    linked.unlink()
    symlink = tmp_path / "symlink"
    symlink.symlink_to(path)
    with pytest.raises(OSError):
        seal.load_config(symlink)


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b'{"binary":"/usr/bin/bwrap","scratch":"/","extra":1}',
        b'{"binary":"/usr/bin/bwrap","binary":"/bin/sh","scratch":"/owned/scratch"}',
        b'{"binary":false,"scratch":"/owned/scratch"}',
        b'{"binary":"relative","scratch":"/owned/scratch"}',
        b"x" * 16385,
        b"\xff",
    ],
)
def test_invalid_private_config_is_refused(tmp_path, payload):
    path = tmp_path / "seal.json"
    path.write_bytes(payload)
    path.chmod(0o400)
    with pytest.raises((ValueError, UnicodeError)):
        seal.load_config(path)


def test_bound_binary_receives_only_the_sealed_argv(tmp_path, monkeypatch):
    path = config(tmp_path / "seal.json")
    seen = []

    class Executed(BaseException):
        pass

    def execute(binary, argv):
        seen.append((binary, argv))
        raise Executed

    monkeypatch.setattr(seal.os, "execv", execute)
    with pytest.raises(Executed):
        seal.run(arguments(), path)
    assert seen == [
        (
            "/usr/bin/bwrap",
            ["/usr/bin/bwrap", *seal.sealed_arguments(arguments(), "/owned/scratch")],
        )
    ]


def test_invalid_or_failed_exec_reports_only_a_fixed_error(
    tmp_path, monkeypatch, capsys
):
    path = config(tmp_path / "seal.json")
    assert seal.run(["arbitrary-fixture-value"], path) == 78
    assert capsys.readouterr().err == "sandbox_bwrap_unavailable\n"

    def failed(*_):
        raise OSError("private fixture diagnostic")

    monkeypatch.setattr(seal.os, "execv", failed)
    assert seal.run(arguments(), path) == 78
    assert capsys.readouterr().err == "sandbox_bwrap_unavailable\n"


@pytest.fixture
def runtime(tmp_path):
    return sq.SrtRuntime(
        node_path=Path("/usr/bin/node"),
        cli_js=tmp_path / "runtime/dist/cli.js",
        apply_seccomp_path=tmp_path / "runtime/vendor/seccomp/apply-seccomp",
        bwrap_path=Path("/usr/bin/bwrap"),
        socat_path=Path("/usr/bin/socat"),
        artifact_root=tmp_path / "runtime",
        pin=sq.default_artifact_pin(),
    )


def test_parent_adapter_and_child_interpreter_do_not_follow_ambient_path(tmp_path, runtime, monkeypatch):
    directory = tmp_path / "unreviewed-bin"
    directory.mkdir()
    interpreter = directory / "python3"
    interpreter.write_text("#!/bin/sh\nexit 93\n")
    interpreter.chmod(0o700)
    monkeypatch.setenv("PATH", str(directory))
    session = sq.prepare_session(session_dir=tmp_path / "control")
    sq.write_immutable_profile(session, sq.build_srt_settings(session, runtime), runtime)
    adapter = session.session_dir / "bwrap-seal.py"
    assert adapter.read_bytes().splitlines()[0] == ("#!" + sys.executable).encode()
    captured = []

    class Supervisor:
        def spawn(self, **kwargs):
            captured.append(kwargs)
            return self

    supervisor = Supervisor()
    assert sq.launch_sandboxed_effect(supervisor, session, runtime, session.profile_path) is supervisor
    child = Path(captured[0]["argv"][-3]).resolve()
    assert child.is_relative_to("/usr") or child.is_relative_to("/bin")
    assert child != interpreter


def test_missing_approved_child_interpreter_refuses_before_spawn(tmp_path, runtime, monkeypatch):
    session = sq.prepare_session(session_dir=tmp_path / "control")
    monkeypatch.setattr(sq.shutil, "which", lambda *_args, **_kwargs: None)
    sq.write_immutable_profile(session, sq.build_srt_settings(session, runtime), runtime)

    class Supervisor:
        def spawn(self, **_kwargs):
            pytest.fail("missing approved interpreter reached physical spawn")

    with pytest.raises(sq.SandboxQualificationError, match="effect_python_unavailable"):
        sq.launch_sandboxed_effect(Supervisor(), session, runtime, session.profile_path)


def test_profile_uses_an_exclusive_immutable_captured_adapter(tmp_path, runtime):
    session = sq.prepare_session(session_dir=tmp_path / "control")
    settings = sq.build_srt_settings(session, runtime)
    assert settings["bwrapPath"] == str(session.session_dir / "bwrap-seal.py")
    sq.write_immutable_profile(session, settings, runtime)
    wrapper = session.session_dir / "bwrap-seal.py"
    assert wrapper.read_bytes().split(b"\n", 1)[1] == Path(seal.__file__).read_bytes()
    assert seal.load_config(wrapper.with_suffix(".json")) == (
        str(runtime.bwrap_path),
        str(session.scratch_dir),
    )
    sq.verify_bwrap_bundle(session, runtime)
    with pytest.raises(sq.SandboxQualificationError, match="session_artifact_exists"):
        sq.write_immutable_profile(session, settings, runtime)


@pytest.mark.parametrize("name", ["bwrap-seal.py", "bwrap-seal.json", "profile.json"])
def test_changed_bundle_fails_before_spawn(tmp_path, runtime, name):
    session = sq.prepare_session(session_dir=tmp_path / "control")
    settings = sq.build_srt_settings(session, runtime)
    sq.write_immutable_profile(session, settings, runtime)
    target = (
        session.profile_path if name == "profile.json" else session.session_dir / name
    )
    target.chmod(0o600)
    target.write_bytes(b"changed")

    class Supervisor:
        def spawn(self, **_):
            pytest.fail("changed bundle reached a physical launch")

    with pytest.raises(sq.SandboxQualificationError, match="session_bwrap_changed"):
        sq.launch_sandboxed_effect(Supervisor(), session, runtime, session.profile_path)


def test_missing_linked_or_symlinked_bundle_is_refused(tmp_path, runtime):
    session = sq.prepare_session(session_dir=tmp_path / "control")
    with pytest.raises(sq.SandboxQualificationError, match="session_bwrap_changed"):
        sq.verify_bwrap_bundle(session, runtime)
    sq.write_immutable_profile(
        session, sq.build_srt_settings(session, runtime), runtime
    )
    wrapper = session.session_dir / "bwrap-seal.py"
    saved = tmp_path / "saved"
    wrapper.rename(saved)
    wrapper.symlink_to(saved)
    with pytest.raises(sq.SandboxQualificationError, match="session_bwrap_changed"):
        sq.verify_bwrap_bundle(session, runtime)
    wrapper.unlink()
    saved.rename(wrapper)
    os.link(wrapper, saved)
    with pytest.raises(sq.SandboxQualificationError, match="session_bwrap_changed"):
        sq.verify_bwrap_bundle(session, runtime)
