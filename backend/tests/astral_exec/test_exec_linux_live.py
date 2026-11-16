"""Runs real confined tools through the Linux backend in the bubblewrap harness: filesystem, socket, signal, fork, namespace, limit and forwarder confinement, and the injected refusals.
It runs only under scripts/exec_harness/run.sh on Linux, so a host without bubblewrap never reaches it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

import pytest
from astral_exec import Policy, launch
from astral_exec.facts import probe
from exec_vectors import minimal_policy

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTRAL_EXEC_HARNESS") != "1" or sys.platform != "linux",
    reason="runs only under scripts/exec_harness/run.sh on linux",
)

PY = sys.executable


def _dirs(tmp_path):
    data = tmp_path / "data"
    workspace = data / "exec" / "call"
    home = tmp_path / "home"
    reads = tmp_path / "reads"
    for directory in (workspace, home, reads):
        directory.mkdir(parents=True)
    (reads / "readable.txt").write_text("ok")
    return data, workspace, home, reads


def _policy(tmp_path, *, reads=(), hosts=(), wall_ms=None, **limits):
    data, workspace, home, read_dir = _dirs(tmp_path)
    document = minimal_policy(
        workspace=str(workspace),
        reads=sorted(str(r) for r in reads),
        hosts=list(hosts),
    )
    document["limits"].update(limits)
    if wall_ms is not None:
        document["time"]["wall_ms"] = wall_ms
    policy = Policy.load(document, "linux", str(data), str(home))
    return policy, workspace, read_dir, data


def _run(policy, code, *args, egress_socket=None):
    argv = [PY, "-I", "-S", "-c", code, *args]
    return launch._run(policy, argv, {}, facts=probe(), egress_socket=egress_socket)


PROBE = r"""
import ctypes, json, mmap, os, signal, socket, sys, threading
sibling = sys.argv[1]
r = {}

def err(fn):
    try:
        fn()
        return "allowed"
    except OSError as e:
        return e.errno

r["write_outside"] = err(lambda: open("/etc/astral_probe", "w"))
r["read_sibling"] = err(lambda: open(sibling))
r["read_base_root"] = err(lambda: open("/etc/hostname").read())
r["write_workspace"] = err(lambda: open("made.txt", "w").write("x"))

path = os.path.abspath("mapped.bin")
with open(path, "wb") as h:
    h.write(b"\0" * 4096)
fd = os.open(path, os.O_RDONLY)
try:
    mmap.mmap(fd, 4096, prot=mmap.PROT_READ | mmap.PROT_EXEC, flags=mmap.MAP_PRIVATE)
    r["exec_mmap_workspace"] = "allowed"
except OSError as e:
    r["exec_mmap_workspace"] = e.errno
os.close(fd)

