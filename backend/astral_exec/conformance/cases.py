"""The conformance cases: each one runs the probe tool through astral_exec.launch under a policy and asserts the outcome the execution policy contract promises, never a duration.
Cases cover reads, writes, executable mappings, the environment, direct and proxied network access, limits, strays and the refusals, and __init__.py runs them against one backend.
"""

from __future__ import annotations

import ctypes
import os
import secrets
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from astral_exec.constants import DENIED_LOG_SERVICES, DENIED_SERVICES, DNSSD_SERVICE
from astral_exec.errors import ExecRefused
from astral_exec.facts import Facts
from astral_exec.proxy import Proxy
from .context import (
    SECRET_NAME,
    Context,
    Failed,
    Run,
    processes_matching,
    process_table,
)
from .fixtures import PUBLIC, Upstream

MIB = 1 << 20
REACHABLE_SERVICE = "com.apple.system.opendirectoryd.libinfo"
LINUX_NET = {"ENETUNREACH", "ECONNREFUSED", "EHOSTUNREACH", "EAFNOSUPPORT"}
DENIED_NET = frozenset({"EPERM", "EACCES"})
FORK_DENIED = frozenset({"EPERM", "EAGAIN", "EACCES"})
SOCKET_DENIED = frozenset({"EPERM", "EACCES", "EAFNOSUPPORT"})
HOSTS = [{"host": "fixture.example.com", "port": port} for port in (80, 443)]
ELSEWHERE = {
    "rebind.example.com": ["127.0.0.1"],
    "mixed.example.com": [PUBLIC, "169.254.169.254"],
    "metadata.example.com": ["169.254.169.254"],
    "loopback6.example.com": ["::1"],
    "mapped.example.com": ["::ffff:127.0.0.1"],
    "private.example.com": ["10.0.0.5"],
}
REFUSED_TARGETS = (
    "other.example.com:80",
    "fixture.example.com:22",
    "fixture.example.com:8080",
    "127.0.0.1:80",
    "169.254.169.254:80",
    "[::1]:80",
    "localhost:80",
    *(f"{name}:80" for name in ELSEWHERE),
)
TARGETS = ("fixture.example.com:80", *REFUSED_TARGETS)
MEMORY_LIMIT = 128 * MIB
MEMORY_CEILING = 1024 * MIB
Check = Callable[[Context], None]


@dataclass(frozen=True)
class Case:
    name: str
    run: Check
    platforms: tuple[str, ...]
    needs_backend: bool


CASES: list[Case] = []


def case(
    name: str, *, platforms: tuple[str, ...] = ("darwin", "linux"), backend: bool = True
):
    def register(function: Check) -> Check:
        CASES.append(Case(name, function, platforms, backend))
        return function

    return register


def expect(run: Run, check: str, allowed) -> None:
    value = run.get(check)
    if not isinstance(value, str) or value not in allowed:
        raise Failed(f"{check} was {value!r}, expected one of {sorted(allowed)}")


def expect_equal(actual, wanted, what: str) -> None:
    if actual != wanted:
        raise Failed(f"{what} was {actual!r}, expected {wanted!r}")


def net_denied(ctx: Context) -> frozenset[str]:
    return DENIED_NET | LINUX_NET if ctx.linux else DENIED_NET


def base(ctx: Context) -> Run:
    def make() -> Run:
        for stale in ("/tmp/astral-conformance.txt", "/usr/astral-conformance.txt"):
            if os.path.exists(stale):
                os.unlink(stale)
        outsider = subprocess.Popen(["/bin/sleep", "600"])
        os.environ[SECRET_NAME] = secrets.token_hex(16)
        try:
            run = ctx.run(
                "base", reads=[ctx.readable], extra={"outsider": outsider.pid}
            )
        finally:
            del os.environ[SECRET_NAME]
            alive = outsider.poll() is None
            outsider.kill()
            outsider.wait()
        run.checks["outsider_alive"] = alive
        return run

    return ctx.remembered("base", make)


