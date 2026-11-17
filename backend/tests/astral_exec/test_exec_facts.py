"""Drives astral_exec.facts.probe with injected syscalls, paths, commands and environment for every refusal reason.
Each probe result is fed to select, so a fact that probes wrongly changes the refusal the node reports.
"""

from __future__ import annotations

import os
import sys
import types

import pytest
from astral_exec import Backend, Refusal, select
from astral_exec.constants import FACT_KEYS
from astral_exec.facts import Hooks, probe, resolve_bwrap
from exec_vectors import DATA_DIR, loaded, minimal_policy


def _stat(mode=0o100755, uid=0):
    return types.SimpleNamespace(st_mode=mode, st_uid=uid)


def _missing(path):
    raise FileNotFoundError(path)


def _hooks(**overrides):
    base = {
        "platform": "linux",
        "syscall": lambda number, a, b, c: 6,
        "executable": lambda path: path == sys.executable,
        "stat": lambda path: _stat() if path == "/usr/bin/bwrap" else _missing(path),
        "sandbox_check": lambda pid: 0,
        "read_text": lambda path: "1",
        "run": lambda argv: 0,
        "env": {},
        "euid": lambda: 1000,
    }
    base.update(overrides)
    return Hooks(**base)


def _select(hooks):
    facts = probe(hooks)
    return select(loaded(minimal_policy(), facts["platform"]), facts, DATA_DIR)


def test_probe_returns_exactly_the_documented_keys_without_mode():
    facts = probe(_hooks())
    assert set(facts) == set(FACT_KEYS) - {"mode"}
    assert facts == {
        "platform": "linux",
        "landlock_abi": 6,
        "bwrap": True,
        "userns": True,
        "euid": 1000,
        "sandbox_exec": False,
        "app_sandboxed": False,
    }


@pytest.mark.parametrize("euid", [lambda: 0, lambda: 1 / 0, lambda: -1, lambda: True])
def test_a_root_or_unreadable_effective_uid_refuses_the_linux_backend(euid):
    outcome = _select(_hooks(euid=euid))
    assert isinstance(outcome, Refusal)
    assert outcome.code == "exec.backend_unavailable" and outcome.reason == "root_node"
    assert "unprivileged" in outcome.message


def test_the_userns_refusal_outranks_the_root_refusal():
    outcome = _select(_hooks(euid=lambda: 0, read_text=lambda path: "0"))
    assert isinstance(outcome, Refusal) and outcome.reason == "userns_denied"


def test_a_capable_linux_host_selects_the_linux_backend():
    assert _select(_hooks()) == Backend("linux")


def test_landlock_abi_comes_from_the_version_flag_of_create_ruleset():
    seen = []

    def syscall(number, a, b, c):
        seen.append((number, a, b, c))
        return 8

    assert probe(_hooks(syscall=syscall))["landlock_abi"] == 8
    assert seen == [(444, 0, 0, 1)]


@pytest.mark.parametrize("returned", [5, 3, 1])
def test_old_kernels_refuse_with_the_observed_abi(returned):
    outcome = _select(_hooks(syscall=lambda number, a, b, c: returned))
    assert isinstance(outcome, Refusal)
    assert (outcome.reason, outcome.details) == (
        "landlock_abi",
        {"observed": returned, "required": 6},
    )


def _raises(error):
    def syscall(number, a, b, c):
        raise error

    return syscall


@pytest.mark.parametrize(
    "syscall",
    [
        _raises(OSError(38, "ENOSYS")),
        _raises(OSError(95, "EOPNOTSUPP")),
        _raises(RuntimeError("boom")),
        lambda number, a, b, c: -1,
        lambda number, a, b, c: None,
        lambda number, a, b, c: "8",
        lambda number, a, b, c: True,
    ],
    ids=["enosys", "eopnotsupp", "runtime_error", "minus_one", "none", "text", "bool"],
)
def test_a_failing_or_odd_landlock_probe_reports_zero(syscall):
    assert probe(_hooks(syscall=syscall))["landlock_abi"] == 0
    outcome = _select(_hooks(syscall=syscall))
    assert isinstance(outcome, Refusal) and outcome.reason == "landlock_abi"
    assert outcome.details["observed"] == 0


