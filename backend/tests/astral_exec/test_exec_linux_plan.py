"""Pins the Linux plan, the Landlock rules, the seccomp program, the mount table check, the forwarder and the inner launcher's steps on any host, without bubblewrap.
The plan must equal every committed vector byte for byte, and the confined runs live in test_exec_linux_live.py.
"""

from __future__ import annotations

import os
import socket
import struct
import threading

import pytest
from astral_exec import ContextError, Policy, canonjson, forwarder, landlock, linux
from astral_exec import seccomp, watchdog
from astral_exec.select import _mountinfo_refuses, _translate, mount_exposes_data_dir
from exec_vectors import PROFILE_ERRORS, PROFILES, loaded, minimal_policy


@pytest.mark.parametrize("case", PROFILES, ids=[c["name"] for c in PROFILES])
def test_the_plan_equals_each_profile_vector_byte_for_byte(case):
    policy = Policy.parse(case["policy"], "linux")
    built = linux.plan(policy, case["context"]["egress_socket"])
    assert canonjson.canonical(built) == canonjson.canonical(case["linux"])


@pytest.mark.parametrize(
    "case", PROFILE_ERRORS, ids=[c["name"] for c in PROFILE_ERRORS]
)
def test_a_policy_the_darwin_fold_refuses_still_plans_on_linux(case):
    policy = Policy.parse(case["policy"], "linux")
    built = linux.plan(policy, case["context"].get("egress_socket"))
    assert canonjson.canonical(built) == canonjson.canonical(case["linux"])


HOSTED = minimal_policy(hosts=[{"host": "api.example.com", "port": 443}])


@pytest.mark.parametrize("socket", [None, "", "relative.sock", "/a/../b", 7])
def test_a_policy_with_hosts_needs_an_absolute_egress_socket(socket):
    with pytest.raises(ContextError):
        linux.plan(Policy.parse(HOSTED, "linux"), socket)


def test_a_policy_without_hosts_ignores_the_egress_socket():
    plain = Policy.parse(minimal_policy(), "linux")
    assert linux.plan(plain, None) == linux.plan(plain, "/run/x.sock")
    assert linux.plan(plain, None)["egress"] is None


def test_the_plan_refuses_a_policy_loaded_for_another_platform():
    with pytest.raises(ContextError):
        linux.plan(loaded(minimal_policy(), "darwin"), None)
    with pytest.raises(ContextError):
        linux.plan(minimal_policy(), None)  # type: ignore[arg-type]


def _seccomp_data(nr, arch, arg0=0, arg1=0):
    return (
        struct.pack("<II", nr & 0xFFFFFFFF, arch & 0xFFFFFFFF)
        + b"\0" * 8
        + struct.pack("<Q", arg0)
        + struct.pack("<Q", arg1)
        + b"\0" * 24
    )


def _run_bpf(program, data):
    rows = [
        struct.unpack("HBBI", program[i : i + 8]) for i in range(0, len(program), 8)
    ]
    pc = 0
    acc = 0
    while True:
        op, jt, jf, k = rows[pc]
        pc += 1
        if op == seccomp.LD_ABS_W:
            acc = struct.unpack("<I", data[k : k + 4])[0]
        elif op == seccomp.ALU_AND_K:
            acc &= k
        elif op == seccomp.JEQ_K:
            pc += jt if acc == k else jf
        elif op == seccomp.JGE_K:
            pc += jt if acc >= k else jf
        elif op == seccomp.JA:
            pc += k
        elif op == seccomp.RET_K:
            return k
        else:
            raise AssertionError(f"unknown op {op:#x}")


ARCHES = ["x86_64", "aarch64"]
EPERM = seccomp.RET_ERRNO | seccomp.EPERM
ENOSYS = seccomp.RET_ERRNO | seccomp.ENOSYS


@pytest.mark.parametrize("machine", ARCHES)
def test_the_seccomp_filter_denies_ipc_and_bounds_sockets(machine):
    nr = seccomp.numbers(machine)
    arch = seccomp.AUDIT_ARCH[machine]
    program = seccomp.build(machine, deny_fork=True, inet_stream_sockets=True)
    assert len(program) % 8 == 0

    def verdict(number, arg0=0, arg1=0, use_arch=arch):
        return _run_bpf(program, _seccomp_data(number, use_arch, arg0, arg1))

    assert verdict(99999) == seccomp.RET_ALLOW
    assert verdict(99999, use_arch=0x1234) == seccomp.RET_KILL_PROCESS
    for name in seccomp.DENIED_SYSCALLS:
        assert verdict(nr[name]) == EPERM, name
    assert verdict(nr["socket"], seccomp.AF_UNIX, seccomp.SOCK_STREAM) == EPERM
    assert verdict(nr["socket"], seccomp.AF_INET, seccomp.SOCK_STREAM) == seccomp.RET_ALLOW
    assert verdict(nr["socket"], seccomp.AF_INET, 2) == EPERM
    assert verdict(nr["socket"], seccomp.AF_INET6, 1 | 0x80000) == seccomp.RET_ALLOW
    assert verdict(nr["socket"], seccomp.AF_INET6, 2 | 0x80000) == EPERM
    assert verdict(nr["socket"], 16, seccomp.SOCK_STREAM) == seccomp.RET_ALLOW
    assert verdict(nr["clone3"]) == ENOSYS
    assert verdict(nr["clone"], seccomp.CLONE_THREAD | 0x100) == seccomp.RET_ALLOW
    assert verdict(nr["clone"], 0x100) == EPERM