def hosted(ctx: Context) -> tuple[Run, Upstream]:
    def make() -> Run:
        upstream = Upstream(
            ctx.fixtures, {"fixture.example.com": [PUBLIC], **ELSEWHERE}
        )
        other = Proxy(HOSTS, "tcp")
        other.start()
        try:
            hosts = [*HOSTS, *({"host": name, "port": 80} for name in ELSEWHERE)]
            run = ctx.run(
                "hosted",
                reads=[ctx.readable],
                hosts=hosts,
                extra={
                    "targets": list(TARGETS),
                    "other_proxy_port": int(other.address),
                },
                options=upstream.options,
            )
        finally:
            other.close()
        run.upstream = upstream
        return run

    run = ctx.remembered("hosted", make)
    return run, run.upstream


def untouched(*items: tuple[str, str | None]) -> None:
    problems = []
    for path, text in items:
        if not os.path.exists(path):
            if text is not None:
                problems.append(f"{path} vanished")
        elif text is None:
            os.unlink(path)
            problems.append(f"the tool created {path}")
        elif Path(path).read_text(encoding="utf-8") != text:
            problems.append(f"the tool changed {path}")
    if problems:
        raise Failed("; ".join(problems))


@case("fs.read_outside_policy_denied")
def _(ctx: Context) -> None:
    expect(base(ctx), "read_outside", ctx.denied_files())


@case("fs.read_inside_policy_allowed")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "read_read_root", {"ok"})
    expect(run, "read_workspace", {"ok"})


@case("fs.read_node_data_dir_denied")
def _(ctx: Context) -> None:
    expect(base(ctx), "read_data_dir", ctx.denied_files())


