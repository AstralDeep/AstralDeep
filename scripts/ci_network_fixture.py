"""Provides a bounded synthetic TLS/DNS peer in an owned Linux network namespace.
CI qualifies its lifecycle independently; enforcement observers reuse its authenticated
responses and fresh-connection assertions without granting firewall authority.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import time

ROOT = Path("/run/astral-ci-peer")
PEER = "192.0.2.2"
HOST = "192.0.2.1"
NETWORK = "192.0.2.0/30"
PORTS = {"allowed": 18443, "excluded": 19443, "address": 443, "dns": 53}
ENV = {"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "LANG": "C", "LC_ALL": "C"}
LIFETIME = 180
MAX_DOCUMENT = 16384


class FixtureError(RuntimeError):
    pass


def failure_code(error):
    if isinstance(error, FixtureError) and re.fullmatch(
        "[A-Za-z_:]{1,120}", str(error)
    ):
        return str(error)
    return type(error).__name__


def identity(value):
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{16}", value) is None:
        raise FixtureError("fixture_identity_invalid")
    return {
        "id": value,
        "namespace": "acp-" + value,
        "host_link": "ach" + value[:12],
        "peer_link": "acp" + value[:12],
        "host": HOST,
        "peer": PEER,
    }


def owned_metadata(metadata, *, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not expected(metadata.st_mode)
    ):
        raise FixtureError("untrusted_fixture_file")


def read_owned(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        owned_metadata(os.fstat(descriptor))
        data = os.read(descriptor, MAX_DOCUMENT + 1)
        if len(data) > MAX_DOCUMENT:
            raise FixtureError("fixture_document_too_large")
        return json.loads(data)
    finally:
        os.close(descriptor)


def _write(path, value):
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
    )
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink()


def _run(*arguments):
    result = subprocess.run(
        arguments, env=ENV, capture_output=True, timeout=10, check=False
    )
    if result.returncode:
        raise FixtureError("fixture_os_command_failed:" + Path(arguments[0]).name)
    return result.stdout


def _ip(*arguments):
    return _run("/usr/sbin/ip", *arguments)


def assert_network_available(routes, addresses):
    network = ipaddress.IPv4Network(NETWORK)
    if (
        not isinstance(routes, list)
        or not isinstance(addresses, list)
        or len(routes) + len(addresses) > 4096
    ):
        raise FixtureError("fixture_network_inventory_invalid")
    for route in routes:
        destination = route.get("dst", "default")
        if destination not in ("default", "0.0.0.0/0") and network.overlaps(
            ipaddress.IPv4Network(destination, strict=False)
        ):
            raise FixtureError("fixture_route_conflict")
    for interface in addresses:
        for address in interface.get("addr_info", []):
            if (
                address.get("family") == "inet"
                and ipaddress.IPv4Address(address["local"]) in network
            ):
                raise FixtureError("fixture_address_conflict")


def _directory(value):
    return ROOT / identity(value)["id"]


def _root_directory():
    try:
        ROOT.mkdir(mode=0o755)
    except FileExistsError:
        pass
    owned_metadata(ROOT.lstat(), directory=True)


def _dns_question(identifier):
    return (
        struct.pack("!HHHHHH", identifier, 0x0100, 1, 0, 0, 0)
        + b"\x07fixture\x07invalid\0\0\1\0\1"
    )


def _dns_answer(query):
    if len(query) != len(_dns_question(0)) or query[2:] != _dns_question(0)[2:]:
        return None
    return (
        query[:2]
        + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
        + query[12:]
        + bytes.fromhex("c00c000100010000003c0004c0000202")
    )


def _listeners(directory):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "ca.pem", directory / "key.pem")
    selection = selectors.DefaultSelector()
    try:
        for name, port in PORTS.items():
            kind = socket.SOCK_DGRAM if name == "dns" else socket.SOCK_STREAM
            connection = socket.socket(socket.AF_INET, kind)
            try:
                connection.bind((PEER, port))
                if kind == socket.SOCK_STREAM:
                    connection.listen(4)
                selection.register(connection, selectors.EVENT_READ, name)
            except BaseException:
                connection.close()
                raise
        return selection, context
    except BaseException:
        for key in selection.get_map().values():
            key.fileobj.close()
        selection.close()
        raise


def _serve(selection, context, nonce):
    counters = dict.fromkeys(PORTS, 0)
    finished = False
    deadline = time.monotonic() + LIFETIME
    try:
        while not finished and time.monotonic() < deadline:
            for key, _mask in selection.select(0.2):
                if key.data == "dns":
                    query, address = key.fileobj.recvfrom(512)
                    response = _dns_answer(query)
                    if response is not None:
                        counters["dns"] += 1
                        key.fileobj.sendto(response, address)
                    continue
                raw, _address = key.fileobj.accept()
                raw.settimeout(2)
                try:
                    with context.wrap_socket(raw, server_side=True) as connection:
                        request = connection.recv(1024)
                        route = request.split(b"\r\n", 1)[0]
                        if route == b"GET /probe HTTP/1.1":
                            counters[key.data] += 1
                        elif route == f"GET /close/{nonce} HTTP/1.1".encode():
                            finished = True
                        elif route != b"GET /health HTTP/1.1":
                            continue
                        body = json.dumps({"nonce": nonce, "health": counters}).encode()
                        connection.sendall(
                            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                            + str(len(body)).encode()
                            + b"\r\n\r\n"
                            + body
                        )
                except (OSError, ssl.SSLError):
                    raw.close()
    finally:
        for key in selection.get_map().values():
            key.fileobj.close()
        selection.close()


def _peer(namespace, directory, nonce, ready):
    for received in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(received, signal.SIG_DFL)
    descriptor = os.open("/run/netns/" + namespace, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if library.setns(descriptor, 0x40000000) != 0:
            raise FixtureError("fixture_setns_failed")
    finally:
        os.close(descriptor)
    selection, context = _listeners(directory)
    if library.prctl(38, 1, 0, 0, 0) != 0:
        raise FixtureError("fixture_no_new_privileges_failed")
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    status = Path("/proc/self/status").read_text()
    capabilities = dict(re.findall(r"^(CapEff|CapPrm):\s*([0-9a-f]+)$", status, re.M))
    if set(capabilities) != {"CapEff", "CapPrm"} or any(
        int(value, 16) for value in capabilities.values()
    ):
        raise FixtureError("fixture_capabilities_retained")
    os.write(ready, b"1")
    os.close(ready)
    _serve(selection, context, nonce)


def _reap(pid):
    if pid is None:
        return
    if type(pid) is not int or pid < 1:
        raise FixtureError("fixture_child_identity_invalid")
    done, _status = os.waitpid(pid, os.WNOHANG)
    if done == 0:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)


def process_identity(pid):
    if type(pid) is not int or pid < 1:
        raise FixtureError("fixture_process_identity_invalid")
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[19]), fields[0]


def process_exited(process):
    try:
        start, state = process_identity(process["pid"])
    except FileNotFoundError:
        return True
    return start != process["start"] or state == "Z"


def _peer_child(value, directory, nonce, read_ready, write_ready):
    status = 1
    try:
        os.close(read_ready)
        _peer(value["namespace"], directory, nonce, write_ready)
        status = 0
    except BaseException as error:
        try:
            os.write(write_ready, ("E" + failure_code(error)).encode())
        except OSError:
            pass
    finally:
        os._exit(status)


def _cleanup(resources):
    value = resources["identity"]
    if resources.get("ifindex") is not None:
        path = Path("/sys/class/net") / value["host_link"] / "ifindex"
        if path.exists():
            if int(path.read_text()) != resources["ifindex"]:
                raise FixtureError("fixture_link_identity_changed")
            _ip("link", "del", value["host_link"])
    if resources.get("namespace_inode") is not None:
        path = Path("/run/netns") / value["namespace"]
        if path.exists():
            if path.stat().st_ino != resources["namespace_inode"]:
                raise FixtureError("fixture_namespace_identity_changed")
            _ip("netns", "del", value["namespace"])
    if resources.get("namespace_pending") or resources.get("link_pending"):
        raise FixtureError("fixture_cleanup_uncertain")


def _supervise(value, directory):
    resources = {"identity": value}
    child = None
    outcome = "failed"
    failure = "fixture_lifetime_expired"
    previous_handlers = {}

    def interrupted(_received, _frame):
        raise FixtureError("fixture_supervisor_interrupted")

    for received in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        previous_handlers[received] = signal.signal(received, interrupted)
    try:
        assert_network_available(
            json.loads(_ip("-j", "-4", "route", "show", "table", "all")),
            json.loads(_ip("-j", "-4", "address", "show")),
        )
        if (Path("/run/netns") / value["namespace"]).exists():
            raise FixtureError("fixture_namespace_conflict")
        resources["namespace_pending"] = True
        _ip("netns", "add", value["namespace"])
        resources["namespace_inode"] = (
            (Path("/run/netns") / value["namespace"]).stat().st_ino
        )
        resources["namespace_pending"] = False
        if any(
            (Path("/sys/class/net") / value[name]).exists()
            for name in ("host_link", "peer_link")
        ):
            raise FixtureError("fixture_link_conflict")
        resources["link_pending"] = True
        _ip(
            "link",
            "add",
            value["host_link"],
            "type",
            "veth",
            "peer",
            "name",
            value["peer_link"],
        )
        resources["ifindex"] = int(
            (Path("/sys/class/net") / value["host_link"] / "ifindex").read_text()
        )
        resources["link_pending"] = False
        _ip("link", "set", value["peer_link"], "netns", value["namespace"])
        _ip("addr", "add", HOST + "/30", "dev", value["host_link"])
        _ip("link", "set", value["host_link"], "up")
        _ip(
            "-n",
            value["namespace"],
            "addr",
            "add",
            PEER + "/30",
            "dev",
            value["peer_link"],
        )
        _ip("-n", value["namespace"], "link", "set", value["peer_link"], "up")
        _ip("-n", value["namespace"], "link", "set", "lo", "up")
        _run(
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(directory / "key.pem"),
            "-out",
            str(directory / "ca.pem"),
            "-subj",
            "/CN=fixture.invalid",
            "-addext",
            "subjectAltName=DNS:fixture.invalid,DNS:pypi.org",
        )
        (directory / "key.pem").chmod(0o600)
        nonce = secrets.token_hex(16)
        read_ready, write_ready = os.pipe()
        child = os.fork()
        if child == 0:
            _peer_child(value, directory, nonce, read_ready, write_ready)
        os.close(write_ready)
        with os.fdopen(read_ready, "rb") as ready:
            selected = selectors.DefaultSelector()
            selected.register(ready, selectors.EVENT_READ)
            try:
                if not selected.select(10):
                    raise FixtureError("fixture_peer_not_ready")
                response = ready.read(128)
                if response != b"1":
                    raise FixtureError(
                        "fixture_peer_not_ready:"
                        + response[1:].decode("ascii", "replace")
                    )
            finally:
                selected.close()
        public = {
            **value,
            "nonce": nonce,
            "ca_sha256": hashlib.sha256(
                (directory / "ca.pem").read_bytes()
            ).hexdigest(),
            "supervisor": {
                "pid": os.getpid(),
                "start": process_identity(os.getpid())[0],
            },
        }
        resources["supervisor"] = public["supervisor"]
        _write(directory / "ready.json", public)
        deadline = time.monotonic() + LIFETIME
        while time.monotonic() < deadline:
            done, status = os.waitpid(child, os.WNOHANG)
            if done:
                child = None
                if status != 0:
                    raise FixtureError("fixture_peer_failed")
                outcome = "closed"
                break
            time.sleep(0.1)
    except BaseException as exc:
        failure = failure_code(exc)
        raise
    finally:
        for received in previous_handlers:
            signal.signal(received, signal.SIG_IGN)
        try:
            _reap(child)
            _cleanup(resources)
            shutil.rmtree(directory)
            receipt = {"id": value["id"], "outcome": outcome, "resources_removed": True}
            if outcome != "closed":
                receipt["failure"] = failure
            if "supervisor" in resources:
                receipt["supervisor"] = resources["supervisor"]
            _write(ROOT / (value["id"] + ".receipt"), receipt)
        except BaseException as cleanup_error:
            _write(
                ROOT / (value["id"] + ".receipt"),
                {
                    "id": value["id"],
                    "outcome": "cleanup_uncertain"
                    if str(cleanup_error) == "fixture_cleanup_uncertain"
                    else "cleanup_failed",
                    "resources_removed": False,
                },
            )
            raise
        finally:
            for received, previous_handler in previous_handlers.items():
                signal.signal(received, previous_handler)


def _supervisor_child(value, directory):
    status = 1
    try:
        os.setsid()
        with open(os.devnull, "r+b", buffering=0) as null:
            for descriptor in (0, 1, 2):
                os.dup2(null.fileno(), descriptor)
        _supervise(value, directory)
        status = 0
    except BaseException:
        pass
    finally:
        os._exit(status)


def start():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise FixtureError("fixture_requires_isolated_linux_root")
    _root_directory()
    value = identity(secrets.token_hex(8))
    directory = _directory(value["id"])
    directory.mkdir(mode=0o755)
    child = os.fork()
    if child == 0:
        _supervisor_child(value, directory)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (directory / "ready.json").exists():
                state = read_owned(directory / "ready.json")
                before = observe(state)
                assert_baseline(before)
                _write(directory / "baseline.json", before)
                return state
            if (ROOT / (value["id"] + ".receipt")).exists():
                receipt = read_owned(ROOT / (value["id"] + ".receipt"))
                raise FixtureError(
                    "fixture_setup_failed:" + receipt.get("failure", "cleanup_failed")
                )
            time.sleep(0.1)
        raise FixtureError("fixture_setup_timeout")
    except BaseException:
        _reap(child)
        raise


def _fresh_socket(kind, previous):
    for _attempt in range(64):
        connection = socket.socket(socket.AF_INET, kind)
        connection.settimeout(2)
        connection.bind(("", 0))
        if connection.getsockname()[1] not in previous:
            return connection
        connection.close()
    raise FixtureError("fixture_fresh_port_exhausted")


def _tls(state, name, route, previous=()):
    directory = _directory(state["id"])
    ca = (directory / "ca.pem").read_bytes()
    if hashlib.sha256(ca).hexdigest() != state["ca_sha256"]:
        raise FixtureError("fixture_ca_changed")
    context = ssl.create_default_context(cadata=ca.decode("ascii"))
    port = PORTS[name]
    hostname = "pypi.org" if name == "address" else "fixture.invalid"
    with _fresh_socket(socket.SOCK_STREAM, previous) as raw:
        source_port = raw.getsockname()[1]
        try:
            raw.connect((PEER, port))
            with context.wrap_socket(raw, server_hostname=hostname) as connection:
                connection.sendall(
                    f"GET {route} HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n".encode()
                )
                response = http.client.HTTPResponse(connection)
                response.begin()
                value = json.loads(response.read(1025))
                if response.status != 200 or value.get("nonce") != state["nonce"]:
                    raise FixtureError("fixture_response_invalid")
                return {
                    "name": name,
                    "source_port": source_port,
                    "answered": True,
                    "nonce": value["nonce"],
                    "health": value["health"],
                }
        except ssl.SSLError as exc:
            raise FixtureError("fixture_tls_invalid") from exc
        except OSError:
            return {
                "name": name,
                "source_port": source_port,
                "answered": False,
                "nonce": None,
            }


def probe_flow(state, name, previous=()):
    value = identity(state["id"])
    if (
        any(state.get(key) != expected for key, expected in value.items())
        or re.fullmatch("[a-f0-9]{32}", state.get("nonce", "")) is None
    ):
        raise FixtureError("fixture_state_invalid")
    if name not in PORTS:
        raise FixtureError("fixture_flow_invalid")
    if name != "dns":
        return _tls(state, name, "/probe", previous)
    query = _dns_question(secrets.randbelow(65536))
    with _fresh_socket(socket.SOCK_DGRAM, previous) as connection:
        flow = {
            "name": "dns",
            "source_port": connection.getsockname()[1],
            "answered": False,
            "nonce": None,
        }
        try:
            connection.connect((PEER, PORTS["dns"]))
            connection.send(query)
            response = connection.recv(513)
            if response != _dns_answer(query):
                raise FixtureError("fixture_dns_response_invalid")
            flow.update(answered=True, nonce=state["nonce"])
        except OSError:
            pass
    return flow


def observe(state, previous=()):
    flows = [probe_flow(state, name, previous) for name in PORTS]
    health = _tls(state, "allowed", "/health", previous)
    if not health["answered"]:
        raise FixtureError("fixture_health_unavailable")
    return {"nonce": state["nonce"], "flows": flows, "health": health["health"]}


def assert_baseline(observed):
    flows = observed["flows"]
    if {flow["name"] for flow in flows} != set(PORTS) or len(flows) != len(PORTS):
        raise FixtureError("fixture_flow_set_invalid")
    for flow in flows:
        if flow["answered"] is not True or flow["nonce"] != observed["nonce"]:
            raise FixtureError("baseline_unreachable:" + flow["name"])


def assert_boundary(before, after):
    assert_baseline(before)
    if before["nonce"] != after["nonce"]:
        raise FixtureError("peer_identity_changed")
    previous = {flow["source_port"] for flow in before["flows"]}
    if {flow["name"] for flow in after["flows"]} != set(PORTS) or len(
        after["flows"]
    ) != len(PORTS):
        raise FixtureError("fixture_flow_set_invalid")
    for flow in after["flows"]:
        assert_flow_boundary(
            before["nonce"], previous, flow, before["health"], after["health"]
        )


def assert_flow_boundary(nonce, previous, flow, health_before, health_after):
    name = flow["name"]
    if flow["source_port"] in previous:
        raise FixtureError("connection_tuple_reused:" + name)
    if name == "allowed":
        if flow["answered"] is not True or flow["nonce"] != nonce:
            raise FixtureError("allowed_peer_unavailable")
    elif flow["answered"] is not False:
        raise FixtureError("excluded_endpoint_answered:" + name)
    elif health_before[name] != health_after[name]:
        raise FixtureError("excluded_peer_counter_changed:" + name)


def close(state):
    response = _tls(state, "allowed", "/close/" + state["nonce"])
    if not response["answered"]:
        raise FixtureError("fixture_close_unavailable")
    deadline = time.monotonic() + 10
    path = ROOT / (state["id"] + ".receipt")
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    receipt = read_owned(path)
    if receipt != {
        "id": state["id"],
        "outcome": "closed",
        "resources_removed": True,
        "supervisor": state["supervisor"],
    }:
        raise FixtureError("fixture_cleanup_incomplete")
    while not process_exited(state["supervisor"]):
        if time.monotonic() >= deadline:
            raise FixtureError("fixture_supervisor_still_running")
        time.sleep(0.1)
    receipt["supervisor_exited"] = True
    return receipt


def qualify():
    state = start()
    try:
        before = read_owned(_directory(state["id"]) / "baseline.json")
        after = observe(state, [flow["source_port"] for flow in before["flows"]])
        assert_baseline(after)
        try:
            assert_boundary(before, after)
        except FixtureError as exc:
            if str(exc) != "excluded_endpoint_answered:excluded":
                raise
        else:
            raise FixtureError("fixture_negative_control_did_not_fail")
    finally:
        receipt = close(state)
        os.waitpid(state["supervisor"]["pid"], 0)
        receipt["supervisor_reaped"] = True
    return {
        "fixture": state["id"],
        "baseline": "all_routes_reachable",
        "unfenced_denial_assertion": "rejected_answered_excluded_peer",
        "cleanup": receipt,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("qualify", "start", "observe", "close"))
    parser.add_argument("--fixture-id")
    arguments = parser.parse_args(argv)
    if arguments.mode == "qualify" and os.geteuid() != 0:
        return subprocess.run(
            [
                "/usr/bin/sudo",
                "--non-interactive",
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "qualify",
            ],
            env=ENV,
            check=False,
        ).returncode
    try:
        if arguments.mode == "qualify":
            result = qualify()
        elif arguments.mode == "start":
            result = start()
        else:
            state = read_owned(_directory(arguments.fixture_id) / "ready.json")
            result = close(state) if arguments.mode == "close" else observe(state)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (FixtureError, OSError, ValueError, KeyError) as exc:
        print(
            json.dumps({"error": type(exc).__name__, "code": failure_code(exc)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
