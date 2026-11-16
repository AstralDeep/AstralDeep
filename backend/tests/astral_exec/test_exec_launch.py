"""Drives the astral_exec launcher without sandbox-exec: refusals and launch failures start no process, the limits applier, the monitoring loop, the watchdog over an injected process table and the run.py command line.
The confined runs live in test_exec_seatbelt_live.py, and every test here runs on any platform.
"""

from __future__ import annotations

import ctypes
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import pytest
from astral_exec import Policy, constants, launch
from astral_exec import run as run_command
from astral_exec.constants import LAUNCH_MARKER, MESSAGES, REFUSED_EXIT_STATUS
from astral_exec.facts import Facts
from astral_exec.launch import Outcome, apply_limits, run
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
    outcome = run(darwin_policy(), ["/bin/echo"], {}, None, facts=facts)
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
    outcome = run(policy, ["/bin/echo"], {}, None, facts=darwin_facts())
    assert outcome.code == "exec.policy_overlaps_data"
    assert outcome.message == MESSAGES["exec.policy_overlaps_data"]
    assert spawns == []


@pytest.mark.parametrize(
    ("policy", "facts"),
    [
        (Policy.parse(minimal_policy(workspace=WORKSPACE), "darwin"), darwin_facts()),
        (minimal_policy(workspace=WORKSPACE), darwin_facts()),
        (darwin_policy(), good_facts()),
        (darwin_policy(), {"platform": "darwin"}),
        (loaded(minimal_policy()), good_facts()),
        (darwin_policy(hosts=[{"host": "api.example.com", "port": 443}]), None),
    ],
    ids=[
        "parse_only",
        "not_a_policy",
        "other_platform_facts",
        "bad_facts",
        "linux_backend",
        "hosts_without_proxy",
    ],
)
def test_anything_but_the_seatbelt_backend_is_a_launch_failure(spawns, policy, facts):
    outcome = run(policy, ["/bin/echo"], {}, None, facts=facts or darwin_facts())
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
        (["/bin/echo"], {"A": 1}, None),
        (["/bin/echo"], {"": "x"}, None),
        (["/bin/echo"], {"A=B": "x"}, None),
        (["/bin/echo"], {"A": "x\0y"}, None),
        (["/bin/echo"], {LAUNCH_MARKER: "x"}, None),
        (["/bin/echo"], [("A", "x")], None),
        (["/bin/echo"], {}, "text"),
    ],
)
def test_a_bad_argument_environment_or_stdin_is_a_launch_failure(
    spawns, argv, env, stdin
):
    outcome = run(darwin_policy(), argv, env, stdin, facts=darwin_facts())
    assert outcome.code == "exec.launch_failed"
    assert spawns == []


def test_a_spawn_failure_is_a_launch_failure_and_removes_the_profile(monkeypatch):
    created = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append((path, os.stat(path).st_mode & 0o777))
        return fd, path

    def failing_popen(argv, **kwargs):
        assert argv[:2] == [constants.SANDBOX_EXEC, "-f"]
        assert argv[3:] == ["/bin/echo", "hi"]
        assert kwargs["executable"] == constants.SANDBOX_EXEC
        assert kwargs["start_new_session"] is True and kwargs["close_fds"] is True
        assert kwargs["cwd"] == WORKSPACE
        assert set(kwargs["env"]) == {"A", LAUNCH_MARKER}
        with open(argv[2], encoding="utf-8") as handle:
            assert handle.read().startswith("(version 1)\n(deny default)\n")
        raise OSError("no sandbox-exec here")

    monkeypatch.setattr(launch.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(launch.subprocess, "Popen", failing_popen)
    outcome = run(
        darwin_policy(), ["/bin/echo", "hi"], {"A": "1"}, None, facts=darwin_facts()
    )
    assert outcome.code == "exec.launch_failed"
    assert len(created) == 1 and created[0][1] == 0o600
    assert not os.path.exists(created[0][0])


def test_the_outcome_message_follows_its_code():
    assert Outcome(None, 0).message is None
    assert Outcome("exec.time_limit", -9).message == MESSAGES["exec.time_limit"]


@dataclass
class FakeResource:
    hard: dict
    RLIMIT_CORE: int = 4
    RLIMIT_FSIZE: int = 1
    RLIMIT_NOFILE: int = 8
    RLIM_INFINITY: int = -1
    calls: list = field(default_factory=list)

    def getrlimit(self, which):
        return (0, self.hard[which])

    def setrlimit(self, which, value):
        self.calls.append((which, value))


def test_apply_limits_sets_core_to_zero_and_never_raises_a_hard_limit():
    api = FakeResource(hard={1: -1, 8: 128})
    apply_limits({"file_bytes": 4096, "open_files": 256}, api)
    assert api.calls == [(4, (0, 0)), (1, (4096, 4096)), (8, (128, 128))]
    api = FakeResource(hard={1: 1 << 40, 8: -1})
    apply_limits({"file_bytes": 4096, "open_files": 256}, api)
    assert api.calls == [(4, (0, 0)), (1, (4096, 4096)), (8, (256, 256))]


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


def supervise(proc, fake, wall_ms=60000, data=None, limits=LIMITS):
    return launch._supervise(proc, limits, wall_ms, "feedface", data, fake)


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

    outcome = launch._supervise(
        spawn("import time; time.sleep(600)"), LIMITS, 60000, "f", None, broken
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


def test_run_py_dispatches_and_names_the_missing_conformance_suite(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(launch, "main", lambda args: seen.append(args) or 7)
    assert run_command.main(["launch", "--policy", "p"]) == 7
    assert seen == [["--policy", "p"]]
    assert run_command.main(["conformance", "--json"]) == 2
    assert "not yet available" in capsys.readouterr().err
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
    with pytest.raises(launch._Stopped):
        launch._stop(signal.SIGHUP, None)


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

    monkeypatch.setattr(launch.os, "killpg", gone_group)
    assert launch._end(proc, None) is False
    watch = FakeWatch()
    watch.end = broken_end
    assert launch._end(proc, watch) is False
    assert groups == [proc.pid, proc.pid]
    watch.end = lambda: {proc.pid}
    with caplog.at_level("WARNING", logger="astral_exec.launch"):
        assert launch._end(proc, watch) is False
    assert "survived teardown count=1" in caplog.text
    watch.end = set
    assert launch._end(proc, watch) is True
    assert groups == [proc.pid, proc.pid]
    launch._remove(str(tempfile.gettempdir()) + "/astral-exec-missing.sb")


def test_a_failed_teardown_turns_a_finished_call_into_a_launch_failure():
    fake = FakeWatch()
    fake.end = lambda: {1}
    outcome = supervise(spawn("pass"), fake)
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", 0)


def test_run_py_runs_as_a_script_with_only_its_parent_on_the_path(monkeypatch, capsys):
    script = os.path.join(os.path.dirname(launch.__file__), "run.py")
    monkeypatch.setattr(sys, "argv", [script, "conformance"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(script, run_name="__main__")
    assert stopped.value.code == 2
    assert sys.path[0] == os.path.dirname(os.path.dirname(script))
    assert "not yet available" in capsys.readouterr().err