def test_missing_bwrap_refuses_with_bwrap_missing():
    outcome = _select(_hooks(stat=_missing))
    assert isinstance(outcome, Refusal) and outcome.reason == "bwrap_missing"


def test_a_stat_that_raises_counts_as_absent():
    def stat(path):
        raise OSError("stat failed")

    assert probe(_hooks(stat=stat))["bwrap"] is False


def test_a_setuid_root_bwrap_is_accepted_on_purpose():
    for mode in (0o104755, 0o104555):
        facts = probe(_hooks(stat=lambda path, mode=mode: _stat(mode=mode)))
        assert facts["bwrap"] is True and facts.bwrap_path == "/usr/bin/bwrap"
    assert probe(_hooks(stat=lambda path: _stat(mode=0o104775)))["bwrap"] is False
    assert probe(_hooks(stat=lambda path: _stat(mode=0o104757)))["bwrap"] is False


@pytest.mark.parametrize(
    "info",
    [
        _stat(uid=1000),
        _stat(mode=0o100775),
        _stat(mode=0o100757),
        _stat(mode=0o100644),
        _stat(mode=0o040755),
        _stat(mode=0o120777),
    ],
    ids=[
        "not_root",
        "group_writable",
        "world_writable",
        "not_executable",
        "directory",
        "symlink_mode",
    ],
)
def test_a_bwrap_that_is_not_root_owned_and_locked_down_is_absent(info):
    facts = probe(_hooks(stat=lambda path: info))
    assert facts["bwrap"] is False and facts.bwrap_path is None
    outcome = _select(_hooks(stat=lambda path: info))
    assert isinstance(outcome, Refusal) and outcome.reason == "bwrap_missing"


def test_bwrap_is_looked_up_only_at_fixed_absolute_paths():
    asked = []

    def stat(path):
        asked.append(path)
        return _missing(path)

    assert resolve_bwrap(_hooks(stat=stat)) is None
    assert asked == ["/usr/bin/bwrap", "/bin/bwrap"]


def test_the_second_fixed_path_is_used_when_the_first_is_absent():
    hooks = _hooks(
        stat=lambda path: _stat() if path == "/bin/bwrap" else _missing(path)
    )
    assert resolve_bwrap(hooks) == "/bin/bwrap"
    assert probe(hooks).bwrap_path == "/bin/bwrap"


def test_the_trial_and_the_selected_backend_use_the_resolved_binary():
    seen = []

    def run(argv):
        seen.append(argv[0])
        return 0

    hooks = _hooks(run=run)
    facts = probe(hooks)
    assert facts.bwrap_path == "/usr/bin/bwrap" and seen == ["/usr/bin/bwrap"]
    outcome = select(loaded(minimal_policy()), facts, DATA_DIR)
    assert outcome == Backend("linux") and outcome.bwrap == "/usr/bin/bwrap"


@pytest.mark.parametrize(
    "path_value", [None, "", ".", "bin:", ":/usr/bin", "relative/dir"]
)
def test_a_stand_in_bwrap_in_the_working_directory_is_never_found(
    tmp_path, monkeypatch, path_value
):
    (tmp_path / "bwrap").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "bwrap").chmod(0o755)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "bwrap").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "bin" / "bwrap").chmod(0o755)
    monkeypatch.chdir(tmp_path)
    if path_value is None:
        monkeypatch.delenv("PATH", raising=False)
    else:
        monkeypatch.setenv("PATH", path_value)
    real = Hooks(platform="linux", syscall=lambda n, a, b, c: 6, env={})
    found = resolve_bwrap(real)
    assert found is None or found in ("/usr/bin/bwrap", "/bin/bwrap")
    assert found != str(tmp_path / "bwrap")
    stand_in = Hooks(
        platform="linux",
        syscall=lambda n, a, b, c: 6,
        stat=lambda path: (
            os.stat(tmp_path / "bwrap") if path == "bwrap" else _missing(path)
        ),
        run=lambda argv: 0,
        executable=lambda path: True,
        read_text=lambda path: "1",
    )
    assert probe(stand_in)["bwrap"] is False


