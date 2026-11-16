"""Drives astral_exec.facts.probe with injected syscalls, paths, commands and environment for every refusal reason.
Each probe result is fed to select, so a fact that probes wrongly changes the refusal the node reports.
"""

from __future__ import annotations

import pytest
from astral_exec import Backend, Policy, Refusal, select
from astral_exec.constants import FACT_KEYS
from astral_exec.facts import Hooks, probe
from exec_vectors import minimal_policy

DATA_DIR = "/var/lib/astral/node"


def _hooks(**overrides):
    base = {
        "platform": "linux",
        "syscall": lambda number, a, b, c: 6,
        "executable": lambda path: path == "/usr/bin/true",
        "which": lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
        "read_text": lambda path: "1",
        "run": lambda argv: 0,
        "env": {},
    }
    base.update(overrides)
    return Hooks(**base)


def _select(hooks):
    facts = probe(hooks)
    policy = Policy.parse(minimal_policy(), facts["platform"])
    return select(policy, facts, DATA_DIR)


def test_probe_returns_exactly_the_documented_keys_without_mode():
    facts = probe(_hooks())
    assert set(facts) == set(FACT_KEYS) - {"mode"}
    assert facts == {
        "platform": "linux",
        "landlock_abi": 6,
        "bwrap": True,
        "userns": True,
        "sandbox_exec": False,
        "app_sandboxed": False,
    }


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
    outcome = _select(_hooks(which=lambda name: None))
    assert isinstance(outcome, Refusal) and outcome.reason == "bwrap_missing"


def test_a_which_that_raises_counts_as_absent():
    def which(name):
        raise OSError("path walk failed")

    facts = probe(_hooks(which=which))
    assert facts["bwrap"] is False


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
    assert argv[-1] in ("/usr/bin/true", "/bin/true")


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


def test_without_bwrap_the_sysctls_alone_answer_userns():
    no_bwrap = {"which": lambda name: None}
    assert probe(_hooks(**no_bwrap))["userns"] is True
    assert probe(_hooks(read_text=lambda name: "0", **no_bwrap))["userns"] is False


def test_no_true_binary_means_no_trial_and_no_userns():
    def run(argv):
        raise AssertionError("no trial without a true binary")

    facts = probe(_hooks(run=run, executable=lambda path: False))
    assert facts["bwrap"] is True
    assert facts["userns"] is False


def test_bin_true_is_used_when_usr_bin_true_is_absent():
    seen = []

    def run(argv):
        seen.append(argv[-1])
        return 0

    assert (
        probe(_hooks(run=run, executable=lambda path: path == "/bin/true"))["userns"]
        is True
    )
    assert seen == ["/bin/true"]


def test_darwin_finds_sandbox_exec_and_ignores_linux_probes():
    hooks = _hooks(
        platform="darwin",
        executable=lambda path: path == "/usr/bin/sandbox-exec",
        syscall=_raises(AssertionError("never called on darwin")),
        run=lambda argv: (_ for _ in ()).throw(AssertionError("no trial on darwin")),
        which=lambda name: None,
    )
    facts = probe(hooks)
    assert facts == {
        "platform": "darwin",
        "landlock_abi": 0,
        "bwrap": False,
        "userns": False,
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
    facts = probe()
    assert set(facts) == set(FACT_KEYS) - {"mode"}
    assert isinstance(facts["landlock_abi"], int) and not isinstance(
        facts["landlock_abi"], bool
    )
    assert all(
        isinstance(facts[k], bool)
        for k in ("bwrap", "userns", "sandbox_exec", "app_sandboxed")
    )