@case("fs.read_home_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "read_home_ssh", ctx.denied_files())
    expect(run, "read_home_dotfile", ctx.denied_files())


@case("fs.list_outside_denied")
def _(ctx: Context) -> None:
    expect(base(ctx), "list_outside", ctx.denied_files())


@case("fs.write_workspace_allowed")
def _(ctx: Context) -> None:
    expect(base(ctx), "write_workspace", {"ok"})


@case("fs.write_outside_workspace_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    untouched(
        (f"{ctx.outside}/escape.txt", None),
        (f"{ctx.outside}/secret.txt", "outside canary"),
        ("/tmp/astral-conformance.txt", None),
        ("/usr/astral-conformance.txt", None),
    )
    for check in (
        "write_outside",
        "write_outside_existing",
        "write_tmp",
        "write_system",
    ):
        expect(run, check, ctx.denied_files())


@case("fs.write_read_root_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    untouched((f"{ctx.readable}/new.txt", None))
    expect(run, "write_read_root", ctx.denied_files())


@case("fs.write_home_startup_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    untouched((f"{ctx.home}/.zshrc", "shell startup canary"))
    expect(run, "write_home_startup", ctx.denied_files())


@case("fs.symlink_escape_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    untouched((f"{ctx.outside}/via-link.txt", None))
    expect(run, "write_via_symlink", ctx.denied_files())


@case("fs.hardlink_escape_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    untouched((f"{run.workspace}/hard", None))
    expect(run, "hardlink_outside", ctx.denied_files() | {"EXDEV"})


@case("fs.rename_escape_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    untouched((f"{ctx.outside}/moved.txt", None))
    expect(run, "rename_outside", ctx.denied_files() | {"EXDEV"})


@case("fs.keychain_denied", platforms=("darwin",))
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "read_system_keychain", ctx.denied_files())
    expect(run, "read_framework_keychain", ctx.denied_files())
    granted = ctx.run("keychain", reads=[ctx.home])
    expect(granted, "read_home_dotfile", {"ok"})
    expect(granted, "read_home_keychain", ctx.denied_files())


@case("fs.workspace_map_executable_denied")
def _(ctx: Context) -> None:
    library = ctx.library()
    if library is None:
        raise Failed("no extension module was found to copy for the mapping check")

    def prepare(workspace: str) -> None:
        copy = f"{workspace}/copied.so"
        shutil.copyfile(library, copy)
        ctypes.CDLL(copy)

    run = ctx.run("map", prepare=prepare)
    expect(run, "map_original", {"ok"})
    expect(run, "map_copy", {"OSError"})


def exec_attempt(ctx: Context, mode: str) -> Run:
    control = ctx.run(mode, extra={"copy": False})
    if "EXEC-RAN" not in control.outcome.stdout.decode("utf-8", "replace"):
        raise Failed(
            f"the control exec of the interpreter did not run: {control.describe()}"
        )
    run = ctx.run(mode, extra={"copy": True}, limits={"file_bytes": 1 << 29})
    if "EXEC-RAN" in run.outcome.stdout.decode("utf-8", "replace"):
        raise Failed("a file in the workspace was executed")
    return run


@case("fs.workspace_exec_script_denied")
def _(ctx: Context) -> None:
    run = ctx.run("exec_script")
    if "EXEC-RAN" in run.outcome.stdout.decode("utf-8", "replace"):
        raise Failed("a script in the workspace was executed")
    expect(run, "exec_script", {"EPERM", "EACCES"})


@case("fs.workspace_exec_binary_denied")
def _(ctx: Context) -> None:
    run = exec_attempt(ctx, "exec_binary")
    expect(run, "exec_binary", {"EPERM", "EACCES"})


@case("fs.workspace_exec_loader_denied", platforms=("linux",))
def _(ctx: Context) -> None:
    loader = ctx.loader()
    if loader is None:
        raise Failed("no dynamic loader was found for the loader check")
    run = exec_attempt(ctx, "exec_loader")
    if run.completed:
        raise Failed("the loader ran a workspace file to completion")


@case("data.policy_overlap_refused")
def _(ctx: Context) -> None:
    workspace = f"{ctx.workdir}/alt"
    os.makedirs(workspace, exist_ok=True)
    try:
        policy = ctx.policy(workspace, reads=[ctx.data])
    except ExecRefused as error:
        expect_equal(error.code, "exec.policy_overlaps_data", "the refusal code")
        return
    run = ctx.launch(policy, "marker", workspace)
    expect_equal(run.outcome.code, "exec.policy_overlaps_data", "the outcome code")
    untouched((f"{workspace}/ran", None))


@case("env.cleared")
def _(ctx: Context) -> None:
    run = base(ctx)
    environ = run.get("environ")
    if SECRET_NAME in environ:
        raise Failed("a node secret reached the sandbox environment")
    expect_equal(run.get("leaked"), [], "parent variables in the sandbox")
    if "CONFORMANCE_PASSED" not in environ:
        raise Failed("the variable the caller passed is missing")


@case("env.secret_not_in_process_listing")
def _(ctx: Context) -> None:
    secret = secrets.token_hex(16)
    workspace = ctx.workspace()
    policy = ctx.policy(workspace)
    holder: list[Run] = []
    worker = threading.Thread(
        target=lambda: holder.append(
            ctx.launch(policy, "wait", workspace, env={"CONFORMANCE_SECRET": secret})
        )
    )
    worker.start()
    started = Path(workspace, "started")
    release = Path(workspace, "release")
    try:
        for _ in range(3000):
            if started.exists() or not worker.is_alive():
                break
            threading.Event().wait(0.02)
        listed = [pid for pid, command in process_table() if secret in command]
    finally:
        release.write_text("1", encoding="utf-8")
        worker.join(60)
    if not started.exists():
        raise Failed(
            f"the tool never started: {holder[0].describe() if holder else 'no result'}"
        )
    if listed:
        raise Failed("the secret appears in a process command line")


@case("net.public_address_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "tcp_public", net_denied(ctx))
    expect(run, "udp_public", net_denied(ctx))


@case("net.loopback_ports_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "tcp_node_port", net_denied(ctx))
    expect_equal(run.seen["tcp"], 0, "connections that reached the node port fixture")


@case("net.udp_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "udp_loopback", net_denied(ctx))
    expect_equal(run.seen["udp"], 0, "datagrams that reached the fixture")


@case("net.unix_socket_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "unix_fixture", net_denied(ctx) | ({"ENOENT"} if ctx.linux else set()))
    expect_equal(run.seen["unix"], 0, "connections that reached the Unix fixture")


@case("net.mdns_responder_denied", platforms=("darwin",))
def _(ctx: Context) -> None:
    expect(base(ctx), "unix_mdnsresponder", net_denied(ctx))


@case("net.dns_service_denied", platforms=("darwin",))
def _(ctx: Context) -> None:
    services = [DNSSD_SERVICE, *DENIED_SERVICES, *DENIED_LOG_SERVICES]
    control = ctx.control("mach", {"services": [DNSSD_SERVICE]})
    expect_equal(
        control.checks.get(f"mach:{DNSSD_SERVICE}"),
        "ok",
        "the lookup outside the sandbox",
    )
    run = ctx.run("mach", extra={"services": [*services, REACHABLE_SERVICE]})
    expect(run, f"mach:{REACHABLE_SERVICE}", {"ok"})
    for name in services:
        if run.get(f"mach:{name}") == "ok":
            raise Failed(f"the sandbox could look up {name}")


@case("net.listen_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "listen_loopback", net_denied(ctx))
    expect(run, "listen_all", net_denied(ctx))


@case("net.socket_families_denied", platforms=("linux",))
def _(ctx: Context) -> None:
    run = base(ctx)
    for check in (
        "socket_inet_stream",
        "socket_inet6_stream",
        "socket_inet_dgram",
        "socket_inet_raw",
        "socket_unix",
        "socket_netlink",
        "socket_packet",
    ):
        expect(run, check, SOCKET_DENIED)
    expect(run, "socketpair", {"ok"})


@case("net.io_uring_denied", platforms=("linux",))
def _(ctx: Context) -> None:
    expect(base(ctx), "io_uring", {"EPERM"})


@case("net.hosted_socket_families", platforms=("linux",))
def _(ctx: Context) -> None:
    run, _upstream = hosted(ctx)
    expect(run, "socket_inet_stream", {"ok"})
    expect(run, "socket_inet6_stream", {"ok", "EAFNOSUPPORT"})
    for check in (
        "socket_inet_dgram",
        "socket_inet_raw",
        "socket_unix",
        "socket_netlink",
    ):
        expect(run, check, SOCKET_DENIED)


@case("proxy.allowed_host_tunnels")
def _(ctx: Context) -> None:
    run, upstream = hosted(ctx)
    expect_equal(
        run.get("connect:fixture.example.com:80"),
        [200, "PONG:ping"],
        "the tunnel to the policy host",
    )
    if not run.get("proxy_env").startswith("http://127.0.0.1:"):
        raise Failed("the proxy variables do not name the loopback proxy")
    if (PUBLIC, 80) not in upstream.dials:
        raise Failed("the proxy never dialed the resolved address")


@case("proxy.https_through_proxy")
def _(ctx: Context) -> None:
    if ctx.fixtures.cert is None:
        raise Failed(f"no TLS fixture: {ctx.fixtures.cert_error}")
    run, _upstream = hosted(ctx)
    expect(run, "https_via_proxy", {"ok:fixture-ok"})
    expect(run, "https_by_environment", {"ok:fixture-ok"})


@case("proxy.off_policy_host_refused")
def _(ctx: Context) -> None:
    run, upstream = hosted(ctx)
    status = run.get("connect:other.example.com:80")
    expect_equal(status[0], 403, "the proxy status for an unlisted host")
    if ("other.example.com", 80) in upstream.lookups:
        raise Failed("the proxy resolved a host that is not in the policy")


@case("proxy.off_policy_port_refused")
def _(ctx: Context) -> None:
    run, upstream = hosted(ctx)
    for target in ("fixture.example.com:22", "fixture.example.com:8080"):
        expect_equal(
            run.get("connect:" + target)[0], 403, f"the proxy status for {target}"
        )
    if any(port not in (80, 443) for _host, port in upstream.lookups):
        raise Failed("the proxy resolved an off-policy port")


@case("proxy.ip_literals_refused")
def _(ctx: Context) -> None:
    run, upstream = hosted(ctx)
    for target in ("127.0.0.1:80", "169.254.169.254:80", "[::1]:80", "localhost:80"):
        expect_equal(
            run.get("connect:" + target)[0], 403, f"the proxy status for {target}"
        )
    if any(
        host in ("127.0.0.1", "169.254.169.254", "::1", "localhost")
        for host, _port in upstream.lookups
    ):
        raise Failed("the proxy looked up a literal or an unlisted name")


@case("proxy.resolves_elsewhere_refused")
def _(ctx: Context) -> None:
    run, upstream = hosted(ctx)
    for name in ELSEWHERE:
        expect_equal(
            run.get(f"connect:{name}:80")[0], 403, f"the proxy status for {name}"
        )
    if any(address != PUBLIC for address, _port in upstream.dials):
        raise Failed(f"the proxy dialed a non-public address: {upstream.dials}")


@case("proxy.other_sandbox_ports_unreachable")
def _(ctx: Context) -> None:
    run, _upstream = hosted(ctx)
    expect(run, "tcp_other_proxy", net_denied(ctx))
    expect(run, "tcp_node_port", net_denied(ctx))
    expect(run, "tcp_echo_direct", net_denied(ctx))
    expect_equal(run.seen["tcp"], 0, "connections that reached the node port fixture")


@case("proxy.udp_and_unix_denied_with_hosts")
def _(ctx: Context) -> None:
    run, _upstream = hosted(ctx)
    expect(run, "udp_loopback", net_denied(ctx))
    expect(run, "unix_fixture", net_denied(ctx) | ({"ENOENT"} if ctx.linux else set()))
    expect_equal(run.seen["udp"], 0, "datagrams that reached the fixture")
    expect_equal(run.seen["unix"], 0, "connections that reached the Unix fixture")


@case("proc.single_process_by_default")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "fork", FORK_DENIED)
    if run.get("spawn") == "ok":
        raise Failed("a child process started under a one-process limit")
    expect(run, "thread", {"ok"})


@case("proc.limit_enforced")
def _(ctx: Context) -> None:
    token = str(secrets.randbelow(9000000) + 1000000)
    try:
        run = ctx.run(
            "sleepers",
            limits={"processes": 4},
            extra={"token": token, "count": 16, "linger": 30},
        )
        expect_equal(run.outcome.code, "exec.process_limit", "the outcome code")
        left = processes_matching(token)
        if left:
            raise Failed(f"{len(left)} processes outlived the call")
    finally:
        for pid in processes_matching(token):
            os.kill(pid, signal.SIGKILL)


@case("proc.no_strays")
def _(ctx: Context) -> None:
    token = str(secrets.randbelow(9000000) + 1000000)
    try:
        run = ctx.run(
            "sleepers",
            limits={"processes": 8},
            extra={"token": token, "count": 4, "linger": 0},
        )
        run.require()
        if run.get("spawned") < 1:
            raise Failed("the tool could not start any process")
        left = processes_matching(token)
        if left:
            raise Failed(f"{len(left)} processes outlived the call")
    finally:
        for pid in processes_matching(token):
            os.kill(pid, signal.SIGKILL)


@case("signal.outside_process_denied")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect(run, "kill_outsider", {"EPERM", "ESRCH"})
    if not run.checks["outsider_alive"]:
        raise Failed("a process outside the sandbox was signalled")


@case("limit.wall_time")
def _(ctx: Context) -> None:
    run = ctx.run("time", wall_ms=2000)
    expect_equal(run.outcome.code, "exec.time_limit", "the outcome code")
    expect_equal(run.checks.get("started"), "ok", "the tool start")


@case("limit.memory")
def _(ctx: Context) -> None:
    run = ctx.run(
        "memory",
        limits={"memory_bytes": MEMORY_LIMIT},
        extra={"ceiling": MEMORY_CEILING},
    )
    expect_equal(run.checks.get("ready"), "ok", "the tool start")
    peak = run.checks.get("allocated", 0)
    if peak >= MEMORY_CEILING or run.checks.get("stopped") == "ceiling":
        raise Failed(
            f"the tool allocated {peak} bytes past a {MEMORY_LIMIT} byte limit"
        )
    if ctx.linux:
        expect_equal(
            run.checks.get("stopped"), "MemoryError", "how the allocation ended"
        )
        if peak > MEMORY_LIMIT:
            raise Failed(f"the address space limit let {peak} bytes through")
    else:
        expect_equal(run.outcome.code, "exec.memory_limit", "the outcome code")


@case("limit.file_size")
def _(ctx: Context) -> None:
    run = ctx.run("file_size", limits={"file_bytes": MIB}, extra={"chunks": 64})
    expect_equal(run.outcome.code, "exec.file_limit", "the outcome code")
    written = os.path.getsize(f"{run.workspace}/big.bin")
    if written > MIB:
        raise Failed(f"the tool wrote {written} bytes past a {MIB} byte limit")


@case("limit.open_files")
def _(ctx: Context) -> None:
    run = ctx.run(
        "open_files",
        reads=[ctx.readable],
        limits={"open_files": 64},
        extra={"attempts": 512, "readable_file": f"{ctx.readable}/readable.txt"},
    ).require()
    if run.get("opened") > 64:
        raise Failed(f"the tool held {run.get('opened')} files past a limit of 64")
    expect_equal(run.get("refused"), "EMFILE", "how the open ended")


@case("limit.core_dumps_off")
def _(ctx: Context) -> None:
    run = base(ctx)
    expect_equal(run.get("rlimit_core"), [0, 0], "the core limit")
    expect_equal(run.get("rlimit_nofile"), [256, 256], "the open file limit")
    expect_equal(run.get("rlimit_fsize"), [1 << 24, 1 << 24], "the file size limit")


def refusal_facts(ctx: Context, **changes) -> Facts:
    facts = Facts({**ctx.facts, **changes})
    facts.bwrap_path = ctx.facts.bwrap_path
    facts.proc_children = ctx.facts.proc_children
    return facts


def refused_run(ctx: Context, facts: Facts, code: str, reason: str | None) -> None:
    run = ctx.run("marker", facts=facts)
    expect_equal(run.outcome.code, code, "the outcome code")
    refusal = run.outcome.refusal
    if refusal is None or refusal.reason != reason:
        raise Failed(
            f"the refusal reason was {getattr(refusal, 'reason', None)!r}, expected {reason!r}"
        )
    untouched((f"{run.workspace}/ran", None))


@case("refusal.no_backend", backend=False)
def _(ctx: Context) -> None:
    facts = refusal_facts(
        ctx,
        landlock_abi=0,
        bwrap=False,
        userns=False,
        sandbox_exec=False,
        app_sandboxed=False,
    )
    facts.bwrap_path = None
    reason = "landlock_abi" if ctx.linux else "sandbox_exec_missing"
    refused_run(ctx, facts, "exec.backend_unavailable", reason)


@case("refusal.shadow_mode", backend=False)
def _(ctx: Context) -> None:
    refused_run(ctx, refusal_facts(ctx, mode="shadow"), "exec.shadow_refused", None)


@case("refusal.app_sandboxed", platforms=("darwin",), backend=False)
def _(ctx: Context) -> None:
    refused_run(
        ctx,
        refusal_facts(ctx, app_sandboxed=True),
        "exec.backend_unavailable",
        "app_sandboxed",
    )


@case("refusal.root_node", platforms=("linux",), backend=False)
def _(ctx: Context) -> None:
    refused_run(
        ctx, refusal_facts(ctx, euid=0), "exec.backend_unavailable", "root_node"
    )


@case("refusal.proc_children_missing", platforms=("linux",), backend=False)
def _(ctx: Context) -> None:
    facts = refusal_facts(ctx)
    facts.proc_children = False
    refused_run(ctx, facts, "exec.backend_unavailable", "proc_children_missing")
