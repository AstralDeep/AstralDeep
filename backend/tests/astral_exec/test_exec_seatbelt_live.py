"""Runs real tools under sandbox-exec through astral_exec.launch and run.py on this Mac and checks each limit and denial by its outcome.
The first test records that RLIMIT_AS cannot carry a policy memory limit here, which is why the watchdog enforces memory and processes.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from astral_exec import Policy, launch
from astral_exec.constants import (
    OUTPUT_LIMIT_BYTES,
    LAUNCH_MARKER,
    MESSAGES,
    REFUSED_EXIT_STATUS,
    SANDBOX_EXEC,
)
from astral_exec.facts import probe
from astral_exec.launch import run
from astral_exec.watchdog import PROC_PPID_ONLY, Darwin

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC),
    reason="Seatbelt runs need macOS with /usr/bin/sandbox-exec",
)

PYTHON = os.path.realpath(sys.executable)
PREFIX = os.path.realpath(sys.base_prefix)
HOME = pwd.getpwuid(os.getuid()).pw_dir
RUN_PY = Path(launch.__file__).resolve().parent / "run.py"
LIMITS = {
    "memory_bytes": 1 << 30,
    "processes": 1,
    "open_files": 256,
    "file_bytes": 1 << 24,
}

RLIMIT_AS_PROBE = """
import json, resource
results = {}
for value in (16777216, 536870912, 68719476736):
    try:
        resource.setrlimit(resource.RLIMIT_AS, (value, value))
        results[str(value)] = "set"
    except (ValueError, OSError) as error:
        results[str(value)] = type(error).__name__
