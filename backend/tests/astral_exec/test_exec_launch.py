"""Drives the astral_exec launcher without sandbox-exec: refusals and launch failures start no process, the trampoline, the monitoring loop, the watchdog over an injected process table and the run.py command line.
The confined runs live in test_exec_seatbelt_live.py, and every test here runs on any platform.
"""

from __future__ import annotations

import ctypes
import json
import os
import runpy
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import logging

import pytest
from astral_exec import Policy, constants, launch, watchdog
from astral_exec import run as run_command
from astral_exec.constants import LAUNCH_MARKER, MESSAGES, REFUSED_EXIT_STATUS
from astral_exec.errors import ContextError
from astral_exec.facts import Facts
from astral_exec.launch import Outcome
from astral_exec.watchdog import (
    CTL_KERN,
    KERN_ARGMAX,
    KERN_PROCARGS2,
    PROC_PGRP_ONLY,
    PROC_PPID_ONLY,
    PROC_UID_ONLY,
    Darwin,
    Usage,
    Watch,
    procargs_strings,
)
from exec_vectors import good_facts, loaded, minimal_policy

DATA = "/Users/test/Library/Application Support/Astral/node"
WORKSPACE = DATA + "/exec/call"
HOME = "/Users/test"
PYTHON = sys.executable


def darwin_facts(**overrides):
    facts = Facts(
        platform="darwin",
        landlock_abi=0,
        bwrap=False,
        userns=False,
        euid=501,
        sandbox_exec=True,
        app_sandboxed=False,
    )
    facts.update(overrides)
    return facts


def darwin_policy(**overrides):
    return loaded(
        minimal_policy(workspace=WORKSPACE, **overrides), "darwin", DATA, HOME
    )


@pytest.fixture
def real_call(tmp_path):
    data = (tmp_path / "data").resolve()
    (data / "exec" / "call").mkdir(parents=True)
    home = (tmp_path / "home").resolve()
    home.mkdir()

    def make(**limit_overrides):
        workspace = data / "exec" / "call"
        document = minimal_policy(workspace=str(workspace))
        document["limits"].update(limit_overrides)
        return Policy.load(document, "darwin", str(data), str(home))

    make.data = str(data)
    return make


@pytest.fixture
def spawns(monkeypatch):
    calls = []

    def refuse_to_spawn(*args, **kwargs):
        calls.append(args)
        raise AssertionError("no process may start")

    monkeypatch.setattr(launch.subprocess, "Popen", refuse_to_spawn)
    return calls


@pytest.mark.parametrize(
    ("facts", "code", "reason"),
    [
        (darwin_facts(mode="shadow"), "exec.shadow_refused", None),
        (
            darwin_facts(sandbox_exec=False),
            "exec.backend_unavailable",
            "sandbox_exec_missing",
        ),
        (
            darwin_facts(app_sandboxed=True),
            "exec.backend_unavailable",
            "app_sandboxed",
        ),
    ],
)
def test_a_selection_refusal_starts_no_process(spawns, facts, code, reason):
    outcome = launch._run(darwin_policy(), ["/bin/echo"], {}, None, facts=facts)
    assert (outcome.code, outcome.exit_code, outcome.refusal.reason) == (
        code,
        None,
        reason,
    )
    assert outcome.message == outcome.refusal.message
    assert outcome.stdout == b"" and outcome.stderr == b""
    assert spawns == []


def test_a_workspace_holding_the_data_directory_is_refused(spawns):
    policy = loaded(minimal_policy(workspace=HOME), "darwin", DATA, HOME)
    outcome = launch._run(policy, ["/bin/echo"], {}, None, facts=darwin_facts())
    assert outcome.code == "exec.policy_overlaps_data"
    assert outcome.message == MESSAGES["exec.policy_overlaps_data"]
    assert spawns == []


HOSTED = [{"host": "api.example.com", "port": 443}]


def test_a_proxy_that_cannot_start_is_a_launch_failure(spawns, monkeypatch):
    def refuse(self):
        raise OSError("no port")

    monkeypatch.setattr(launch.Proxy, "start", refuse)
    outcome = launch._run(
        darwin_policy(hosts=HOSTED), ["/bin/echo"], {}, None, facts=darwin_facts()
    )
    assert outcome.code == "exec.launch_failed" and spawns == []


def test_a_seatbelt_call_with_hosts_gets_the_proxy_port_and_it_is_closed_after(
    monkeypatch,
):
    seen = {}

    def fake(policy, call, capture, data, log):
        seen["profile"] = call.profile
        seen["env"] = call.env
        return Outcome(None, 0)

    closed = []
    real_close = launch.Proxy.close
    monkeypatch.setattr(launch, "_launch_seatbelt", fake)
    monkeypatch.setattr(
        launch.Proxy, "close", lambda self: (closed.append(1), real_close(self))
    )
    outcome = launch._run(
        darwin_policy(hosts=HOSTED), ["/bin/echo"], {"KEEP": "1"}, None, facts=darwin_facts()
    )
    assert outcome.code is None
    port = int(seen["env"]["HTTPS_PROXY"].rsplit(":", 1)[1])
    assert f'(allow network-outbound (remote ip "localhost:{port}"))' in seen["profile"]
    assert seen["env"]["KEEP"] == "1" and seen["env"]["no_proxy"] == ""
    assert seen["env"]["http_proxy"] == seen["env"]["ALL_PROXY"] == f"http://127.0.0.1:{port}"
    assert closed == [1]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_stop_signal_while_building_the_call_still_closes_the_proxy(
    monkeypatch, platform
):
    closed = []
    real_close = launch.Proxy.close
    monkeypatch.setattr(
        launch.Proxy, "close", lambda self: (closed.append(1), real_close(self))
    )

    def stop(*args, **kwargs):
        raise launch._Stopped(signal.SIGTERM)

    monkeypatch.setattr(launch, f"_{'seatbelt' if platform == 'darwin' else 'linux'}_call", stop)
    policy = (
        darwin_policy(hosts=HOSTED)
        if platform == "darwin"
        else loaded(minimal_policy(hosts=HOSTED))
    )
    facts = darwin_facts() if platform == "darwin" else good_facts()
    with pytest.raises(launch._Stopped):
        launch._run(policy, ["/bin/echo"], {}, None, facts=facts)
    assert closed == [1]