r["unix_socket"] = err(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
r["inet_stream"] = err(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
r["inet_dgram"] = err(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
try:
    a, b = socket.socketpair()
    a.close(); b.close()
    r["socketpair"] = "allowed"
except OSError as e:
    r["socketpair"] = e.errno

r["signal_launcher"] = err(lambda: os.kill(1, signal.SIGTERM))
r["fork"] = err(lambda: os.fork())

flag = {}
t = threading.Thread(target=lambda: flag.setdefault("ran", True))
t.start(); t.join()
r["thread"] = flag.get("ran", False)

print(json.dumps(r))
"""


def test_the_confined_tool_is_boxed_in_every_dimension(tmp_path):
    policy, workspace, read_dir, data = _policy(tmp_path)
    outcome = _run(policy, PROBE, str(data / "secret.txt"))
    assert outcome.code is None, (outcome.code, outcome.stderr)
    assert outcome.exit_code == 0, outcome.stderr
    result = json.loads(outcome.stdout)
    import errno

    assert result["write_outside"] in (errno.EACCES, errno.EPERM, errno.EROFS)
    assert result["read_sibling"] in (errno.EACCES, errno.ENOENT, errno.EPERM)
    assert result["read_base_root"] == "allowed"
    assert result["write_workspace"] == "allowed"
    assert result["exec_mmap_workspace"] in (errno.EACCES, errno.EPERM)
    assert result["unix_socket"] == errno.EPERM
    assert result["inet_stream"] == errno.EPERM
    assert result["inet_dgram"] == errno.EPERM
    assert result["socketpair"] == "allowed"
    assert result["signal_launcher"] in (errno.EPERM, errno.ESRCH)
    assert result["fork"] == errno.EPERM
    assert result["thread"] is True


def test_the_tool_sees_only_its_own_namespace_in_proc(tmp_path):
    policy, workspace, read_dir, data = _policy(tmp_path, reads=["/proc"])
    code = (
        "import json, os\n"
        "pids = sorted(int(p) for p in os.listdir('/proc') if p.isdigit())\n"
        "print(json.dumps(pids))\n"
    )
    outcome = _run(policy, code)
    assert outcome.code is None and outcome.exit_code == 0, outcome.stderr
    pids = json.loads(outcome.stdout)
    assert 1 in pids
    assert len(pids) < 20


CHILD_PROBE = r"""
import json, os, time
pid = os.fork()
if pid == 0:
    res = {}
    try:
        os.setsid(); res["setsid"] = "allowed"
    except OSError as e:
        res["setsid"] = e.errno
    with open("tool.sh", "w") as h:
        h.write("#!/bin/sh\necho hi\n")
    os.chmod("tool.sh", 0o755)
    try:
        os.execv("./tool.sh", ["tool.sh"]); res["exec"] = "replaced"
    except OSError as e:
        res["exec"] = e.errno
    with open("ready.txt", "w") as h:
        h.write(json.dumps(res))
    time.sleep(600)
    os._exit(0)
while not os.path.exists("ready.txt"):
    time.sleep(0.01)
os._exit(0)
"""


def _sentinel_alive(token):
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/environ", "rb") as handle:
                block = handle.read()
        except OSError:
            continue
        if token.encode() in block.split(b"\0"):
            return True
    return False


def test_a_forked_child_cannot_detach_or_exec_and_is_reaped(tmp_path):
    import errno

    policy, workspace, read_dir, data = _policy(tmp_path, processes=2)
    token = f"SENTINEL={os.urandom(8).hex()}"
    argv = [PY, "-I", "-S", "-c", CHILD_PROBE]
    outcome = launch._run(
        policy, argv, {token.split("=")[0]: token.split("=")[1]}, facts=probe()
    )
    assert outcome.code is None and outcome.exit_code == 0, outcome.stderr
    result = json.loads((workspace / "ready.txt").read_text())
    assert result["setsid"] == errno.EPERM
    assert result["exec"] in (errno.EACCES, errno.EPERM)
    assert _sentinel_alive(token.split("=")[1]) is False


def test_the_time_limit_stops_a_sleeping_tool(tmp_path):
    policy, workspace, read_dir, data = _policy(tmp_path, wall_ms=300)
    outcome = _run(policy, "import time\ntime.sleep(600)\n")
    assert outcome.code == "exec.time_limit"


def test_the_file_size_limit_stops_a_large_write(tmp_path):
    policy, workspace, read_dir, data = _policy(tmp_path, file_bytes=4096)
    argv = ["/usr/bin/dd", "if=/dev/zero", "of=big", "bs=8192", "count=16"]
    outcome = launch._run(policy, argv, {}, facts=probe())
    assert outcome.code == "exec.file_limit"


def test_the_open_file_limit_bounds_descriptors(tmp_path):
    import errno

    policy, workspace, read_dir, data = _policy(tmp_path, open_files=16)
    code = (
        "import json, os\n"
        "opened = []\n"
        "last = None\n"
        "for i in range(200):\n"
        "    try:\n"
        "        opened.append(os.open('/etc/hostname', os.O_RDONLY))\n"
        "    except OSError as e:\n"
        "        last = e.errno; break\n"
        "print(json.dumps({'count': len(opened), 'errno': last}))\n"
    )
    outcome = _run(policy, code)
    assert outcome.code is None and outcome.exit_code == 0, outcome.stderr
    result = json.loads(outcome.stdout)
    assert result["errno"] == errno.EMFILE
    assert result["count"] < 16


EGRESS_PROBE = r"""
import json, socket, sys
r = {}

def err(fn):
    try:
        return fn()
    except OSError as e:
        return e.errno

def forward():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(("127.0.0.1", 3128))
    s.sendall(b"ping")
    data = s.recv(64)
    s.close()
    return data.decode()

r["forward"] = err(forward)

def other_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(("127.0.0.1", 8001))
        return "connected"
    finally:
        s.close()

r["tcp_8001"] = err(other_port)
r["udp"] = err(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
print(json.dumps(r))
"""


def test_the_egress_forwarder_reaches_the_host_socket_and_blocks_other_ports(tmp_path):
    import errno

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock_path = f"/tmp/eg{os.getpid()}.sock"
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    server.bind(sock_path)
    server.listen(1)

    def echo():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.sendall(b"PONG:" + conn.recv(64))

    worker = threading.Thread(target=echo, daemon=True)
    worker.start()
    policy, workspace, read_dir, data = _policy(
        tmp_path, hosts=[{"host": "api.example.com", "port": 443}]
    )
    outcome = _run(policy, EGRESS_PROBE, egress_socket=sock_path)
    worker.join(timeout=5)
    server.close()
    os.unlink(sock_path)
    assert outcome.code is None and outcome.exit_code == 0, outcome.stderr
    result = json.loads(outcome.stdout)
    assert result["forward"] == "PONG:ping"
    assert result["tcp_8001"] in (errno.EACCES, errno.EPERM)
    assert result["udp"] == errno.EPERM


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"landlock_abi": 5}, "landlock_abi"),
        ({"bwrap": False}, "bwrap_missing"),
        ({"userns": False}, "userns_denied"),
    ],
)
def test_injected_facts_refuse_before_any_process(tmp_path, override, reason):
    policy, workspace, read_dir, data = _policy(tmp_path)
    facts = probe()
    facts.update(override)
    if override.get("bwrap") is False:
        facts.bwrap_path = None
    outcome = launch._run(policy, [PY, "-c", "print(1)"], {}, facts=facts)
    assert outcome.code == "exec.backend_unavailable"
    assert outcome.refusal is not None and outcome.refusal.reason == reason
    assert outcome.stdout == b"" and outcome.exit_code is None