def test_the_bwrap_trial_decides_userns_and_runs_the_confinement_flags():
    seen = []

    def run(argv):
        seen.append(list(argv))
        return 1

    outcome = _select(_hooks(run=run))
    assert isinstance(outcome, Refusal) and outcome.reason == "userns_denied"
    argv = seen[0]
    assert argv[0] == "/usr/bin/bwrap"
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-net", "--proc", "--dev"):
        assert flag in argv
    probe_at = argv.index("--probe")
    assert argv[probe_at - 4 : probe_at - 1] == ["-I", "-S", "-B"]
    assert argv[probe_at - 5] == sys.executable
    assert argv[probe_at - 1].endswith("/astral_exec/inner.py")
    work = argv[probe_at + 1]
    assert argv[argv.index("--bind") + 1 : argv.index("--bind") + 3] == [work, work]
    assert argv[argv.index("--ro-bind") : argv.index("--ro-bind") + 3] == [
        "--ro-bind",
        "/",
        "/",
    ]


@pytest.mark.parametrize(
    "run",
    [lambda argv: 1, lambda argv: 126, lambda argv: -9],
    ids=["one", "cannot_exec", "killed"],
)
def test_a_failed_trial_means_no_user_namespaces(run):
    assert probe(_hooks(run=run))["userns"] is False


def test_a_trial_that_raises_means_no_user_namespaces():
    def run(argv):
        raise TimeoutError("hung")

    assert probe(_hooks(run=run))["userns"] is False


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("/proc/sys/user/max_user_namespaces", "0\n"),
        ("/proc/sys/kernel/unprivileged_userns_clone", "0\n"),
    ],
)
def test_sysctls_that_disable_namespaces_skip_the_trial(path, text):
    def read_text(name):
        return text if name == path else "1\n"

    def run(argv):
        raise AssertionError("trial must not run")

    assert probe(_hooks(read_text=read_text, run=run))["userns"] is False


def test_unreadable_sysctls_defer_to_the_trial():
    def read_text(name):
        raise FileNotFoundError(name)

    assert probe(_hooks(read_text=read_text))["userns"] is True
    assert probe(_hooks(read_text=read_text, run=lambda argv: 1))["userns"] is False


def test_garbage_sysctls_defer_to_the_trial():
    assert probe(_hooks(read_text=lambda name: "banana"))["userns"] is True


def test_without_a_trial_userns_is_false_whatever_the_sysctls_say():
    no_bwrap = {"stat": _missing}

    def run(argv):
        raise AssertionError("no trial without bwrap")

    assert probe(_hooks(run=run, **no_bwrap))["userns"] is False
    assert (
        probe(_hooks(run=run, read_text=lambda name: "1", **no_bwrap))["userns"]
        is False
    )

    def unreadable(name):
        raise FileNotFoundError(name)

    assert probe(_hooks(run=run, read_text=unreadable, **no_bwrap))["userns"] is False


def test_no_runnable_interpreter_means_no_trial_and_no_userns():
    def run(argv):
        raise AssertionError("no trial without an interpreter")

    facts = probe(_hooks(run=run, executable=lambda path: False))
    assert facts["bwrap"] is True
    assert facts["userns"] is False


def test_the_trial_cleans_up_its_directory():
    seen = []

    def run(argv):
        seen.append(argv[argv.index("--probe") + 1])
        assert os.path.isdir(seen[0])
        return 0

    assert probe(_hooks(run=run))["userns"] is True
    assert not os.path.exists(seen[0])


def test_darwin_finds_sandbox_exec_and_ignores_linux_probes():
    hooks = _hooks(
        platform="darwin",
        executable=lambda path: path == "/usr/bin/sandbox-exec",
        syscall=_raises(AssertionError("never called on darwin")),
        run=lambda argv: (_ for _ in ()).throw(AssertionError("no trial on darwin")),
        stat=_missing,
    )
    facts = probe(hooks)
    assert facts == {
        "platform": "darwin",
        "landlock_abi": 0,
        "bwrap": False,
        "userns": False,
        "euid": 1000,
        "sandbox_exec": True,
        "app_sandboxed": False,
    }
    assert _select(hooks) == Backend("seatbelt")