def test_the_seccomp_filter_without_hosts_denies_every_inet_socket():
    nr = seccomp.numbers("x86_64")
    program = seccomp.build("x86_64", deny_fork=True, inet_stream_sockets=False)
    data = _seccomp_data(nr["socket"], seccomp.AUDIT_ARCH["x86_64"], seccomp.AF_INET, 1)
    assert _run_bpf(program, data) == EPERM
    x32 = _seccomp_data(5 | seccomp.X32_BIT, seccomp.AUDIT_ARCH["x86_64"])
    assert _run_bpf(program, x32) == seccomp.RET_KILL_PROCESS


def test_the_seccomp_filter_allows_fork_when_not_denied():
    nr = seccomp.numbers("x86_64")
    program = seccomp.build("x86_64", deny_fork=False, inet_stream_sockets=True)
    data = _seccomp_data(nr["clone3"], seccomp.AUDIT_ARCH["x86_64"])
    assert _run_bpf(program, data) == seccomp.RET_ALLOW


def test_seccomp_numbers_refuse_an_unknown_machine():
    with pytest.raises(OSError):
        seccomp.numbers("riscv64")
    with pytest.raises(OSError):
        seccomp.build("riscv64", True, True)


def test_seccomp_install_runs_prctl_twice_with_the_program():
    calls = []
    seccomp.install(
        {"deny_fork": True, "inet_stream_sockets": False},
        machine="x86_64",
        prctl=lambda *args: calls.append(args) or 0,
    )
    assert calls[0] == (seccomp.PR_SET_NO_NEW_PRIVS, 1, 0)
    assert calls[1][0] == seccomp.PR_SET_SECCOMP and calls[1][1] == 2


LANDLOCK_PLAN = {
    "exec_read": ["/usr", "/bin"],
    "device_files": ["/dev/null", "/dev/zero"],
    "workspace": "/ws",
}


def test_landlock_rights_builders():
    assert landlock.fs_rights(1) == (1 << 13) - 1
    assert landlock.fs_rights(6) == (1 << 16) - 1
    fs = landlock.fs_rights(6)
    assert landlock.workspace_rights(fs) == fs & ~(landlock.EXECUTE | landlock.MAKE_SOCK)
    assert landlock.ruleset_attr(fs) == struct.pack("QQQ", fs, 3, 3)
    rows = landlock.grants(LANDLOCK_PLAN, fs)
    assert rows[0] == ("/usr", landlock.READ_RIGHTS)
    assert ("/dev/null", landlock.DEVICE_RIGHTS) in rows
    assert rows[-1] == ("/ws", landlock.workspace_rights(fs))


class _FakeKernel:
    def __init__(self, abi=8):
        self._abi = abi
        self.rules = []
        self.restricted = None
        self.created = None

    def abi(self):
        return self._abi

    def create_ruleset(self, attr):
        self.created = attr
        return 7

    def add_rule(self, ruleset, kind, rule):
        self.rules.append((ruleset, kind, rule))

    def restrict(self, ruleset):
        self.restricted = ruleset


def test_landlock_apply_adds_every_rule_and_restricts():
    kernel = _FakeKernel()
    closed = []
    opened = {"/usr": 10, "/bin": 11, "/dev/null": 12, "/dev/zero": 13, "/ws": 14}
    landlock.apply(
        LANDLOCK_PLAN,
        hosted=True,
        kernel=kernel,
        open_path=lambda path: opened[path],
        isdir=lambda path: path != "/dev/null",
        close=closed.append,
    )
    kinds = [kind for _ruleset, kind, _rule in kernel.rules]
    assert kinds.count(landlock.RULE_PATH_BENEATH) == 5
    assert kinds.count(landlock.RULE_NET_PORT) == 1
    net_rule = kernel.rules[-1][2]
    assert net_rule == struct.pack("=QQ", landlock.NET_CONNECT_TCP, landlock.PROXY_PORT)
    assert kernel.restricted == 7
    assert closed[-1] == 7