def test_a_linux_call_with_hosts_binds_the_proxy_socket_and_closes_it_after(
    monkeypatch,
):
    seen = {}

    def fake(call, capture, data, log):
        seen["env"] = call.env
        bwrap = call.plan["bwrap"]
        seen["path"] = bwrap[bwrap.index(constants.EGRESS_SOCKET) - 1]
        seen["exists"] = os.path.exists(seen["path"])
        return Outcome(None, 0)

    monkeypatch.setattr(launch, "_launch_linux", fake)
    monkeypatch.setattr(launch.sys, "executable", "/usr/bin/python3")
    outcome = launch._run(
        loaded(minimal_policy(hosts=HOSTED)), ["/bin/echo"], {}, None, facts=good_facts()
    )
    assert outcome.code is None
    assert seen["env"]["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    assert seen["exists"] is True
    assert not os.path.exists(seen["path"])


@pytest.mark.parametrize(
    ("policy", "facts"),
    [
        (Policy.parse(minimal_policy(workspace=WORKSPACE), "darwin"), darwin_facts()),
        (minimal_policy(workspace=WORKSPACE), darwin_facts()),
        (darwin_policy(), good_facts()),
        (darwin_policy(), {"platform": "darwin"}),
        pytest.param(
            loaded(minimal_policy()),
            good_facts(),
            marks=pytest.mark.skipif(
                sys.platform != "darwin",
                reason="a Linux host legitimately starts the linux backend",
            ),
        ),
    ],
    ids=[
        "parse_only",
        "not_a_policy",
        "other_platform_facts",
        "bad_facts",
        "linux_backend",
    ],
)
def test_anything_but_the_seatbelt_backend_is_a_launch_failure(spawns, policy, facts):
    outcome = launch._run(
        policy, ["/bin/echo"], {}, None, facts=facts or darwin_facts()
    )
    assert (outcome.code, outcome.exit_code, outcome.refusal) == (
        "exec.launch_failed",
        None,
        None,
    )
    assert outcome.message == MESSAGES["exec.launch_failed"]
    assert spawns == []


@pytest.mark.parametrize(
    ("argv", "env", "stdin"),
    [
        ([], {}, None),
        ("/bin/echo", {}, None),
        (["/bin/echo", 1], {}, None),
        (["/bin/ec\0ho"], {}, None),
        (["-D", "X=Y", "/bin/echo"], {}, None),
        (["-p", "(version 1)(allow default)", "/bin/echo"], {}, None),
        (["/bin/echo"], {"A": 1}, None),
        (["/bin/echo"], {"": "x"}, None),
        (["/bin/echo"], {"A=B": "x"}, None),
        (["/bin/echo"], {"A": "x\0y"}, None),
        (["/bin/echo"], {LAUNCH_MARKER: "x"}, None),
        (["/bin/echo"], {"DYLD_INSERT_LIBRARIES": "/x.dylib"}, None),
        (["/bin/echo"], [("A", "x")], None),
        (["/bin/echo"], {}, "text"),
    ],
)
def test_a_bad_argument_environment_or_stdin_is_a_launch_failure(
    spawns, argv, env, stdin
):
    outcome = launch._run(darwin_policy(), argv, env, stdin, facts=darwin_facts())
    assert outcome.code == "exec.launch_failed"
    assert spawns == []


def test_a_spawn_failure_is_a_launch_failure_and_removes_the_profile(
    monkeypatch, real_call
):
    policy = real_call()
    created = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append((path, os.stat(path).st_mode & 0o777))
        return fd, path

    def failing_popen(argv, **kwargs):
        status_fd, env_fd = kwargs["pass_fds"]
        assert argv[:6] == [sys.executable, "-I", "-B", "-S", launch.RUN_PY, "rlimit"]
        assert argv[6:18] == [
            "--core",
            "0",
            "--fsize",
            "104857600",
            "--nofile",
            "256",
            "--cpu",
            "31",
            "--status-fd",
            str(status_fd),
            "--env-fd",
            str(env_fd),
        ]
        assert argv[18:21] == ["--", constants.SANDBOX_EXEC, "-f"]
        assert argv[22:] == ["--", "/bin/echo", "hi"]
        assert kwargs["env"] == {}
        assert kwargs["start_new_session"] is True and kwargs["close_fds"] is True
        assert kwargs["cwd"] == policy.document["workspace"]
        with open(argv[21], encoding="utf-8") as handle:
            assert handle.read().startswith("(version 1)\n(deny default)\n")
        raise OSError("no interpreter here")

    monkeypatch.setattr(launch.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(launch.subprocess, "Popen", failing_popen)
    outcome = launch._run(
        policy, ["/bin/echo", "hi"], {"A": "1"}, None, facts=darwin_facts()
    )
    assert outcome.code == "exec.launch_failed"
    assert len(created) == 1 and created[0][1] == 0o600
    assert created[0][0].startswith(policy.loaded_for[1] + os.sep)
    assert not os.path.exists(created[0][0])


def test_the_outcome_message_follows_its_code():
    assert Outcome(None, 0).message is None
    assert Outcome("exec.time_limit", -9).message == MESSAGES["exec.time_limit"]


@dataclass
class FakeResource:
    RLIMIT_CORE: int = 4
    RLIMIT_FSIZE: int = 1
    RLIMIT_NOFILE: int = 8
    RLIMIT_CPU: int = 0
    refuse: int | None = None
    calls: list = field(default_factory=list)

    def setrlimit(self, which, value):
        if which == self.refuse:
            raise ValueError("not allowed to raise maximum limit")
        self.calls.append((which, value))


@dataclass
class FakeSignals:
    SIGPIPE: int = 13
    SIGXFSZ: int = 25
    SIG_DFL: int = 0
    calls: list = field(default_factory=list)

    def signal(self, signum, handler):
        self.calls.append((signum, handler))


def trampoline_args(status_fd, env_fd, *command, **overrides):
    nproc = overrides.pop("nproc", None)
    values = {"core": 0, "fsize": 4096, "nofile": 32, "cpu": 5, **overrides}
    args = []
    for name, value in values.items():
        args += [f"--{name}", str(value)]
    args += ["--status-fd", str(status_fd), "--env-fd", str(env_fd)]
    if nproc is not None:
        args += ["--nproc", str(nproc)]
    return [*args, "--", *command]


def env_pipe(data):
    read_end, write_end = os.pipe()
    os.write(write_end, data)
    os.close(write_end)
    return read_end


def run_rlimit(env_data, *command, api=None, **overrides):
    status_read, status_write = os.pipe()
    env_read = env_pipe(env_data)
    executed = []
    api = api or FakeResource()
    signals = FakeSignals()
    try:
        code = run_command.rlimit(
            trampoline_args(status_write, env_read, *command, **overrides),
            api=api,
            signals=signals,
            execve=lambda path, argv, env: executed.append((path, argv, env)),
        )
        inheritable = os.get_inheritable(status_write)
    finally:
        os.close(status_write)
    with os.fdopen(status_read, "rb") as handle:
        status = handle.read()
    return code, status, executed, api.calls, signals.calls, inheritable


def test_the_trampoline_sets_every_limit_restores_signals_and_execs_with_the_env():
    code, status, executed, limits, signals, inheritable = run_rlimit(
        b'{"A": "1"}', "/usr/bin/sandbox-exec", "-f", "p"
    )
    assert (code, status, inheritable) == (0, run_command.READY, False)
    assert run_command.READY == constants.TRAMPOLINE_READY
    assert executed == [
        ("/usr/bin/sandbox-exec", ["/usr/bin/sandbox-exec", "-f", "p"], {"A": "1"})
    ]
    assert limits == [(4, (0, 0)), (1, (4096, 4096)), (8, (32, 32)), (0, (5, 5))]
    assert signals == [(13, 0), (25, 0)]


@pytest.mark.parametrize(
    ("env_data", "command", "refuse"),
    [
        (b'{"A": "1"}', (), None),
        (b"{", ("/bin/x",), None),
        (b'["A"]', ("/bin/x",), None),
        (b'{"A": 1}', ("/bin/x",), None),
        (b"{}", ("/bin/x",), 8),
    ],
    ids=["no_command", "bad_json", "not_an_object", "not_strings", "limit_refused"],
)
def test_the_trampoline_reports_a_failure_and_never_execs(env_data, command, refuse):
    code, status, executed, _limits, _signals, _inheritable = run_rlimit(
        env_data, *command, api=FakeResource(refuse=refuse)
    )
    assert code == 1 and status.startswith(b"fail ") and executed == []


def test_the_trampoline_refuses_a_negative_limit_before_anything_else(capsys):
    with pytest.raises(SystemExit):
        run_command.rlimit(trampoline_args(1, 2, "/bin/x", nofile=-1))
    assert "non-negative" in capsys.readouterr().err


def real_trampoline(command, env, prefix=(), **overrides):
    status_read, status_write = os.pipe()
    env_read, env_write = os.pipe()
    os.write(env_write, json.dumps(env).encode())
    os.close(env_write)
    args = trampoline_args(status_write, env_read, *command, **overrides)
    proc = subprocess.Popen(
        [*prefix, PYTHON, "-I", "-B", "-S", launch.RUN_PY, "rlimit", *args],
        env={},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(status_write, env_read),
        start_new_session=True,
    )
    os.close(status_write)
    os.close(env_read)
    out, err = proc.communicate(timeout=120)
    with os.fdopen(status_read, "rb") as handle:
        status = handle.read()
    return proc.returncode, status, out, err


REPORT_LIMITS = (
    "import json, resource\n"
    "names = ('CORE', 'FSIZE', 'NOFILE', 'CPU')\n"
    "print(json.dumps([resource.getrlimit(getattr(resource, 'RLIMIT_' + n))"
    " for n in names]))\n"
)


def test_the_real_trampoline_applies_its_limits_and_env_through_exec():
    code, status, out, err = real_trampoline(
        [PYTHON, "-I", "-c", REPORT_LIMITS], {}, fsize=8192, nofile=48, cpu=9
    )
    assert (code, status) == (0, run_command.READY), err
    assert json.loads(out) == [[0, 0], [8192, 8192], [48, 48], [9, 9]]
    code, status, out, err = real_trampoline(["/usr/bin/env"], {"A": "1", "B": "x y"})
    assert (code, status, out) == (0, run_command.READY, b"A=1\nB=x y\n"), err


def test_the_real_trampoline_refuses_a_limit_the_kernel_rejects():
    lowered = ["/bin/sh", "-c", 'ulimit -n 64 && exec "$@"', "sh"]
    code, status, out, _err = real_trampoline(
        ["/bin/echo", "ran"], {}, prefix=lowered, nofile=128
    )
    assert (code, status, out) == (1, b"fail ValueError", b"")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the rlimit trampoline is the macOS launcher, and Linux ends a tool at its hard CPU limit with SIGKILL instead of SIGXCPU",
)
def test_the_cpu_limit_kills_a_busy_tool_without_any_launcher():
    code, status, _out, _err = real_trampoline(
        [PYTHON, "-I", "-c", "while True: pass"], {}, cpu=1
    )
    assert (code, status) == (-signal.SIGXCPU, run_command.READY)


@dataclass
class Proc:
    start: int
    ppid: int
    pgid: int
    footprint: int = 0
    exited: bool = False
    strings: list = field(default_factory=list)


class FakeSystem:
    def __init__(self, table):
        self.table = table
        self.killed = []
        self.groups = []
        self.unkillable = set()
        self.args_reads = 0

    def pids(self, kind, info):
        if kind == PROC_PGRP_ONLY:
            return [pid for pid, proc in self.table.items() if proc.pgid == info]
        if kind == PROC_PPID_ONLY:
            return [pid for pid, proc in self.table.items() if proc.ppid == info]
        assert kind == PROC_UID_ONLY
        return list(self.table)

    def usage(self, pid):
        proc = self.table.get(pid)
        return None if proc is None else Usage(proc.start, proc.exited, proc.footprint)

    def strings(self, pid):
        self.args_reads += 1
        proc = self.table.get(pid)
        return None if proc is None or proc.exited else proc.strings

    def kill(self, pid, signum):
        assert signum == signal.SIGKILL
        self.killed.append(pid)
        if pid in self.unkillable:
            raise PermissionError(pid)
        if pid not in self.table:
            raise ProcessLookupError(pid)
        self.table[pid].exited = True

    def killpg(self, pgid, signum):
        assert signum == signal.SIGKILL
        self.groups.append(pgid)
        members = [p for p, proc in self.table.items() if proc.pgid == pgid]
        if not members:
            raise ProcessLookupError(pgid)
        for pid in members:
            if pid not in self.unkillable:
                self.table[pid].exited = True


MARK = f"{LAUNCH_MARKER}=feedface".encode()


def watch(system, marker=None):
    return Watch(100, marker, system=system, kill=system.kill, killpg=system.killpg)


def test_the_watch_counts_and_sums_the_group_and_every_descendant():
    system = FakeSystem(
        {
            100: Proc(1, 1, 100, 1000),
            101: Proc(2, 100, 100, 2000),
            102: Proc(3, 101, 102, 4000),
            103: Proc(4, 102, 102, 0, exited=True),
            200: Proc(5, 1, 200, 99999),
        }
    )
    assert watch(system).sample() == (4, 7000)


def test_a_seen_escapee_stays_tracked_and_a_reused_pid_is_dropped():
    system = FakeSystem({100: Proc(1, 1, 100), 101: Proc(2, 100, 101, 10)})
    tracked = watch(system)
    assert tracked.sample() == (2, 10)
    system.table[101].ppid = 1
    assert tracked.sample() == (2, 10)
    system.table[101] = Proc(9, 1, 101, 10)
    assert tracked.sample() == (1, 0)


def test_marked_processes_join_only_when_the_call_may_fork():
    table = {100: Proc(1, 1, 100), 300: Proc(7, 1, 300, 50, strings=[b"/x", MARK])}
    table[301] = Proc(8, 1, 301, 70, strings=[b"/y", MARK + b"0"])
    assert watch(FakeSystem(dict(table))).sample() == (1, 0)
    system = FakeSystem(dict(table))
    marked = watch(system, MARK)
    assert marked.sample() == (2, 50)
    reads = system.args_reads
    assert marked.sample() == (2, 50)
    assert system.args_reads == reads


def test_the_end_kills_the_group_every_tracked_pid_and_every_marked_pid():
    system = FakeSystem(
        {
            100: Proc(1, 1, 100),
            101: Proc(2, 100, 100),
            102: Proc(3, 1, 102),
            300: Proc(7, 1, 300, strings=[MARK]),
            os.getpid(): Proc(8, 1, 1, strings=[MARK]),
        }
    )
    tracked = watch(system, MARK)
    system.table[102].ppid = 100
    tracked.sample()
    system.table[102].ppid = 1
    assert tracked.end() == set()
    assert system.groups[0] == 100
    assert system.table[102].exited and system.table[300].exited
    assert not system.table[os.getpid()].exited
    assert os.getpid() not in system.killed


def test_the_end_reports_survivors_it_cannot_kill():
    system = FakeSystem({100: Proc(1, 1, 100), 101: Proc(2, 100, 100)})
    system.unkillable.add(101)
    assert watch(system).end() == {101}


def test_a_reused_root_pid_is_neither_signalled_as_a_group_nor_listed():
    system = FakeSystem({100: Proc(1, 1, 100), 101: Proc(2, 100, 100)})
    tracked = watch(system)
    del system.table[101]
    system.table[100] = Proc(9, 1, 100)
    system.table[150] = Proc(10, 100, 100)
    assert tracked.sample() == (0, 0)
    assert tracked.end() == set()
    assert system.groups == [] and system.killed == []


def test_procargs_strings_skip_argc_and_padding():
    raw = b"\x02\x00\x00\x00/bin/sleep\x00\x00\x00/bin/sleep\x005\x00A=1\x00\x00"
    assert procargs_strings(raw) == [b"/bin/sleep", b"/bin/sleep", b"5", b"A=1"]
    assert procargs_strings(b"\x01") == []


class FakeWatch:
    def __init__(self, samples=(), fail=False):
        self.samples = list(samples)
        self.fail = fail
        self.ended = 0
        self.root = None

    def __call__(self, root, marker):
        self.root = root
        self.marker = marker
        return self

    def sample(self):
        if self.fail:
            raise OSError("libproc failed")
        return self.samples.pop(0) if self.samples else (1, 0)

    def end(self):
        self.ended += 1
        try:
            os.killpg(self.root, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return set()


def spawn(code, stdin=False):
    return subprocess.Popen(
        [PYTHON, "-I", "-B", "-c", code],
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


LIMITS = {
    "memory_bytes": 1 << 30,
    "processes": 4,
    "open_files": 64,
    "file_bytes": 1 << 20,
}


def ready_fds(status=b"exec"):
    env_read, env_write = os.pipe()
    os.close(env_read)
    return env_pipe(status), env_write


SUPERVISE_DIGEST = "sha256:feedfed"


def supervise(proc, fake, wall_ms=60000, data=None, limits=LIMITS, status=b"exec"):
    status_fd, env_fd = ready_fds(status)
    return launch._supervise(
        proc,
        status_fd,
        env_fd,
        {"A": "1"},
        limits,
        wall_ms,
        "feedface",
        data,
        fake,
        log=launch._log(SUPERVISE_DIGEST),
    )


def test_the_monitor_returns_the_exit_and_output_of_a_finished_child():
    fake = FakeWatch()
    proc = spawn(
        "import sys; sys.stdout.write(sys.stdin.read()); sys.stderr.write('e'); sys.exit(3)",
        stdin=True,
    )
    outcome = supervise(proc, fake, data=b"in")
    assert outcome == Outcome(None, 3, b"in", b"e")
    assert fake.ended == 1 and fake.marker == MARK


def test_the_monitor_passes_no_marker_when_the_call_cannot_fork():
    fake = FakeWatch()
    supervise(spawn("pass"), fake, limits={**LIMITS, "processes": 1})
    assert fake.marker is None


@pytest.mark.parametrize(
    ("samples", "wall_ms", "code"),
    [
        ([], 1, "exec.time_limit"),
        ([(1, (1 << 30) + 1)], 60000, "exec.memory_limit"),
        ([(5, 0)], 60000, "exec.process_limit"),
    ],
)
def test_the_monitor_kills_the_group_on_a_limit(samples, wall_ms, code):
    fake = FakeWatch(samples)
    outcome = supervise(spawn("import time; time.sleep(600)"), fake, wall_ms)
    assert (outcome.code, outcome.exit_code) == (code, -signal.SIGKILL)
    assert fake.ended == 1


def test_a_watch_failure_is_a_launch_failure_and_still_ends_the_call():
    fake = FakeWatch(fail=True)
    outcome = supervise(spawn("import time; time.sleep(600)"), fake)
    assert outcome.code == "exec.launch_failed" and fake.ended == 1


def test_a_watch_that_cannot_start_still_kills_the_group():
    def broken(root, marker):
        raise OSError("no libproc")

    status_fd, env_fd = ready_fds()
    outcome = launch._supervise(
        spawn("import time; time.sleep(600)"),
        status_fd,
        env_fd,
        {},
        LIMITS,
        60000,
        "f",
        None,
        broken,
    )
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", -signal.SIGKILL)


def test_a_file_size_signal_is_a_file_limit():
    code = (
        "import os, signal\n"
        "signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
        "os.kill(os.getpid(), signal.SIGXFSZ)\n"
    )
    outcome = supervise(spawn(code), FakeWatch())
    assert (outcome.code, outcome.exit_code) == ("exec.file_limit", -signal.SIGXFSZ)


STRAY = (
    "import os, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],"
    " start_new_session={escape})\n"
    "print(child.pid, flush=True)\n"
)


def test_a_stray_holding_the_output_is_ended_when_the_root_exits():
    outcome = supervise(spawn(STRAY.format(escape=False)), FakeWatch())
    stray = int(outcome.stdout)
    assert outcome.code is None and outcome.exit_code == 0
    assert _gone(stray)


def test_the_drain_gives_up_on_an_escaped_stray_holding_the_output(monkeypatch):
    monkeypatch.setattr(launch, "DRAIN_SECONDS", 0.2)
    outcome = supervise(spawn(STRAY.format(escape=True)), FakeWatch())
    stray = int(outcome.stdout)
    try:
        assert outcome.code is None and outcome.exit_code == 0
    finally:
        os.kill(stray, signal.SIGKILL)


def _running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform == "darwin":
        usage = Darwin().usage(pid)
        return usage is not None and not usage.exited
    with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
        return handle.read().rsplit(")", 1)[1].split()[0] != "Z"


def _gone(pid):
    for _ in range(500):
        if not _running(pid):
            return True
        time.sleep(0.01)
    return False


def test_run_py_dispatches_each_command(monkeypatch, capsys):
    from astral_exec import conformance

    seen = []
    monkeypatch.setattr(launch, "main", lambda args: seen.append(args) or 7)
    assert run_command.main(["launch", "--policy", "p"]) == 7
    assert seen == [["--policy", "p"]]
    monkeypatch.setattr(conformance, "main", lambda args: seen.append(args) or 5)
    assert run_command.main(["conformance", "--json"]) == 5
    assert seen[-1] == ["--json"]
    with pytest.raises(SystemExit):
        run_command.main(["rlimit", "--core", "0"])
    for args in ([], ["other"]):
        assert run_command.main(args) == 2
        assert "usage" in capsys.readouterr().err


def _write(path, data):
    path.write_text(data, encoding="utf-8")
    return str(path)


@pytest.fixture
def cli(tmp_path):
    data = (tmp_path / "data").resolve()
    workspace = data / "exec" / "call"
    workspace.mkdir(parents=True)
    home = (tmp_path / "home").resolve()
    home.mkdir()
    policy = _write(
        tmp_path / "policy.json", json.dumps(minimal_policy(workspace=str(workspace)))
    )
    env = _write(tmp_path / "env.json", json.dumps({"A": "1"}))
    return {
        "policy": policy,
        "data": str(data),
        "home": str(home),
        "env": env,
        "tmp": tmp_path,
    }


def launch_args(cli, **overrides):
    values = {
        "policy": cli["policy"],
        "data-dir": cli["data"],
        "home": cli["home"],
        "env-file": cli["env"],
    }
    values.update(overrides)
    args = []
    for name, value in values.items():
        args += [f"--{name}", value]
    return args


def _refusal(capsys):
    line = capsys.readouterr().err.strip().splitlines()[-1]
    return json.loads(line)


def test_launch_main_maps_the_outcome_to_an_exit_status(monkeypatch, cli, capsys):
    received = []

    def fake_launch(policy, argv, env, stdin, facts, capture):
        received.append((policy.loaded_for[1], argv, env, stdin, capture))
        return outcomes.pop(0)

    outcomes = [
        Outcome(None, 3),
        Outcome(None, -signal.SIGKILL),
        Outcome("exec.time_limit", -signal.SIGKILL),
    ]
    monkeypatch.setattr(launch, "_launch", fake_launch)
    before = {sig: signal.getsignal(sig) for sig in launch.STOP_SIGNALS}
    assert launch.main([*launch_args(cli), "--", "/bin/echo", "hi"]) == 3
    assert received[0] == (cli["data"], ["/bin/echo", "hi"], {"A": "1"}, None, False)
    assert launch.main([*launch_args(cli), "--", "/bin/echo"]) == 128 + signal.SIGKILL
    capsys.readouterr()
    assert launch.main([*launch_args(cli), "--", "/bin/echo"]) == REFUSED_EXIT_STATUS
    assert _refusal(capsys) == {
        "code": "exec.time_limit",
        "message": MESSAGES["exec.time_limit"],
    }
    assert {sig: signal.getsignal(sig) for sig in launch.STOP_SIGNALS} == before


def test_launch_main_returns_the_signal_status_when_stopped(monkeypatch, cli):
    def stopped(*args):
        raise launch._Stopped(signal.SIGTERM)

    monkeypatch.setattr(launch, "_launch", stopped)
    assert launch.main([*launch_args(cli), "--", "/bin/echo"]) == 128 + signal.SIGTERM
    saved = {sig: signal.getsignal(sig) for sig in launch.STOP_SIGNALS}
    try:
        with pytest.raises(launch._Stopped):
            launch._stop(signal.SIGHUP, None)
        assert all(
            signal.getsignal(sig) == signal.SIG_IGN for sig in launch.STOP_SIGNALS
        )
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def test_launch_main_refuses_bad_inputs_before_any_launch(monkeypatch, cli, capsys):
    monkeypatch.setattr(launch, "_launch", lambda *a: pytest.fail("launched"))
    bad_policy = _write(cli["tmp"] / "bad.json", "{")
    assert launch.main([*launch_args(cli, policy=bad_policy), "--", "/bin/echo"]) == (
        REFUSED_EXIT_STATUS
    )
    assert _refusal(capsys)["code"] == "policy.malformed"
    missing = str(cli["tmp"] / "missing")
    assert launch.main([*launch_args(cli, **{"data-dir": missing}), "--", "x"]) == (
        REFUSED_EXIT_STATUS
    )
    assert _refusal(capsys)["code"] == "exec.launch_failed"
    for env in ("[]", '{"A": 1}'):
        env_file = _write(cli["tmp"] / "env-bad.json", env)
        assert (
            launch.main([*launch_args(cli, **{"env-file": env_file}), "--", "x"])
            == REFUSED_EXIT_STATUS
        )
        assert _refusal(capsys)["code"] == "exec.launch_failed"
    overlap = _write(
        cli["tmp"] / "overlap.json",
        json.dumps(minimal_policy(workspace=str(cli["tmp"].resolve()))),
    )
    assert launch.main([*launch_args(cli, policy=overlap), "--", "x"]) == (
        REFUSED_EXIT_STATUS
    )
    assert _refusal(capsys)["code"] == "exec.policy_overlaps_data"


def test_launch_main_needs_the_separator_and_a_tool(cli, capsys):
    assert launch.main(launch_args(cli)) == 2
    assert launch.main([*launch_args(cli), "--"]) == 2
    assert "usage" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        launch.main(["--policy", cli["policy"], "--", "/bin/echo"])


class Symbol:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class FakeLib:
    def __init__(self, listings, usages, args):
        self.listings = listings
        self.usages = usages
        self.args = args
        self.proc_listpids = Symbol(self._listpids)
        self.proc_pid_rusage = Symbol(self._rusage)
        self.sysctl = Symbol(self._sysctl)

    def _listpids(self, kind, info, buffer, size):
        values = self.listings.get((kind, info))
        if values is None:
            return -1
        if buffer is None:
            return 0
        count = min(len(values), size // 4)
        for index in range(count):
            buffer[index] = values[index]
        return 4 * count

    def _rusage(self, pid, flavor, ref):
        if pid not in self.usages:
            return -1
        info = ref._obj
        start, exit_time, footprint = self.usages[pid]
        info.ri_proc_start_abstime = start
        info.ri_proc_exit_abstime = exit_time
        info.ri_phys_footprint = footprint
        return 0

    def _sysctl(self, mib, length, out, size, new, new_length):
        name = tuple(mib[index] for index in range(length))
        if name == (CTL_KERN, KERN_ARGMAX):
            out._obj.value = 64
            size._obj.value = 4
            return 0
        data = (
            self.args.get(name[2]) if name[:2] == (CTL_KERN, KERN_PROCARGS2) else None
        )
        if data is None:
            return -1
        ctypes.memmove(out._obj, data, len(data))
        size._obj.value = len(data)
        return 0


def test_the_darwin_reader_grows_its_buffer_and_reads_usage_and_arguments():
    many = list(range(1, 301))
    lib = FakeLib(
        {(PROC_UID_ONLY, 501): many, (PROC_PPID_ONLY, 7): [8, 0, 9]},
        {7: (11, 0, 4096), 8: (12, 13, 0)},
        {7: b"\x01\x00\x00\x00/bin/x\x00\x00/bin/x\x00A=1\x00"},
    )
    system = Darwin(lib)
    assert system.pids(PROC_UID_ONLY, 501) == many
    assert system.pids(PROC_PPID_ONLY, 7) == [8, 9]
    assert system.usage(7) == Usage(11, False, 4096)
    assert system.usage(8) == Usage(12, True, 0)
    assert system.usage(9) is None
    assert system.strings(7) == [b"/bin/x", b"/bin/x", b"A=1"]
    assert system.strings(8) is None
    with pytest.raises(OSError):
        system.pids(PROC_PGRP_ONLY, 7)


def test_teardown_fails_closed_unless_every_process_is_gone(monkeypatch, caplog):
    proc = spawn("pass")
    proc.wait()
    proc.returncode = None
    groups = []

    def gone_group(pgid, signum):
        groups.append(pgid)
        raise PermissionError(pgid)

    def broken_end():
        raise OSError("libproc failed")

    log = launch._log("sha256:x")
    monkeypatch.setattr(launch.os, "killpg", gone_group)
    assert launch._end(proc, None, log) is False
    watch = FakeWatch()
    watch.end = broken_end
    assert launch._end(proc, watch, log) is False
    assert groups == [proc.pid, proc.pid]
    watch.end = lambda: {proc.pid}
    with caplog.at_level("WARNING", logger="astral_exec.launch"):
        assert launch._end(proc, watch, log) is False
    assert "survived teardown count=1" in caplog.text
    watch.end = set
    assert launch._end(proc, watch, log) is True
    assert groups == [proc.pid, proc.pid]
    launch._remove(str(tempfile.gettempdir()) + "/astral-exec-missing.sb")


def test_a_failed_teardown_turns_a_finished_call_into_a_launch_failure():
    fake = FakeWatch()
    fake.end = lambda: {1}
    outcome = supervise(spawn("pass"), fake)
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", 0)


def test_run_py_runs_as_a_script_with_only_its_parent_on_the_path(monkeypatch, capsys):
    script = os.path.join(os.path.dirname(launch.__file__), "run.py")
    monkeypatch.setattr(sys, "argv", [script, "conformance", "--backend", "container"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(script, run_name="__main__")
    assert stopped.value.code == 2
    assert sys.path[0] == os.path.dirname(os.path.dirname(script))
    assert "container" in capsys.readouterr().err


def test_a_cpu_limit_signal_is_a_time_limit():
    code = "import os, signal\nos.kill(os.getpid(), signal.SIGXCPU)\n"
    outcome = supervise(spawn(code), FakeWatch())
    assert (outcome.code, outcome.exit_code) == ("exec.time_limit", -signal.SIGXCPU)


def test_a_trampoline_that_never_got_ready_is_a_launch_failure():
    outcome = supervise(spawn("import sys; sys.exit(1)"), FakeWatch(), status=b"fail X")
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", None)
    outcome = supervise(spawn("pass"), FakeWatch(), status=b"")
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", None)


def test_output_past_the_cap_is_drained_kept_to_the_cap_and_flagged(monkeypatch):
    monkeypatch.setattr(launch, "OUTPUT_LIMIT_BYTES", 1000)
    code = (
        "import sys\nsys.stdout.write('o' * 300000)\nsys.stdout.flush()\n"
        "sys.stderr.write('e' * 999)\n"
    )
    outcome = supervise(spawn(code), FakeWatch())
    assert (outcome.code, outcome.exit_code) == (None, 0)
    assert outcome.stdout == b"o" * 1000 and outcome.stderr == b"e" * 999
    assert outcome.truncated == ("stdout",)


def test_large_stdin_and_env_reach_a_tool_that_reads_slowly():
    data = b"x" * 300000
    code = "import sys, time\ntime.sleep(0.2)\nprint(len(sys.stdin.buffer.read()))"
    outcome = supervise(spawn(code, stdin=True), FakeWatch(), data=data)
    assert (outcome.code, outcome.stdout) == (None, b"300000\n")


def test_a_tool_that_ignores_stdin_still_finishes():
    outcome = supervise(spawn("pass", stdin=True), FakeWatch(), data=b"x" * 300000)
    assert (outcome.code, outcome.exit_code) == (None, 0)


def test_a_stream_setup_failure_is_a_launch_failure(monkeypatch):
    def broken(*args):
        raise OSError("no selector")

    monkeypatch.setattr(launch, "_Streams", broken)
    outcome = supervise(spawn("import time; time.sleep(600)"), FakeWatch())
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", None)


def test_a_relative_interpreter_is_a_launch_failure(spawns, monkeypatch, real_call):
    policy = real_call()
    monkeypatch.setattr(launch.sys, "executable", "python3")
    outcome = launch._run(policy, ["/bin/echo"], {}, None, facts=darwin_facts())
    assert outcome.code == "exec.launch_failed" and spawns == []


def test_empty_stdin_closes_the_tool_input_at_once():
    code = "import sys\nprint(len(sys.stdin.read()))"
    outcome = supervise(spawn(code, stdin=True), FakeWatch(), data=b"")
    assert (outcome.code, outcome.stdout) == (None, b"0\n")


def test_a_call_with_inherited_stdio_waits_on_the_process():
    proc = subprocess.Popen(
        [PYTHON, "-I", "-B", "-c", "import time; time.sleep(0.1)"],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    outcome = supervise(proc, FakeWatch())
    assert outcome == Outcome(None, 0)


def test_the_digest_log_prefixes_every_message():
    adapter = launch._log("sha256:abc")
    assert adapter.process("hello %s", {}) == ("digest=sha256:abc hello %s", {})


def test_every_emitted_launch_line_carries_the_digest(real_call, caplog):
    policy = real_call()
    with caplog.at_level(logging.INFO, logger="astral_exec.launch"):
        launch._run(policy, ["/bin/echo", "hi"], {}, None, facts=darwin_facts())
        launch._run(policy, ["-bad"], {}, None, facts=darwin_facts())
        launch._run(policy, ["/bin/echo"], {}, None, facts=darwin_facts(mode="shadow"))
    assert len(caplog.records) >= 3
    for record in caplog.records:
        assert record.getMessage().startswith("digest=sha256:"), record.getMessage()


def test_a_watch_over_an_invisible_root_is_not_started_but_still_tears_down():
    tracked = watch(FakeSystem({}))
    assert tracked.started is False
    assert tracked.end() == set()


class ExitedWatch:
    def __init__(self):
        self.started = False
        self.root = None

    def __call__(self, root, marker):
        self.root = root
        return self

    def end(self):
        try:
            os.killpg(self.root, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return set()


def test_an_unsampleable_live_child_fails_closed():
    outcome = supervise(spawn("import time; time.sleep(600)"), ExitedWatch())
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", -signal.SIGKILL)


def test_a_sandbox_exec_failure_is_a_launch_failure_not_a_tool_exit():
    code = "import sys\nsys.stderr.write('sandbox-exec: bad profile')\nsys.exit(65)"
    outcome = supervise(spawn(code), FakeWatch())
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", None)
    assert b"sandbox-exec:" in outcome.stderr


def test_a_plain_tool_exit_of_71_without_the_prefix_stays_a_tool_exit():
    outcome = supervise(spawn("import sys; sys.exit(71)"), FakeWatch())
    assert (outcome.code, outcome.exit_code) == (None, 71)


def test_a_stop_signal_during_teardown_still_finishes_the_sweep():
    previous = {sig: signal.signal(sig, launch._stop) for sig in launch.STOP_SIGNALS}
    proc = spawn("import time; time.sleep(600)")
    fake = FakeWatch()
    group_end = fake.end

    def end_with_a_signal():
        os.kill(os.getpid(), signal.SIGTERM)
        return group_end()

    fake.end = end_with_a_signal
    try:
        with pytest.raises(launch._Stopped):
            supervise(proc, fake, wall_ms=200)
        assert proc.wait(timeout=30) == -signal.SIGKILL
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_the_command_adds_an_nproc_backstop_only_when_fork_is_allowed(
    monkeypatch, real_call
):
    seen = []

    def record_popen(argv, **kwargs):
        seen.append(argv)
        raise OSError("stop")

    monkeypatch.setattr(launch, "user_process_count", lambda: 100)
    monkeypatch.setattr(launch.subprocess, "Popen", record_popen)
    launch._run(real_call(processes=2), ["/bin/echo"], {}, None, facts=darwin_facts())
    argv = seen[-1]
    assert "--nproc" in argv and argv[argv.index("--nproc") + 1] == "134"
    launch._run(real_call(processes=1), ["/bin/echo"], {}, None, facts=darwin_facts())
    assert "--nproc" not in seen[-1]


def test_the_profile_is_written_under_the_data_directory_outside_exec(
    monkeypatch, real_call
):
    seen = []
    real_mkstemp = tempfile.mkstemp

    def recording(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        seen.append(path)
        return fd, path

    monkeypatch.setattr(launch.tempfile, "mkstemp", recording)
    monkeypatch.setattr(
        launch.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
    )
    policy = real_call()
    launch._run(policy, ["/bin/echo"], {}, None, facts=darwin_facts())
    data = policy.loaded_for[1]
    assert len(seen) == 1 and seen[0].startswith(data + os.sep)
    relative = os.path.relpath(seen[0], data).split(os.sep)
    assert relative[0] == launch.PROFILE_SUBDIR and relative[0] != "exec"
    assert not os.path.exists(seen[0])


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the nproc backstop and its libproc process count exist only on the macOS launcher",
)
def test_a_fork_loop_cannot_grow_past_the_nproc_backstop():
    base = watchdog.user_process_count()
    code = (
        "import os\nmade = 0\n"
        "try:\n"
        "    for _ in range(200):\n"
        "        if os.fork() == 0:\n"
        "            import time\n"
        "            time.sleep(1)\n"
        "            os._exit(0)\n"
        "        made += 1\n"
        "except OSError:\n"
        "    pass\n"
        "print(made)\n"
    )
    code_rc, status, out, err = real_trampoline(
        [PYTHON, "-I", "-c", code], {}, nproc=base + 25
    )
    assert status == run_command.READY, err
    assert 0 < int(out) < 200


def test_every_supervise_warning_line_carries_the_digest(caplog):
    with caplog.at_level(logging.WARNING, logger="astral_exec.launch"):
        supervise(spawn("import time; time.sleep(600)"), FakeWatch(fail=True))
        survivor = FakeWatch()
        survivor.end = lambda: {1234}
        supervise(spawn("pass"), survivor)
        refused = FakeWatch()
        supervise(
            spawn("import sys; sys.stderr.write('sandbox-exec: no'); sys.exit(65)"),
            refused,
        )
    messages = [record.getMessage() for record in caplog.records]
    assert any("monitor failed" in m for m in messages)
    assert any("survived teardown" in m for m in messages)
    assert messages
    for message in messages:
        assert message.startswith("digest=" + SUPERVISE_DIGEST), message


def test_a_drain_warning_line_carries_the_digest(monkeypatch, caplog):
    monkeypatch.setattr(launch, "DRAIN_SECONDS", 0.05)
    code = (
        "import os, subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'],"
        " stdout=sys.stdout, start_new_session=True)\n"
    )
    with caplog.at_level(logging.WARNING, logger="astral_exec.launch"):
        supervise(spawn(code), FakeWatch())
    stuck = [r.getMessage() for r in caplog.records if "stayed open" in r.getMessage()]
    assert stuck and all(m.startswith("digest=" + SUPERVISE_DIGEST) for m in stuck)


def test_a_child_that_will_not_reap_does_not_hang_teardown(monkeypatch, caplog):
    proc = spawn("pass")

    def never(timeout=None):
        raise subprocess.TimeoutExpired(proc.args, timeout or 0)

    monkeypatch.setattr(proc, "wait", never)
    with caplog.at_level(logging.WARNING, logger="astral_exec.launch"):
        outcome = supervise(proc, FakeWatch())
    assert "did not exit after teardown" in caplog.text
    assert outcome.code == "exec.launch_failed"
    monkeypatch.undo()
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def test_a_child_that_exits_before_the_first_sample_is_a_normal_exit():
    proc = spawn("import sys; sys.exit(7)")
    proc.wait()
    outcome = supervise(proc, ExitedWatch())
    assert (outcome.code, outcome.exit_code) == (None, 7)


def _make_folder(tmp_path, kind):
    data = (tmp_path / "data").resolve()
    data.mkdir()
    folder = data / launch.PROFILE_SUBDIR
    if kind == "symlink":
        (tmp_path / "elsewhere").mkdir()
        folder.symlink_to(tmp_path / "elsewhere")
    elif kind == "loose":
        folder.mkdir(mode=0o755)
        folder.chmod(0o755)
    elif kind == "file":
        folder.write_text("not a dir")
    return str(data)


@pytest.mark.parametrize("kind", ["symlink", "loose", "file"])
def test_a_tampered_profile_folder_is_refused(tmp_path, kind):
    data = _make_folder(tmp_path, kind)
    with pytest.raises(ContextError):
        launch._write_profile("(version 1)\n", data)


def test_a_foreign_owned_profile_folder_is_refused(tmp_path, monkeypatch):
    data = str((tmp_path / "data").resolve())
    os.makedirs(data)
    stranger = os.geteuid() + 1
    monkeypatch.setattr(launch.os, "geteuid", lambda: stranger)
    with pytest.raises(ContextError):
        launch._write_profile("(version 1)\n", data)


def test_a_fresh_profile_folder_is_created_private_and_used(tmp_path):
    data = str((tmp_path / "data").resolve())
    os.makedirs(data)
    path = launch._write_profile("(version 1)\n", data)
    folder = os.path.join(data, launch.PROFILE_SUBDIR)
    assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert path.startswith(folder + os.sep)
    launch._remove(path)


def test_a_write_failure_removes_the_profile_file(monkeypatch, tmp_path):
    data = str((tmp_path / "data").resolve())
    os.makedirs(data)
    seen = []
    real_mkstemp = tempfile.mkstemp

    def recording(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        seen.append((fd, path))
        return fd, path

    def failing_fdopen(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(launch.tempfile, "mkstemp", recording)
    monkeypatch.setattr(launch.os, "fdopen", failing_fdopen)
    with pytest.raises(OSError):
        launch._write_profile("(version 1)\n", data)
    ((fd, path),) = seen
    assert not os.path.exists(path)
    with pytest.raises(OSError):
        os.fstat(fd)


def test_the_fallback_group_kill_skips_a_child_already_reaped(monkeypatch):
    proc = spawn("pass")
    proc.wait()
    groups = []
    monkeypatch.setattr(launch.os, "killpg", lambda pgid, signum: groups.append(pgid))
    assert launch._end(proc, None, launch._log("sha256:x")) is False
    assert groups == []


def test_a_failed_write_after_fdopen_closes_the_descriptor_only_once(
    monkeypatch, tmp_path
):
    data = str((tmp_path / "data").resolve())
    os.makedirs(data)
    seen = []
    closed = []
    real_mkstemp = tempfile.mkstemp
    real_fdopen = os.fdopen
    real_close = os.close

    class FailingWrite:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()
            return False

        def write(self, text):
            raise OSError("disk full")

    def recording(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        seen.append((fd, path))
        return fd, path

    def recording_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(launch.tempfile, "mkstemp", recording)
    monkeypatch.setattr(
        launch.os, "fdopen", lambda *a, **k: FailingWrite(real_fdopen(*a, **k))
    )
    monkeypatch.setattr(launch.os, "close", recording_close)
    with pytest.raises(OSError, match="disk full"):
        launch._write_profile("(version 1)\n", data)
    ((fd, path),) = seen
    assert not os.path.exists(path)
    assert fd not in closed