def test_darwin_without_sandbox_exec_refuses():
    outcome = _select(_hooks(platform="darwin", executable=lambda path: False))
    assert isinstance(outcome, Refusal) and outcome.reason == "sandbox_exec_missing"


@pytest.mark.parametrize(
    "env",
    [
        {"APP_SANDBOX_CONTAINER_ID": "com.astral.node"},
        {"HOME": "/Users/u/Library/Containers/com.astral.node/Data"},
    ],
    ids=["container_id", "container_home"],
)
def test_the_app_sandbox_is_detected_on_darwin(env):
    hooks = _hooks(platform="darwin", executable=lambda path: True, env=env)
    assert probe(hooks)["app_sandboxed"] is True
    outcome = _select(hooks)
    assert isinstance(outcome, Refusal) and outcome.reason == "app_sandboxed"


def test_sandbox_check_is_the_primary_app_sandbox_signal():
    seen = []

    def check(pid):
        seen.append(pid)
        return 1

    hooks = _hooks(
        platform="darwin",
        executable=lambda path: True,
        sandbox_check=check,
        pid=lambda: 4242,
    )
    assert probe(hooks)["app_sandboxed"] is True
    assert seen == [4242]
    outcome = _select(hooks)
    assert isinstance(outcome, Refusal) and outcome.reason == "app_sandboxed"


def test_an_unavailable_or_failing_sandbox_check_reports_sandboxed_on_darwin():
    def broken(pid):
        raise OSError("symbol missing")

    for check in (broken, lambda pid: None):
        hooks = _hooks(
            platform="darwin", executable=lambda path: True, sandbox_check=check
        )
        assert probe(hooks)["app_sandboxed"] is True


def test_a_clean_sandbox_check_with_a_clean_environment_is_not_sandboxed():
    hooks = _hooks(
        platform="darwin",
        executable=lambda path: True,
        sandbox_check=lambda pid: 0,
        env={},
    )
    assert probe(hooks)["app_sandboxed"] is False


def test_an_empty_container_id_is_not_a_sandbox():
    hooks = _hooks(
        platform="darwin",
        executable=lambda path: True,
        env={"APP_SANDBOX_CONTAINER_ID": ""},
    )
    assert probe(hooks)["app_sandboxed"] is False


def test_container_variables_mean_nothing_off_darwin():
    hooks = _hooks(
        env={"APP_SANDBOX_CONTAINER_ID": "x", "HOME": "/Library/Containers/x"}
    )
    assert probe(hooks)["app_sandboxed"] is False


def test_an_executable_check_that_raises_counts_as_absent():
    def executable(path):
        raise PermissionError(path)

    assert (
        probe(_hooks(platform="darwin", executable=executable))["sandbox_exec"] is False
    )


def test_other_platforms_are_refused_by_name():
    outcome = _select(_hooks(platform="win32"))
    assert isinstance(outcome, Refusal)
    assert outcome.reason == "platform_unsupported"
    assert outcome.details == {"platform": "win32"}
    assert "win32" in outcome.message


def test_other_platforms_report_no_probe_results():
    facts = probe(_hooks(platform="freebsd14"))
    assert facts["platform"] == "freebsd14"
    assert facts["landlock_abi"] == 0 and not facts["bwrap"] and not facts["userns"]


def test_default_hooks_describe_this_machine():
    facts = probe(Hooks(run=lambda argv: 1))
    assert set(facts) == set(FACT_KEYS) - {"mode"}
    assert isinstance(facts["landlock_abi"], int) and not isinstance(
        facts["landlock_abi"], bool
    )
    assert all(
        isinstance(facts[k], bool)
        for k in ("bwrap", "userns", "sandbox_exec", "app_sandboxed")
    )