def test_landlock_apply_masks_a_file_grant_and_skips_a_missing_path():
    kernel = _FakeKernel()

    def opener(path):
        if path == "/bin":
            raise OSError("absent")
        return 10

    landlock.apply(
        LANDLOCK_PLAN,
        hosted=False,
        kernel=kernel,
        open_path=opener,
        isdir=lambda path: False,
        close=lambda fd: None,
    )
    kinds = [kind for _ruleset, kind, _rule in kernel.rules]
    assert landlock.RULE_NET_PORT not in kinds
    fs = landlock.fs_rights(6)
    first = struct.unpack("=Qi", kernel.rules[0][2])[0]
    assert first == landlock.READ_RIGHTS & landlock.FILE_RIGHTS & fs


def test_landlock_apply_refuses_an_old_abi():
    with pytest.raises(OSError):
        landlock.apply(LANDLOCK_PLAN, hosted=False, kernel=_FakeKernel(abi=5))


def test_the_forwarder_relays_bytes_between_the_two_sockets():
    listener = forwarder.build_listener("127.0.0.1", 0)
    upstream_server, upstream_client = socket.socketpair()

    def connect(path):
        return upstream_client

    def echo():
        upstream_server.sendall(b"PONG:" + upstream_server.recv(1024))

    threading.Thread(target=echo, daemon=True).start()
    worker = threading.Thread(
        target=forwarder.serve, args=(listener, "/run/x.sock", connect), daemon=True
    )
    worker.start()
    client = socket.create_connection(listener.getsockname())
    client.sendall(b"hello")
    client.settimeout(5)
    assert client.recv(1024) == b"PONG:hello"
    client.close()
    listener.close()
    worker.join(timeout=5)
    upstream_server.close()


def test_the_forwarder_survives_an_upstream_that_cannot_connect(tmp_path):
    listener = forwarder.build_listener("127.0.0.1", 0)
    attempts = []

    def refuse(path):
        attempts.append(path)
        raise OSError("no upstream")

    worker = threading.Thread(
        target=forwarder.serve,
        args=(listener, "/missing.sock", refuse),
        daemon=True,
    )
    worker.start()
    first = socket.create_connection(listener.getsockname())
    first.close()
    second = socket.create_connection(listener.getsockname())
    second.close()
    listener.close()
    worker.join(timeout=5)
    assert len(attempts) >= 1


def test_the_relay_pumps_both_directions_until_eof():
    left_a, left_b = socket.socketpair()
    right_a, right_b = socket.socketpair()
    worker = threading.Thread(
        target=forwarder.relay, args=(left_b, right_a), daemon=True
    )
    worker.start()
    left_a.sendall(b"up")
    assert right_b.recv(16) == b"up"
    right_b.sendall(b"down")
    assert left_a.recv(16) == b"down"
    left_a.close()
    right_b.close()
    worker.join(timeout=5)


class _FakeSystem:
    def __init__(self, table):
        self.table = table

    def marked_pids(self, marker):
        return list(self.table)

    def usage(self, pid):
        return self.table.get(pid)


def test_the_linux_watch_counts_and_sums_only_live_marked_processes():
    table = {
        10: watchdog.Usage(1, False, 1000),
        11: watchdog.Usage(1, False, 2000),
        12: watchdog.Usage(1, True, 4000),
    }
    killed = []
    groups = []
    watch = watchdog.LinuxWatch(
        99,
        b"MARK",
        _FakeSystem(table),
        kill=lambda pid, sig: killed.append(pid),
        killpg=lambda pid, sig: groups.append(pid),
    )
    assert watch.started is True
    assert watch.sample() == (2, 3000)


def test_the_linux_watch_end_kills_the_root_group_and_marked_survivors():
    state = {"live": {10: watchdog.Usage(1, False, 100)}}

    class Draining:
        def marked_pids(self, marker):
            return list(state["live"])

        def usage(self, pid):
            return state["live"].get(pid)

    killed = []
    groups = []

    def kill(pid, sig):
        killed.append(pid)
        state["live"].clear()

    def killpg(pid, sig):
        groups.append(pid)

    watch = watchdog.LinuxWatch(99, b"MARK", Draining(), kill=kill, killpg=killpg)
    assert watch.end() == set()
    assert 99 in groups and 10 in killed


MOUNTINFO_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt/reads")


def test_mount_exposes_a_bind_of_the_data_directory_under_a_read_root():
    text = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:1 /var/lib/node /opt/reads/leak rw - overlay overlay rw\n"
    )
    assert mount_exposes_data_dir(text, MOUNTINFO_ROOTS, "/var/lib/node") is True


