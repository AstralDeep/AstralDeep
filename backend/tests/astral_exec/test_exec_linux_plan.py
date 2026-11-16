"""Pins the Linux plan, the Landlock rules, the seccomp program, the mount table check, the forwarder and the inner launcher's steps on any host, without bubblewrap.
The plan must equal every committed vector byte for byte, and the confined runs live in test_exec_linux_live.py.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading

import pytest
from astral_exec import Backend, ContextError, Policy, Refusal, canonjson, forwarder
from astral_exec import landlock, launch, linux
from astral_exec import seccomp, watchdog
from astral_exec.select import _mountinfo_refuses, _translate, mount_exposes_data_dir
from exec_vectors import (
    DATA_DIR,
    PROFILE_ERRORS,
    PROFILES,
    good_facts,
    loaded,
    minimal_policy,
)


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
    assert (
        verdict(nr["socket"], seccomp.AF_INET, seccomp.SOCK_STREAM) == seccomp.RET_ALLOW
    )
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
    assert landlock.workspace_rights(fs) == fs & ~(
        landlock.EXECUTE | landlock.MAKE_SOCK
    )
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
    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self.stats = 0

    def _table(self):
        table = self._snapshots[0]
        if len(self._snapshots) > 1:
            self._snapshots.pop(0)
        return table

    def stat(self, pid):
        self.stats += 1
        return self._snapshots[0].get(pid)

    def children(self, pid):
        table = self._snapshots[0]
        return sorted(child for child, proc in table.items() if proc.ppid == pid)

    def advance(self):
        if len(self._snapshots) > 1:
            self._snapshots.pop(0)


def _proc(ppid, exited=False, start=1):
    return watchdog.LProc(ppid, start, exited)


def test_the_linux_watch_excludes_bwrap_and_the_forwarder_whenever_it_samples():
    #  99 bwrap -> 100 reaper -> 101 tool -> 102 forwarder (oldest child)
    first = {
        99: _proc(1),
        100: _proc(99, start=1),
        101: _proc(100, start=2),
        102: _proc(101, start=3),
    }
    later = dict(first)
    later[103] = _proc(101, start=10)
    later[104] = _proc(103, start=11)
    system = _FakeSystem([first, later])
    watch = watchdog.LinuxWatch(99, system=system, hosted=True)
    assert watch.started is True
    assert watch.sample() == (1, 0)
    system.advance()
    assert watch.sample() == (3, 0)


def test_the_linux_watch_counts_every_child_when_there_is_no_forwarder():
    table = {99: _proc(1), 100: _proc(99), 101: _proc(100), 102: _proc(101)}
    watch = watchdog.LinuxWatch(99, system=_FakeSystem([table]))
    assert watch.sample() == (2, 0)


def test_the_linux_watch_excludes_an_exited_process():
    table = {
        99: _proc(1),
        100: _proc(99),
        101: _proc(100),
        103: _proc(101, exited=True),
    }
    watch = watchdog.LinuxWatch(99, system=_FakeSystem([table]))
    assert watch.sample() == (1, 0)


def test_the_linux_watch_walks_only_the_sandbox_tree():
    table = {99: _proc(1), 100: _proc(99), 101: _proc(100)}
    table.update({pid: _proc(1) for pid in range(200, 400)})
    system = _FakeSystem([table])
    watch = watchdog.LinuxWatch(99, system=system)
    system.stats = 0
    watch.sample()
    assert system.stats <= 6


class _Draining:
    def __init__(self, table):
        self.table = table

    def stat(self, pid):
        return self.table.get(pid)

    def children(self, pid):
        return sorted(c for c, proc in self.table.items() if proc.ppid == pid)


def test_the_linux_watch_end_kills_the_root_group_and_survivors():
    system = _Draining({99: _proc(1), 100: _proc(99), 101: _proc(100)})
    killed = []
    groups = []

    def kill(pid, sig):
        killed.append(pid)
        system.table = {99: _proc(1), 100: _proc(99)}

    watch = watchdog.LinuxWatch(
        99, system=system, kill=kill, killpg=lambda pid, sig: groups.append(pid)
    )
    assert watch.end() == set()
    assert 99 in groups and 101 in killed


def test_the_linux_watch_never_signals_a_root_that_left_or_was_reused():
    table = {99: _proc(1, start=5), 100: _proc(99), 101: _proc(100)}
    system = _Draining(table)
    groups, killed = [], []
    watch = watchdog.LinuxWatch(
        99,
        system=system,
        kill=lambda pid, sig: killed.append(pid),
        killpg=lambda pid, sig: groups.append(pid),
    )
    system.table = {99: _proc(1, start=6), 100: _proc(99), 101: _proc(100)}
    assert watch.sample() == (0, 0)
    assert watch.end() == set()
    system.table = {}
    assert watch.end() == set()
    assert groups == [] and killed == []


def test_a_watch_over_a_root_that_is_already_gone_did_not_start():
    watch = watchdog.LinuxWatch(99, system=_Draining({}))
    assert watch.started is False
    assert watch.end() == set()


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
    assert (
        mount_exposes_data_dir(other_device, MOUNTINFO_ROOTS, "/var/lib/node") is False
    )
    not_under_root = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:1 /var/lib/node /mnt/leak rw - overlay overlay rw\n"
    )
    assert (
        mount_exposes_data_dir(not_under_root, MOUNTINFO_ROOTS, "/var/lib/node")
        is False
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


def test_the_mountinfo_check_is_skipped_off_linux(monkeypatch):
    import sys

    select_module = sys.modules["astral_exec.select"]
    monkeypatch.setattr(select_module.sys, "platform", "darwin")
    document = minimal_policy(workspace="/work/space")
    assert _mountinfo_refuses(document, "/var/lib/node") is False


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
    assert (
        landlock.Kernel(
            syscall=lambda *a: -5, prctl=lambda *a: 0, get_errno=lambda: 1
        ).abi()
        == 0
    )


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


class _FakeLibc:
    def __init__(self, unshare=0, syscall=0):
        self._unshare = unshare
        self._syscall = syscall
        self.calls = []

    def unshare(self, flags):
        self.calls.append(("unshare", flags))
        return self._unshare

    def syscall(self, *args):
        self.calls.append(("syscall", args[0]))
        return self._syscall


class _Written:
    def __init__(self, sink, name):
        self.sink, self.name = sink, name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, text):
        self.sink[self.name] = text


def test_inner_enters_a_nested_user_and_mount_namespace(monkeypatch):
    from astral_exec import inner

    written = {}
    monkeypatch.setattr(
        inner, "open", lambda path, mode: _Written(written, path), raising=False
    )
    libc = _FakeLibc()
    inner._enter_namespaces(libc)
    assert libc.calls == [("unshare", inner.CLONE_NEWUSER | inner.CLONE_NEWNS)]
    uid, gid = os.getuid(), os.getgid()
    assert written == {
        "/proc/self/setgroups": "deny",
        "/proc/self/uid_map": f"{uid} {uid} 1",
        "/proc/self/gid_map": f"{gid} {gid} 1",
    }
    with pytest.raises(OSError):
        inner._enter_namespaces(_FakeLibc(unshare=-1))


class _Statvfs:
    def __init__(self, flag):
        self.f_flag = flag


def test_inner_remounts_the_workspace_noexec_and_checks_it(monkeypatch):
    from astral_exec import inner

    noexec = getattr(os, "ST_NOEXEC", 8)
    monkeypatch.setattr(inner.os, "ST_NOEXEC", noexec, raising=False)
    monkeypatch.setattr(inner.os, "statvfs", lambda path: _Statvfs(noexec))
    libc = _FakeLibc()
    inner._remount_noexec(libc, "/ws")
    assert libc.calls == [("syscall", inner.MOUNT_SETATTR)]
    with pytest.raises(OSError):
        inner._remount_noexec(_FakeLibc(syscall=-1), "/ws")
    monkeypatch.setattr(inner.os, "statvfs", lambda path: _Statvfs(0))
    with pytest.raises(OSError, match="noexec"):
        inner._remount_noexec(_FakeLibc(), "/ws")


class _Listener:
    def __init__(self):
        self.closed = False

    def fileno(self):
        return 9

    def close(self):
        self.closed = True


class _ForwarderModule:
    def __init__(self):
        self.listener = _Listener()
        self.built = None
        self.served = None

    def build_listener(self, host, port):
        self.built = (host, port)
        return self.listener

    def serve(self, listener, path):
        self.served = (listener, path)


def test_inner_starts_the_forwarder_in_a_child_and_closes_its_own_listener(
    monkeypatch,
):
    from astral_exec import inner

    module = _ForwarderModule()
    monkeypatch.setattr(inner.os, "fork", lambda: 4321)
    inner._start_forwarder(module, "127.0.0.1:3128", "/run/astral/egress.sock")
    assert module.built == ("127.0.0.1", 3128)
    assert module.listener.closed is True

    child = _ForwarderModule()
    ran = []
    monkeypatch.setattr(inner.os, "fork", lambda: 0)
    monkeypatch.setattr(
        inner, "_forwarder_child", lambda mod, lst, path: ran.append(path)
    )
    monkeypatch.setattr(
        inner.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code))
    )
    with pytest.raises(SystemExit):
        inner._start_forwarder(child, "127.0.0.1:3128", "/run/astral/egress.sock")
    assert ran == ["/run/astral/egress.sock"]


def test_the_forwarder_child_keeps_only_stdio_and_its_listener(monkeypatch):
    from astral_exec import inner

    actions = []

    class FakeOs:
        O_RDWR = os.O_RDWR

        def open(self, path, flags):
            actions.append(("open", path))
            return 7

        def dup2(self, source, target):
            actions.append(("dup2", source, target))

        def close(self, fd):
            actions.append(("close", fd))
            if fd == 11:
                raise OSError("already closed")

        def listdir(self, path):
            return ["0", "1", "2", "7", "9", "11"]

    module = _ForwarderModule()
    monkeypatch.setattr(inner, "os", FakeOs())
    inner._forwarder_child(module, module.listener, "/run/astral/egress.sock")
    closed = [entry[1] for entry in actions if entry[0] == "close"]
    assert ("open", inner.DEVNULL) in actions
    assert [entry[2] for entry in actions if entry[0] == "dup2"] == [0, 1, 2]
    assert 9 not in closed and 7 in closed and 11 in closed
    assert module.served == (module.listener, "/run/astral/egress.sock")


def _pipe_with(payload):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps(payload).encode())
    os.close(write_fd)
    return read_fd


def test_inner_run_applies_every_step_in_order_before_exec(monkeypatch):
    from astral_exec import inner, landlock, seccomp

    steps = []
    plan = linux.plan(Policy.parse(HOSTED, "linux"), "/run/x.sock")
    plan_fd = _pipe_with(plan)
    env_fd = _pipe_with({"A": "1"})
    status_read, status_write = os.pipe()
    monkeypatch.setattr(inner.ctypes, "CDLL", lambda *a, **k: _FakeLibc())
    monkeypatch.setattr(inner, "_enter_namespaces", lambda libc: steps.append("ns"))
    monkeypatch.setattr(
        inner, "_remount_noexec", lambda libc, ws: steps.append(("noexec", ws))
    )
    monkeypatch.setattr(
        inner,
        "_start_forwarder",
        lambda mod, listen, path: steps.append(("fwd", listen, path)),
    )
    monkeypatch.setattr(
        landlock, "apply", lambda p, hosted: steps.append(("landlock", hosted))
    )
    monkeypatch.setattr(seccomp, "install", lambda p: steps.append("seccomp"))
    monkeypatch.setattr(
        inner.resource, "setrlimit", lambda which, pair: steps.append(("rlimit", which))
    )
    monkeypatch.setattr(inner.signal, "signal", lambda sig, handler: None)
    monkeypatch.setattr(
        inner.os, "closerange", lambda low, high: steps.append(("close", low, high))
    )
    monkeypatch.setattr(
        inner.os, "execve", lambda path, argv, env: steps.append(("exec", path, env))
    )

    class Options:
        package_fd = 0
        status_fd = status_write

    Options.plan_fd = plan_fd
    Options.env_fd = env_fd
    inner._run(Options, ["/bin/true"])
    os.close(status_write)
    assert os.read(status_read, 16) == inner.READY
    os.close(status_read)
    names = [step if isinstance(step, str) else step[0] for step in steps]
    assert names.index("ns") < names.index("noexec") < names.index("fwd")
    assert names.index("fwd") < names.index("landlock") < names.index("seccomp")
    assert names.index("seccomp") < names.index("rlimit") < names.index("exec")
    assert ("rlimit", inner.resource.RLIMIT_AS) == [
        s for s in steps if s[0] == "rlimit"
    ][-1]
    assert ("close", 3, status_write) in steps
    assert ("close", status_write + 1, 65536) in steps
    assert steps[-1] == ("exec", "/bin/true", {"A": "1"})
    assert ("landlock", True) in steps


def test_inner_run_rejects_an_environment_that_is_not_an_object(monkeypatch):
    from astral_exec import inner

    class Options:
        package_fd = 0
        status_fd = 1

    Options.plan_fd = _pipe_with({})
    Options.env_fd = _pipe_with(["not", "an", "object"])
    with pytest.raises(ValueError):
        inner._run(Options, ["/bin/true"])


def test_inner_main_refuses_an_empty_tool_argv(monkeypatch):
    from astral_exec import inner

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        inner.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code))
    )
    argv = [
        "--package-fd",
        "0",
        "--plan-fd",
        "0",
        "--env-fd",
        "0",
        "--status-fd",
        str(write_fd),
        "--",
    ]
    with pytest.raises(SystemExit):
        inner.main(argv)
    os.close(write_fd)
    assert os.read(read_fd, 64) == b"fail no tool argv"
    os.close(read_fd)


EXPOSING_MOUNTINFO = (
    "1 0 0:1 / / rw - overlay overlay rw\n"
    "2 1 0:1 /var/lib/astral/node /usr/leak rw - overlay overlay rw\n"
)


def _linux_select(monkeypatch, opener):
    import sys

    select_module = sys.modules["astral_exec.select"]
    monkeypatch.setattr(select_module.sys, "platform", "linux")
    monkeypatch.setattr(select_module, "open", opener, raising=False)
    return select_module.select(loaded(minimal_policy()), good_facts(), DATA_DIR)


def test_select_refuses_a_mount_that_exposes_the_data_directory(monkeypatch):
    import io

    outcome = _linux_select(
        monkeypatch, lambda path, encoding: io.StringIO(EXPOSING_MOUNTINFO)
    )
    assert isinstance(outcome, Refusal)
    assert outcome.code == "exec.policy_overlaps_data"


def test_select_fails_closed_when_the_mount_table_cannot_be_read(monkeypatch):
    def unreadable(path, encoding):
        raise PermissionError(path)

    outcome = _linux_select(monkeypatch, unreadable)
    assert isinstance(outcome, Refusal)
    assert outcome.code == "exec.policy_overlaps_data"


def test_select_runs_linux_when_no_mount_exposes_the_data_directory(monkeypatch):
    import io

    clean = "1 0 0:1 / / rw - overlay overlay rw\nshort line\n"
    outcome = _linux_select(monkeypatch, lambda path, encoding: io.StringIO(clean))
    assert outcome == Backend("linux")


def test_the_linux_system_reads_ppid_start_state_and_children_from_proc(tmp_path):
    (tmp_path / "123").mkdir()
    (tmp_path / "123" / "stat").write_bytes(
        b"123 (a) b) S 77 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 4242 0 0\n"
    )
    (tmp_path / "124").mkdir()
    (tmp_path / "124" / "stat").write_bytes(
        b"124 (z) Z 123 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 4243 0 0\n"
    )
    for task, kids in (("123", b"124 130 \n"), ("200", b"131\n")):
        (tmp_path / "123" / "task" / task).mkdir(parents=True)
        (tmp_path / "123" / "task" / task / "children").write_bytes(kids)
    system = watchdog.LinuxSystem(str(tmp_path))
    assert system.stat(123) == watchdog.LProc(77, 4242, False)
    assert system.stat(124) == watchdog.LProc(123, 4243, True)
    assert system.stat(999) is None
    assert sorted(system.children(123)) == [124, 130, 131]
    assert system.children(999) == []


def test_the_linux_watch_end_tolerates_a_process_that_already_left():
    system = _Draining({99: _proc(1), 100: _proc(99), 101: _proc(100)})

    def kill(pid, sig):
        system.table = {99: _proc(1), 100: _proc(99)}
        raise ProcessLookupError(pid)

    def killpg(pid, sig):
        raise PermissionError(pid)

    watch = watchdog.LinuxWatch(99, system=system, kill=kill, killpg=killpg)
    assert watch.end() == set()


class _Flaky:
    def __init__(self, real, recv_error=False, send_error=False):
        self.real = real
        self.recv_error = recv_error
        self.send_error = send_error

    def fileno(self):
        return self.real.fileno()

    def recv(self, size):
        if self.recv_error:
            raise OSError("reset")
        return self.real.recv(size)

    def sendall(self, data):
        if self.send_error:
            raise OSError("broken")
        self.real.sendall(data)

    def shutdown(self, how):
        self.real.shutdown(how)

    def close(self):
        self.real.close()


@pytest.mark.parametrize("flaw", ["recv_error", "send_error"])
def test_the_relay_ends_cleanly_when_a_side_fails(flaw):
    client, down = socket.socketpair()
    up, server = socket.socketpair()
    client.sendall(b"x")
    server.close()
    downstream = _Flaky(down, recv_error=flaw == "recv_error")
    upstream = _Flaky(up, send_error=flaw == "send_error")
    worker = threading.Thread(target=forwarder.relay, args=(downstream, upstream))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    client.close()


def test_the_forwarder_connects_to_a_unix_socket(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind("s")
    listener.listen(1)
    upstream = forwarder._connect_unix("s")
    accepted, _ = listener.accept()
    upstream.sendall(b"hi")
    assert accepted.recv(8) == b"hi"
    for end in (upstream, accepted, listener):
        end.close()


def test_launch_refuses_a_linux_call_it_cannot_start(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = loaded(minimal_policy(workspace=str(workspace)))
    chosen = Backend("linux", "/usr/bin/bwrap")
    monkeypatch.setattr(launch.sys, "executable", "python3")
    with pytest.raises(ContextError, match="absolute"):
        launch._linux_call(policy, ["/bin/true"], {}, chosen, None)
    monkeypatch.setattr(launch.sys, "executable", "/opt/python/bin/python3")
    with pytest.raises(ContextError, match="read root"):
        launch._linux_call(policy, ["/bin/true"], {}, chosen, None)
    monkeypatch.setattr(launch.sys, "executable", "/usr/bin/python3")
    with pytest.raises(ContextError, match="bwrap"):
        launch._linux_call(
            policy, ["/bin/true"], {}, Backend("linux", "/tmp/bwrap"), None
        )


def test_a_linux_spawn_failure_closes_every_pipe_and_is_a_launch_failure(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = loaded(minimal_policy(workspace=str(workspace)))
    monkeypatch.setattr(launch.sys, "executable", "/usr/bin/python3")
    call = launch._linux_call(
        policy, ["/bin/true"], {}, Backend("linux", "/usr/bin/bwrap"), None
    )
    before = set(os.listdir("/dev/fd"))

    def broken(*args, **kwargs):
        raise OSError("no fork")

    monkeypatch.setattr(launch.subprocess, "Popen", broken)
    with pytest.raises(OSError):
        launch._linux_spawn(call, True, None)
    assert set(os.listdir("/dev/fd")) <= before | {"3"}
    outcome = launch._launch_linux(call, True, None, launch._log("sha256:x"))
    assert outcome == launch.Outcome("exec.launch_failed", None)


def test_the_launcher_refuses_a_backend_it_does_not_know(monkeypatch):
    monkeypatch.setattr(launch, "select", lambda *args: Backend("plan9"))
    outcome = launch._run(loaded(minimal_policy()), ["/bin/true"], {}, facts={})
    assert outcome.code == "exec.launch_failed"


def test_launch_close_ignores_a_missing_or_closed_descriptor():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    launch._close(None)
    launch._close(read_fd)
    os.close(write_fd)


def test_inner_fail_still_exits_when_the_status_pipe_is_gone(monkeypatch):
    from astral_exec import inner

    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    monkeypatch.setattr(
        inner.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code))
    )
    with pytest.raises(SystemExit) as caught:
        inner._fail(write_fd, "gone")
    assert caught.value.code == 126


def test_inner_forwarder_child_errors_never_reach_the_status_pipe(monkeypatch):
    from astral_exec import inner

    def explode(module, listener, path):
        raise RuntimeError("bad relay")

    monkeypatch.setattr(inner.os, "fork", lambda: 0)
    monkeypatch.setattr(inner, "_forwarder_child", explode)
    monkeypatch.setattr(
        inner.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code))
    )
    with pytest.raises(SystemExit) as caught:
        inner._start_forwarder(_ForwarderModule(), "127.0.0.1:3128", "/x.sock")
    assert caught.value.code == 0


def test_inner_main_returns_126_if_exec_ever_returns(monkeypatch):
    from astral_exec import inner

    monkeypatch.setattr(inner, "_run", lambda options, argv: None)
    argv = [
        "--package-fd",
        "0",
        "--plan-fd",
        "0",
        "--env-fd",
        "0",
        "--status-fd",
        "1",
        "--",
        "/bin/true",
    ]
    assert inner.main(argv) == 126


def test_select_refuses_a_data_directory_bind_under_the_workspace(monkeypatch):
    import io
    import sys

    text = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:1 /var/lib/astral/node /work/space/inner rw - overlay overlay rw\n"
    )
    select_module = sys.modules["astral_exec.select"]
    monkeypatch.setattr(select_module.sys, "platform", "linux")
    monkeypatch.setattr(
        select_module, "open", lambda path, encoding: io.StringIO(text), raising=False
    )
    policy = loaded(minimal_policy(workspace="/work/space"))
    outcome = select_module.select(policy, good_facts(), DATA_DIR)
    assert isinstance(outcome, Refusal)
    assert outcome.code == "exec.policy_overlaps_data"