block = bytearray(1 << 30)
block[::4096] = b"\\x01" * (len(block) // 4096)
results["allocated"] = len(block)
print(json.dumps(results))
"""


def test_rlimit_as_cannot_hold_any_policy_memory_limit_on_this_mac():
    done = subprocess.run(
        [PYTHON, "-I", "-B", "-c", RLIMIT_AS_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "16777216": "ValueError",
        "536870912": "ValueError",
        "68719476736": "ValueError",
        "allocated": 1 << 30,
    }


@pytest.fixture
def call(tmp_path):
    data = tmp_path.resolve() / "data"
    workspace = data / "exec" / "call"
    workspace.mkdir(parents=True)
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()

    def make(wall_ms=60000, **limits):
        document = {
            "v": 1,
            "reads": [PREFIX],
            "workspace": str(workspace),
            "hosts": [],
            "limits": {**LIMITS, **limits},
            "time": {"wall_ms": wall_ms},
        }
        return Policy.load(document, "darwin", str(data), HOME)

    make.workspace = workspace
    make.outside = outside
    make.data = data
    return make


def python(code):
    return [PYTHON, "-I", "-B", "-c", code]


def gone(pid, tries=500):
    system = Darwin()
    for _ in range(tries):
        usage = system.usage(pid)
        if usage is None or usage.exited:
            return True
        time.sleep(0.01)
    return False


DENIALS = """
import errno, json, os, subprocess, sys
workspace, outside, home = sys.argv[1:4]

def attempt(action):
    try:
        action()
    except OSError as error:
        return errno.errorcode[error.errno]
    return "ok"

def write(path):
    def act():
        with open(path, "w") as handle:
            handle.write("x")
    return act

def open_only(path, flags=os.O_RDONLY):
    return lambda: os.close(os.open(path, flags))

print(json.dumps({
    "write_workspace": attempt(write(os.path.join(workspace, "inside.txt"))),
    "write_outside": attempt(write(os.path.join(outside, "outside.txt"))),
    "write_home_startup": attempt(open_only(os.path.join(home, ".zshrc"), os.O_WRONLY)),
    "list_ssh": attempt(lambda: os.listdir(os.path.join(home, ".ssh"))),
    "read_ssh_key": attempt(open_only(os.path.join(home, ".ssh", "id_ed25519"))),
    "read_keychain": attempt(open_only(os.path.join(home, "Library", "Keychains", "login.keychain-db"))),
    "fork": attempt(lambda: os.waitpid(os.fork() or os._exit(0), 0)),
    "posix_spawn": attempt(lambda: os.posix_spawn("/bin/echo", ["/bin/echo"], {})),
    "subprocess": attempt(lambda: subprocess.run(["/bin/echo"], check=False)),
    "exec_workspace": attempt(lambda: os.execv(os.path.join(workspace, "echo"), ["echo"])),
}))
"""


def test_writes_outside_the_workspace_reads_of_secrets_and_forks_are_refused(call):
    policy = call()
    shutil.copy("/bin/echo", call.workspace / "echo")
    argv = python(DENIALS) + [str(call.workspace), str(call.outside), HOME]
    outcome = run(policy, argv, {}, None)
    assert (outcome.code, outcome.exit_code) == (None, 0), outcome.stderr
    assert json.loads(outcome.stdout) == {
        "write_workspace": "ok",
        "write_outside": "EPERM",
        "write_home_startup": "EPERM",
        "list_ssh": "EPERM",
        "read_ssh_key": "EPERM",
        "read_keychain": "EPERM",
        "fork": "EPERM",
        "posix_spawn": "EPERM",
        "subprocess": "EPERM",
        "exec_workspace": "EPERM",
    }
    assert (call.workspace / "inside.txt").read_text() == "x"
    assert not (call.outside / "outside.txt").exists()


def test_the_child_sees_exactly_the_passed_environment_and_the_marker(call):
    outcome = run(call(), ["/usr/bin/env"], {"A": "1", "B": "two words"}, None)
    lines = outcome.stdout.decode().splitlines()
    marker = [line for line in lines if line.startswith(LAUNCH_MARKER + "=")]
    assert sorted(set(lines) - set(marker)) == ["A=1", "B=two words"]
    assert len(lines) == 3 and len(marker) == 1
    assert re.fullmatch(r"[0-9a-f]{32}", marker[0].split("=", 1)[1])
    again = run(call(), ["/usr/bin/env"], {}, None).stdout.decode().splitlines()
    assert len(again) == 1 and again[0] != marker[0]


def test_stdin_reaches_the_tool_and_the_workspace_is_its_directory(call):
    outcome = run(call(), ["/bin/sh", "-c", "pwd; exec /bin/cat"], {}, b"in\n")
    assert outcome.stdout == f"{call.workspace}\n".encode() + b"in\n"
    assert outcome.code is None and outcome.exit_code == 0


def test_a_tool_past_its_wall_time_is_stopped(call):
    outcome = run(call(wall_ms=2000), ["/bin/sleep", "600"], {}, None)
    assert (outcome.code, outcome.exit_code) == ("exec.time_limit", -signal.SIGKILL)
    assert outcome.message == MESSAGES["exec.time_limit"]


def test_a_tool_past_its_memory_limit_is_stopped(call):
    code = (
        "import time\nblock = bytearray(256 << 20)\n"
        "block[::4096] = b'\\x01' * (len(block) // 4096)\ntime.sleep(600)\n"
    )
    outcome = run(call(memory_bytes=64 << 20), python(code), {}, None)
    assert (outcome.code, outcome.exit_code) == ("exec.memory_limit", -signal.SIGKILL)


def test_a_tool_past_its_process_limit_is_stopped_with_every_child(call):
    code = (
        "import subprocess, time\n"
        "for _ in range(4):\n"
        "    print(subprocess.Popen(['/bin/sleep', '600']).pid, flush=True)\n"
        "time.sleep(600)\n"
    )
    outcome = run(call(processes=3), python(code), {}, None)
    assert (outcome.code, outcome.exit_code) == ("exec.process_limit", -signal.SIGKILL)
    kids = [int(pid) for pid in outcome.stdout.split()]
    assert len(kids) >= 2 and all(gone(pid) for pid in kids)


def test_a_writer_past_file_bytes_is_killed_by_the_file_size_signal(call):
    target = call.workspace / "big"
    argv = ["/bin/dd", "if=/dev/zero", f"of={target}", "bs=4096", "count=64"]
    outcome = run(call(file_bytes=16384), argv, {}, None)
    assert (outcome.code, outcome.exit_code) == ("exec.file_limit", -signal.SIGXFSZ)
    assert target.stat().st_size <= 16384


def test_a_python_writer_past_file_bytes_gets_efbig(call):
    code = (
        "import errno, sys\ntry:\n    open(sys.argv[1], 'wb').write(b'x' * 65536)\n"
        "except OSError as error:\n    print(errno.errorcode[error.errno])\n"
    )
    target = call.workspace / "big"
    outcome = run(call(file_bytes=16384), python(code) + [str(target)], {}, None)
    assert (outcome.code, outcome.stdout) == (None, b"EFBIG\n")
    assert target.stat().st_size <= 16384


def test_open_files_hold_at_the_limit(call):
    code = (
        "import errno, os, sys\nheld = []\ntry:\n    for i in range(200):\n"
        "        held.append(open(os.path.join(sys.argv[1], str(i)), 'w'))\n"
        "except OSError as error:\n    print(len(held), errno.errorcode[error.errno])\n"
    )
    outcome = run(call(open_files=32), python(code) + [str(call.workspace)], {}, None)
    count, name = outcome.stdout.split()
    assert name == b"EMFILE" and int(count) < 32
    assert outcome.code is None and outcome.exit_code == 0


def test_a_nohup_child_is_gone_after_the_call(call):
    argv = ["/bin/sh", "-c", "/usr/bin/nohup /bin/sleep 600 >/dev/null 2>&1 & echo $!"]
    outcome = run(call(processes=4), argv, {}, None)
    assert outcome.code is None and outcome.exit_code == 0
    assert gone(int(outcome.stdout))


ESCAPE = """
import os, sys, time
for target in (["/bin/sleep", "600"], [sys.executable, "-I", "-B", "-c", "import time; time.sleep(600)"]):
    if os.fork() == 0:
        os.setsid()
        pid = os.fork()
        if pid == 0:
            os.execv(target[0], target)
        print(pid, flush=True)
        time.sleep(1)
        os._exit(0)
time.sleep(2)
"""


def test_strays_that_leave_the_group_are_gone_after_the_call(call):
    outcome = run(call(processes=8), python(ESCAPE), {}, None)
    strays = [int(pid) for pid in outcome.stdout.split()]
    assert outcome.code is None and outcome.exit_code == 0
    assert len(strays) == 2 and all(gone(pid) for pid in strays)


LIMITS_SEEN = (
    "import json, resource\n"
    "names = ('CORE', 'FSIZE', 'NOFILE', 'CPU')\n"
    "print(json.dumps([resource.getrlimit(getattr(resource, 'RLIMIT_' + n))"
    " for n in names]))\n"
)


def test_the_confined_tool_runs_under_every_trampoline_limit(call):
    policy = call(wall_ms=4500, file_bytes=65536, open_files=40)
    outcome = run(policy, python(LIMITS_SEEN), {}, None)
    assert outcome.code is None, outcome.stderr
    assert json.loads(outcome.stdout) == [[0, 0], [65536, 65536], [40, 40], [6, 6]]


def test_a_trampoline_refusal_is_a_launch_failure(call, monkeypatch):
    real = launch._command

    def bad_limit(*args):
        command = real(*args)
        command[command.index("--nofile") + 1] = "-1"
        return command

    monkeypatch.setattr(launch, "_command", bad_limit)
    target = call.workspace / "ran"
    outcome = run(call(), ["/usr/bin/touch", str(target)], {}, None)
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", None)
    assert not target.exists()


def test_output_past_the_cap_is_kept_to_the_cap_and_flagged(call):
    code = "import sys\nsys.stdout.buffer.write(b'o' * (9 << 20))\n"
    outcome = run(call(), python(code), {}, None)
    assert (outcome.code, outcome.exit_code) == (None, 0)
    assert outcome.stdout == b"o" * OUTPUT_LIMIT_BYTES
    assert outcome.truncated == ("stdout",)


def wait_for_line(stream, token, timeout=30):
    deadline = time.monotonic() + timeout
    seen = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stream], [], [], deadline - time.monotonic())
        if not ready:
            break
        chunk = os.read(stream.fileno(), 4096)
        if not chunk:
            break
        seen += chunk
        if token in seen:
            return True
    return False


def running_child(launcher, stream):
    assert wait_for_line(stream, b"go"), "the confined tool never started"
    children = Darwin().pids(PROC_PPID_ONLY, launcher.pid)
    assert len(children) == 1
    assert not gone(children[0], tries=1)
    return children[0]


def test_a_busy_tool_dies_by_its_cpu_limit_after_the_launcher_is_killed(tmp_path, call):
    tool = python("import sys\nprint('go', flush=True)\nwhile True: pass")
    launcher = subprocess.Popen(
        _files(tmp_path, call, wall_ms=1000) + tool,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        child = running_child(launcher, launcher.stdout)
        launcher.kill()
        launcher.wait(timeout=60)
        assert gone(child, tries=3000)
    finally:
        launcher.stdout.close()


def test_a_tool_sandbox_exec_cannot_start_is_a_launch_failure(call):
    outcome = run(call(), [str(call.workspace / "missing")], {}, None)
    assert (outcome.code, outcome.exit_code) == ("exec.launch_failed", None)
    assert outcome.stderr.startswith(b"sandbox-exec:") and b"execvp" in outcome.stderr


def test_an_option_in_place_of_the_tool_never_reaches_sandbox_exec(call):
    target = call.outside / "injected"
    argv = ["-p", "(version 1)(allow default)", "/usr/bin/touch", str(target)]
    assert run(call(), argv, {}, None).code == "exec.launch_failed"
    outcome = run(call(), ["/usr/bin/touch", str(target)], {}, None)
    assert outcome.code is None and outcome.exit_code != 0
    assert not target.exists()


def test_real_facts_in_shadow_mode_are_refused(call):
    facts = probe()
    facts["mode"] = "shadow"
    outcome = launch._run(call(), ["/bin/echo"], {}, None, facts=facts)
    assert outcome.code == "exec.shadow_refused" and outcome.exit_code is None


def test_the_profile_is_private_and_removed_after_the_call(call, monkeypatch):
    created = []
    real_mkstemp = tempfile.mkstemp

    def recording(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append((path, os.stat(path).st_mode & 0o777))
        return fd, path

    monkeypatch.setattr(launch.tempfile, "mkstemp", recording)
    assert run(call(), ["/bin/echo", "hi"], {}, None).stdout == b"hi\n"
    assert len(created) == 1 and created[0][1] == 0o600
    assert not os.path.exists(created[0][0])


def _files(tmp_path, call, wall_ms=60000, **limits):
    policy = call(wall_ms=wall_ms, **limits)
    policy_file = tmp_path / "policy.json"
    policy_file.write_bytes(policy.canonical_bytes())
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps({"A": "1"}))
    return [
        PYTHON,
        "-I",
        "-B",
        str(RUN_PY),
        "launch",
        "--policy",
        str(policy_file),
        "--data-dir",
        str(call.data),
        "--home",
        HOME,
        "--env-file",
        str(env_file),
        "--",
    ]


def test_run_py_launches_a_confined_process_with_its_own_stdio(tmp_path, call):
    command = _files(tmp_path, call)
    done = subprocess.run(
        command + ["/bin/sh", "-c", 'echo "$A"; exit 4'],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert (done.returncode, done.stdout) == (4, b"1\n")
    stopped = subprocess.run(
        _files(tmp_path, call, wall_ms=1000) + ["/bin/sleep", "600"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert stopped.returncode == REFUSED_EXIT_STATUS
    assert json.loads(stopped.stderr.splitlines()[-1])["code"] == "exec.time_limit"


def test_run_py_tears_down_its_child_when_terminated(tmp_path, call):
    tool = ["/bin/sh", "-c", "echo go; exec /bin/sleep 600"]
    launcher = subprocess.Popen(
        _files(tmp_path, call) + tool,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        child = running_child(launcher, launcher.stdout)
        launcher.send_signal(signal.SIGTERM)
        assert launcher.wait(timeout=60) == 128 + signal.SIGTERM
        assert gone(child)
    finally:
        launcher.stdout.close()


def test_run_py_conformance_is_refused_until_the_suite_exists():
    done = subprocess.run(
        [PYTHON, "-I", "-B", str(RUN_PY), "conformance", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2 and "not yet available" in done.stderr


MARKED_TOOL = ["/bin/sh", "-c", 'echo "$ASTRAL_EXEC_MARKER"; exec /bin/sleep 600']


@pytest.mark.parametrize(("processes", "swept"), [(2, True), (1, False)])
def test_the_marker_sweep_ends_a_marked_process_only_when_fork_is_allowed(
    tmp_path, call, processes, swept
):
    command = _files(tmp_path, call, wall_ms=3000, processes=processes)
    launcher = subprocess.Popen(
        command + MARKED_TOOL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    marker = launcher.stdout.readline().decode().strip()
    planted = subprocess.Popen(
        python("import time; time.sleep(600)"),
        env={LAUNCH_MARKER: marker},
        start_new_session=True,
    )
    try:
        assert re.fullmatch(r"[0-9a-f]{32}", marker)
        assert launcher.wait(timeout=60) == REFUSED_EXIT_STATUS
        assert (planted.poll() is not None) is swept
    finally:
        planted.kill()
        planted.wait()
        launcher.stdout.close()