def test_a_bind_from_another_device_or_outside_the_roots_is_not_flagged():
    other_device = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 254:1 /var/lib/node /opt/reads/leak rw - ext4 /dev/vda1 rw\n"
    )
    assert mount_exposes_data_dir(other_device, MOUNTINFO_ROOTS, "/var/lib/node") is False
    not_under_root = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:1 /var/lib/node /mnt/leak rw - overlay overlay rw\n"
    )
    assert (
        mount_exposes_data_dir(not_under_root, MOUNTINFO_ROOTS, "/var/lib/node") is False
    )


def test_mountinfo_translation_decodes_escapes_and_needs_a_covering_mount():
    text = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:1 /data\\040dir /opt/reads/leak rw - overlay overlay rw\n"
    )
    assert mount_exposes_data_dir(text, MOUNTINFO_ROOTS, "/data dir") is True
    assert mount_exposes_data_dir(text, MOUNTINFO_ROOTS, "/other") is False
    with pytest.raises(OSError):
        _translate("/x", [])


def test_the_mountinfo_check_is_skipped_off_linux():
    assert _mountinfo_refuses({"reads": []}, "/var/lib/node") is False


def test_the_landlock_kernel_seam_wraps_each_syscall():
    log = []

    def syscall(number, *args):
        log.append((number, args))
        return 4 if number == landlock.SYS_CREATE_RULESET else 0

    kernel = landlock.Kernel(syscall=syscall, prctl=lambda *a: 0, get_errno=lambda: 13)
    assert kernel.abi() == 4
    assert kernel.create_ruleset(b"attr") == 4
    kernel.add_rule(4, landlock.RULE_PATH_BENEATH, b"rule")
    kernel.restrict(4)
    assert any(number == landlock.SYS_ADD_RULE for number, _ in log)


def test_the_landlock_kernel_raises_on_each_failure():
    fail = landlock.Kernel(
        syscall=lambda *a: -1, prctl=lambda *a: 0, get_errno=lambda: 1
    )
    with pytest.raises(OSError):
        fail.create_ruleset(b"attr")
    with pytest.raises(OSError):
        fail.add_rule(1, landlock.RULE_PATH_BENEATH, b"rule")
    with pytest.raises(OSError):
        fail.restrict(1)
    nnp = landlock.Kernel(syscall=lambda *a: 0, prctl=lambda *a: 1, get_errno=lambda: 1)
    with pytest.raises(OSError):
        nnp.restrict(1)
    assert landlock.Kernel(
        syscall=lambda *a: -5, prctl=lambda *a: 0, get_errno=lambda: 1
    ).abi() == 0


def test_inner_reads_json_from_a_pipe():
    from astral_exec import inner

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"a": 1}')
    os.close(write_fd)
    assert inner._read_json(read_fd) == {"a": 1}


def test_inner_applies_the_rlimits_in_order(monkeypatch):
    from astral_exec import inner

    calls = []
    monkeypatch.setattr(
        inner.resource, "setrlimit", lambda which, pair: calls.append((which, pair))
    )
    inner._apply_limits(
        {
            "RLIMIT_AS": 10,
            "RLIMIT_NOFILE": 20,
            "RLIMIT_FSIZE": 30,
            "RLIMIT_CORE": 0,
        }
    )
    assert calls == [
        (inner.resource.RLIMIT_NOFILE, (20, 20)),
        (inner.resource.RLIMIT_FSIZE, (30, 30)),
        (inner.resource.RLIMIT_CORE, (0, 0)),
        (inner.resource.RLIMIT_AS, (10, 10)),
    ]


def test_inner_fail_writes_a_reason_and_exits(monkeypatch):
    from astral_exec import inner

    read_fd, write_fd = os.pipe()

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(inner.os, "_exit", fake_exit)
    with pytest.raises(SystemExit) as caught:
        inner._fail(write_fd, "boom")
    assert caught.value.code == 126
    os.close(write_fd)
    assert os.read(read_fd, 64) == b"fail boom"
    os.close(read_fd)


def test_inner_main_reports_a_failure_through_the_status_descriptor(monkeypatch):
    from astral_exec import inner

    read_fd, write_fd = os.pipe()
    dead_read, dead_write = os.pipe()
    os.close(dead_read)
    os.close(dead_write)
    monkeypatch.setattr(
        inner.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code))
    )
    argv = [
        "--package-fd",
        "0",
        "--plan-fd",
        str(dead_read),
        "--env-fd",
        str(dead_write),
        "--status-fd",
        str(write_fd),
        "--",
        "/bin/true",
    ]
    with pytest.raises(SystemExit):
        inner.main(argv)
    os.close(write_fd)
    assert os.read(read_fd, 200).startswith(b"fail ")
    os.close(read_fd)
