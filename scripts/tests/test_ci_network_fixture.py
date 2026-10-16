"""Exercise fixed CI peer identities, authenticated observations and organic denial failures.
The Linux lifecycle command separately qualifies real namespace setup and cleanup.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading

import pytest


def test_utility_contract_is_available():
    root = Path(__file__).parents[1]
    assert (root / "ci_network_fixture.py").is_file(), "bounded peer utility missing"
    assert (root / "ci_fence_fixture_verifier.py").is_file(), "inert verifier missing"


@pytest.fixture
def peer():
    path = Path(__file__).parents[1] / "ci_network_fixture.py"
    assert path.is_file(), "the bounded CI peer lifecycle utility is missing"
    return importlib.import_module("scripts.ci_network_fixture")


def observations(*, answered=True, offset=0):
    return {
        "nonce": "a" * 32,
        "flows": [
            {
                "name": name,
                "source_port": 40000 + index + offset,
                "answered": answered,
                "nonce": "a" * 32 if answered else None,
            }
            for index, name in enumerate(("allowed", "excluded", "address", "dns"))
        ],
        "health": {"allowed": 1, "excluded": 1, "address": 1, "dns": 1},
    }


def test_identity_is_fixed_bounded_and_not_a_command_input(peer):
    value = peer.identity("abcdef0123456789")
    assert value["namespace"] == "acp-abcdef0123456789"
    assert len(value["host_link"]) <= 15
    assert value["peer"] == "192.0.2.2"
    assert value["host"] == "192.0.2.1"


@pytest.mark.parametrize("value", ["", "../escape", "A" * 16, "a" * 15, "a" * 17, None])
def test_identity_rejects_unsafe_or_ambiguous_values(peer, value):
    with pytest.raises(peer.FixtureError, match="fixture_identity_invalid"):
        peer.identity(value)


def test_baseline_requires_every_live_peer_response(peer):
    peer.assert_baseline(observations())
    value = observations()
    value["flows"][2]["answered"] = False
    with pytest.raises(peer.FixtureError, match="baseline_unreachable:address"):
        peer.assert_baseline(value)


def test_same_network_denial_assertion_fails_organically_without_fence(peer):
    with pytest.raises(peer.FixtureError, match="excluded_endpoint_answered:excluded"):
        peer.assert_boundary(observations(), observations(offset=10))


def test_boundary_requires_new_tuples_live_allowed_peer_and_unchanged_denied_counters(
    peer,
):
    before = observations()
    after = observations(offset=10)
    for flow in after["flows"][1:]:
        flow.update(answered=False, nonce=None)
    peer.assert_boundary(before, after)
    after["flows"][3]["source_port"] = before["flows"][3]["source_port"]
    with pytest.raises(peer.FixtureError, match="connection_tuple_reused:dns"):
        peer.assert_boundary(before, after)


def test_changed_peer_identity_is_never_a_denial(peer):
    after = observations(offset=10)
    after["nonce"] = "b" * 32
    with pytest.raises(peer.FixtureError, match="peer_identity_changed"):
        peer.assert_boundary(observations(), after)


def test_setup_failure_retains_a_bounded_reason_and_removes_owned_staging(
    peer, tmp_path, monkeypatch
):
    monkeypatch.setattr(peer, "ROOT", tmp_path)
    directory = tmp_path / "abcdef0123456789"
    directory.mkdir()

    def conflict(*_arguments):
        return b'[{"dst":"192.0.2.0/30"}]'

    monkeypatch.setattr(peer, "_ip", conflict)
    with pytest.raises(peer.FixtureError, match="fixture_route_conflict"):
        peer._supervise(peer.identity(directory.name), directory)
    receipt = json.loads((tmp_path / (directory.name + ".receipt")).read_text())
    assert receipt["failure"] == "fixture_route_conflict"
    assert receipt["resources_removed"] is True
    assert not directory.exists()


def test_worker_errors_never_serialize_paths_or_arbitrary_messages(peer):
    assert peer.failure_code(OSError("/private/secret")) == "OSError"
    assert (
        peer.failure_code(peer.FixtureError("fixture_setns_failed"))
        == "fixture_setns_failed"
    )
    assert peer.failure_code(peer.FixtureError("/private/secret")) == "FixtureError"


@pytest.mark.parametrize("received", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_supervisor_signal_runs_owned_cleanup_before_exit(peer, tmp_path, received):
    directory = tmp_path / "abcdef0123456789"
    directory.mkdir()
    program = (
        "import os,signal,sys;from pathlib import Path;"
        f"sys.path.insert(0,{str(Path(peer.__file__).parents[1])!r});"
        "from scripts import ci_network_fixture as p;"
        f"p.ROOT=Path({str(tmp_path)!r});"
        f"p._ip=lambda *a:os.kill(os.getpid(),{int(received)});"
        f"p._supervise(p.identity({directory.name!r}),Path({str(directory)!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, timeout=5
    )
    assert result.returncode != 0
    assert not directory.exists(), "signal bypassed the owned cleanup block"
    receipt = json.loads((tmp_path / (directory.name + ".receipt")).read_text())
    assert receipt["resources_removed"] is True


def test_lost_native_create_completion_is_not_claimed_removed(
    peer, tmp_path, monkeypatch
):
    monkeypatch.setattr(peer, "ROOT", tmp_path)
    directory = tmp_path / "abcdef0123456789"
    directory.mkdir()
    kernel = {"present": False}

    def ip(*arguments):
        if arguments[:2] == ("netns", "add"):
            kernel["present"] = True
            raise peer.FixtureError("completion_lost")
        return b"[]"

    monkeypatch.setattr(peer, "_ip", ip)
    with pytest.raises(peer.FixtureError):
        peer._supervise(peer.identity(directory.name), directory)
    receipt = json.loads((tmp_path / (directory.name + ".receipt")).read_text())
    assert kernel["present"] is True
    assert receipt["resources_removed"] is False
    assert receipt["outcome"] == "cleanup_uncertain"


def test_ready_path_is_absent_until_the_complete_document_is_published(
    peer, tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    path = tmp_path / "ready.json"
    original = peer.json.dump

    def paused_dump(value, handle, **kwargs):
        handle.write('{"partial":')
        handle.flush()
        entered.set()
        assert release.wait(5)
        handle.seek(0)
        handle.truncate()
        original(value, handle, **kwargs)

    monkeypatch.setattr(peer.json, "dump", paused_dump)
    worker = threading.Thread(target=peer._write, args=(path, {"ready": True}))
    worker.start()
    try:
        assert entered.wait(5)
        assert not path.exists()
    finally:
        release.set()
        worker.join(5)
    assert json.loads(path.read_text()) == {"ready": True}


def test_cleanup_does_not_attest_an_executing_supervisor_or_kill_a_reused_pid(
    peer, monkeypatch
):
    monkeypatch.setattr(peer, "process_identity", lambda _pid: (123, "S"))
    assert peer.process_exited({"pid": 42, "start": 123}) is False
    monkeypatch.setattr(peer, "process_identity", lambda _pid: (123, "Z"))
    assert peer.process_exited({"pid": 42, "start": 123}) is True
    monkeypatch.setattr(peer, "process_identity", lambda _pid: (124, "S"))
    assert peer.process_exited({"pid": 42, "start": 123}) is True


@pytest.mark.parametrize(
    "destination", ["192.0.2.0/24", "192.0.2.0/30", "192.0.2.2/32", "128.0.0.0/1"]
)
def test_nondefault_route_overlap_refuses_fixture_without_mutation(peer, destination):
    with pytest.raises(peer.FixtureError, match="fixture_route_conflict"):
        peer.assert_network_available([{"dst": destination}], [])


def test_defaults_and_disjoint_routes_are_allowed_but_owned_addresses_are_not(peer):
    peer.assert_network_available([{"dst": "default"}, {"dst": "10.0.0.0/8"}], [])
    with pytest.raises(peer.FixtureError, match="fixture_address_conflict"):
        peer.assert_network_available(
            [], [{"addr_info": [{"family": "inet", "local": "192.0.2.1"}]}]
        )


def test_owned_files_reject_wrong_owner_writable_symlink_and_oversize(
    peer, tmp_path, monkeypatch
):
    import os
    import stat
    from types import SimpleNamespace

    for mode, uid in (
        (stat.S_IFREG | 0o666, 0),
        (stat.S_IFREG | 0o644, 501),
        (stat.S_IFDIR | 0o755, 0),
    ):
        with pytest.raises(peer.FixtureError, match="untrusted_fixture_file"):
            peer.owned_metadata(SimpleNamespace(st_mode=mode, st_uid=uid))
    path = tmp_path / "state.json"
    peer._write(path, {"valid": True})
    metadata = peer.owned_metadata
    monkeypatch.setattr(
        peer,
        "owned_metadata",
        lambda value, **kwargs: metadata(
            SimpleNamespace(st_mode=value.st_mode, st_uid=0), **kwargs
        ),
    )
    assert peer.read_owned(path) == {"valid": True}
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        peer.read_owned(link)
    path.write_bytes(b"x" * (peer.MAX_DOCUMENT + 1))
    with pytest.raises(peer.FixtureError, match="fixture_document_too_large"):
        peer.read_owned(path)
    with pytest.raises(FileExistsError):
        peer._write(path, {"replace": False})
    assert not list(tmp_path.glob(".*"))
    assert os.stat(path).st_size == peer.MAX_DOCUMENT + 1


def test_os_command_fixed_environment_and_failure(peer, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=len(calls) - 1, stdout=b"[]")

    monkeypatch.setattr(peer.subprocess, "run", run)
    assert peer._ip("-j", "link") == b"[]"
    assert calls[0][0] == ("/usr/sbin/ip", "-j", "link")
    assert calls[0][1]["env"] == peer.ENV
    with pytest.raises(peer.FixtureError, match="fixture_os_command_failed:ip"):
        peer._ip("link")


def test_dns_answers_only_the_fixed_bounded_question(peer):
    query = peer._dns_question(27)
    assert peer._dns_answer(query)[:2] == b"\x00\x1b"
    assert peer._dns_answer(query + b"x") is None
    assert peer._dns_answer(query[:13] + b"X" + query[14:]) is None


@pytest.fixture
def tls_peer(peer, tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setattr(peer, "ROOT", tmp_path)
    monkeypatch.setattr(peer, "PEER", "127.0.0.1")
    monkeypatch.setattr(peer, "PORTS", dict.fromkeys(peer.PORTS, 0))
    state = peer.identity("a" * 16)
    directory = tmp_path / state["id"]
    directory.mkdir()
    subprocess.run(
        [
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
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    state.update(
        nonce="a" * 32,
        ca_sha256=hashlib.sha256((directory / "ca.pem").read_bytes()).hexdigest(),
    )
    selection, context = peer._listeners(directory)
    for key in selection.get_map().values():
        peer.PORTS[key.data] = key.fileobj.getsockname()[1]
    worker = threading.Thread(
        target=peer._serve, args=(selection, context, state["nonce"])
    )
    worker.start()
    try:
        yield state
    finally:
        if worker.is_alive():
            peer._tls(state, "allowed", "/close/" + state["nonce"])
        worker.join(5)
        assert not worker.is_alive()


def test_real_private_tls_peer_observations_dns_and_organic_denial(peer, tls_peer):
    before = peer.observe(tls_peer)
    peer.assert_baseline(before)
    after = peer.observe(tls_peer, [flow["source_port"] for flow in before["flows"]])
    with pytest.raises(peer.FixtureError, match="excluded_endpoint_answered:excluded"):
        peer.assert_boundary(before, after)
    assert after["health"] == dict.fromkeys(peer.PORTS, 2)


def test_peer_rejects_ca_changes_invalid_state_and_unknown_flow(peer, tls_peer):
    with pytest.raises(peer.FixtureError, match="fixture_ca_changed"):
        peer._tls({**tls_peer, "ca_sha256": "0" * 64}, "allowed", "/health")
    with pytest.raises(peer.FixtureError, match="fixture_state_invalid"):
        peer.probe_flow({**tls_peer, "host": "untrusted"}, "dns")
    with pytest.raises(peer.FixtureError, match="fixture_flow_invalid"):
        peer.probe_flow(tls_peer, "arbitrary")


def test_failed_flow_requires_live_health_and_never_counts_as_tls_denial(
    peer, monkeypatch
):

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def getsockname(self):
            return ("", 40001)

        def connect(self, _address):
            raise OSError("unreachable")

    monkeypatch.setattr(peer, "_fresh_socket", lambda *_args: Connection())
    state = {**peer.identity("a" * 16), "nonce": "a" * 32}
    assert peer.probe_flow(state, "dns")["answered"] is False
    monkeypatch.setattr(peer, "probe_flow", lambda *_args: {"answered": False})
    monkeypatch.setattr(peer, "_tls", lambda *_args: {"answered": False})
    with pytest.raises(peer.FixtureError, match="fixture_health_unavailable"):
        peer.observe(state)


def test_fresh_source_ports_refuse_exhaustion(peer, monkeypatch):
    from types import SimpleNamespace

    closed = []

    def create(*_args):
        return SimpleNamespace(
            settimeout=lambda _v: None,
            bind=lambda _v: None,
            getsockname=lambda: ("", 123),
            close=lambda: closed.append(True),
        )

    monkeypatch.setattr(peer.socket, "socket", create)
    with pytest.raises(peer.FixtureError, match="fixture_fresh_port_exhausted"):
        peer._fresh_socket(peer.socket.SOCK_STREAM, [123])
    assert len(closed) == 64


def test_boundary_rejects_missing_flows_bad_allowed_and_counter_changes(peer):
    before = observations()
    for value in (before, observations()):
        value["flows"] = value["flows"][:-1]
        with pytest.raises(peer.FixtureError, match="fixture_flow_set_invalid"):
            peer.assert_baseline(value)
    before = observations()
    after = observations(offset=10)
    after["flows"][0]["answered"] = False
    with pytest.raises(peer.FixtureError, match="allowed_peer_unavailable"):
        peer.assert_boundary(before, after)
    after = observations(offset=10)
    after["flows"] = after["flows"][:-1]
    with pytest.raises(peer.FixtureError, match="fixture_flow_set_invalid"):
        peer.assert_boundary(before, after)
    after = observations(offset=10)
    for flow in after["flows"][1:]:
        flow.update(answered=False, nonce=None)
    after["health"]["dns"] += 1
    with pytest.raises(peer.FixtureError, match="excluded_peer_counter_changed:dns"):
        peer.assert_boundary(before, after)


def test_process_identity_and_direct_child_reaping(peer, monkeypatch):
    calls = []
    peer._reap(None)
    waits = iter([(0, 0), (12, 0)])
    monkeypatch.setattr(
        peer.os, "waitpid", lambda *args: (calls.append(("wait", args)), next(waits))[1]
    )
    monkeypatch.setattr(peer.os, "kill", lambda *args: calls.append(("kill", args)))
    peer._reap(12)
    assert calls == [
        ("wait", (12, peer.os.WNOHANG)),
        ("kill", (12, signal.SIGTERM)),
        ("wait", (12, 0)),
    ]
    with pytest.raises(peer.FixtureError, match="fixture_process_identity_invalid"):
        peer.process_identity(True)


def test_qualify_reaps_its_direct_supervisor_and_requires_organic_failure(
    peer, monkeypatch
):
    state = {"id": "a" * 16, "supervisor": {"pid": 42, "start": 2}}
    before, after = observations(), observations(offset=10)
    monkeypatch.setattr(peer, "start", lambda: state)
    monkeypatch.setattr(peer, "read_owned", lambda _path: before)
    monkeypatch.setattr(peer, "observe", lambda *_args: after)
    monkeypatch.setattr(peer, "close", lambda _state: {"resources_removed": True})
    calls = []
    monkeypatch.setattr(peer.os, "waitpid", lambda *args: calls.append(args))
    result = peer.qualify()
    assert result["cleanup"]["supervisor_reaped"] is True
    assert calls == [(42, 0)]
    monkeypatch.setattr(peer, "assert_boundary", lambda *_args: None)
    with pytest.raises(
        peer.FixtureError, match="fixture_negative_control_did_not_fail"
    ):
        peer.qualify()


def test_start_refuses_wrong_platform_and_failed_baseline_reaps_owned_child(
    peer, tmp_path, monkeypatch
):
    monkeypatch.setattr(peer.sys, "platform", "darwin")
    with pytest.raises(peer.FixtureError, match="fixture_requires_isolated_linux_root"):
        peer.start()
    monkeypatch.setattr(peer.sys, "platform", "linux")
    monkeypatch.setattr(peer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(peer, "ROOT", tmp_path)
    monkeypatch.setattr(peer, "_root_directory", lambda: None)
    monkeypatch.setattr(peer.secrets, "token_hex", lambda _size: "a" * 16)
    monkeypatch.setattr(peer.os, "fork", lambda: 42)
    original_mkdir = Path.mkdir

    def mkdir(path, **kwargs):
        original_mkdir(path, **kwargs)
        (path / "ready.json").write_text("{}")

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(peer, "read_owned", lambda _path: {})
    monkeypatch.setattr(
        peer,
        "observe",
        lambda _state: (_ for _ in ()).throw(peer.FixtureError("baseline_failed")),
    )
    reaped = []
    monkeypatch.setattr(peer, "_reap", reaped.append)
    with pytest.raises(peer.FixtureError, match="baseline_failed"):
        peer.start()
    assert reaped == [42]


def test_cli_fixed_modes_and_private_error_codes(peer, monkeypatch, capsys):
    monkeypatch.setattr(peer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(peer, "qualify", lambda: {"qualified": True})
    assert peer.main(["qualify"]) == 0
    assert json.loads(capsys.readouterr().out) == {"qualified": True}
    monkeypatch.setattr(
        peer, "start", lambda: (_ for _ in ()).throw(OSError("/private/key"))
    )
    assert peer.main(["start"]) == 1
    assert "/private" not in capsys.readouterr().err
    monkeypatch.setattr(peer, "read_owned", lambda _path: {"id": "a" * 16})
    monkeypatch.setattr(peer, "observe", lambda _state: {"observed": True})
    monkeypatch.setattr(peer, "close", lambda _state: {"closed": True})
    assert peer.main(["observe", "--fixture-id", "a" * 16]) == 0
    assert peer.main(["close", "--fixture-id", "a" * 16]) == 0


@pytest.fixture
def controlled_kernel(peer, tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(peer, "ROOT", tmp_path / "root")
    peer.ROOT.mkdir()
    original_path, original_pipe = Path, peer.os.pipe
    kernel = tmp_path / "kernel"
    (kernel / "run/netns").mkdir(parents=True)
    (kernel / "sys/class/net").mkdir(parents=True)

    def path(*arguments):
        value = original_path(*arguments)
        if str(value).startswith(("/run/netns", "/sys/class/net")):
            return kernel / str(value).lstrip("/")
        return value

    monkeypatch.setattr(peer, "Path", path)
    calls = []
    value = peer.identity("a" * 16)
    directory = peer.ROOT / value["id"]
    directory.mkdir()

    def ip(*args):
        calls.append(args)
        if args[:2] == ("netns", "add"):
            (path("/run/netns") / value["namespace"]).touch()
        elif args[:2] == ("link", "add"):
            for name in ("host_link", "peer_link"):
                target = path("/sys/class/net") / value[name]
                target.mkdir()
                (target / "ifindex").write_text("91")
        elif args[:2] == ("link", "del"):
            for name in ("host_link", "peer_link"):
                shutil.rmtree(path("/sys/class/net") / value[name])
        elif args[:2] == ("netns", "del"):
            (path("/run/netns") / value["namespace"]).unlink()
        return b"[]"

    def run(*_args):
        (directory / "key.pem").write_bytes(b"private test key")
        (directory / "ca.pem").write_bytes(b"public test CA")
        return b""

    def pipe():
        read, write = original_pipe()
        peer.os.write(write, b"1")
        return read, write

    monkeypatch.setattr(peer, "_ip", ip)
    monkeypatch.setattr(peer, "_run", run)
    monkeypatch.setattr(peer.os, "fork", lambda: 1234)
    monkeypatch.setattr(peer.os, "pipe", pipe)
    monkeypatch.setattr(peer.os, "waitpid", lambda *_args: (1234, 0))
    monkeypatch.setattr(peer, "process_identity", lambda _pid: (77, "S"))
    return value, directory, calls, path


def test_supervisor_tracks_kernel_identities_and_cleans_only_created_resources(
    peer, controlled_kernel
):
    value, directory, calls, path = controlled_kernel
    peer._supervise(value, directory)
    receipt = json.loads((peer.ROOT / (value["id"] + ".receipt")).read_text())
    assert receipt["outcome"] == "closed"
    assert receipt["resources_removed"] is True
    assert not directory.exists()
    assert not (path("/run/netns") / value["namespace"]).exists()
    assert not (path("/sys/class/net") / value["host_link"]).exists()
    assert calls[-2:] == [
        ("link", "del", value["host_link"]),
        ("netns", "del", value["namespace"]),
    ]


def test_partial_setup_failure_still_removes_recorded_namespace_and_link(
    peer, controlled_kernel, monkeypatch
):
    value, directory, calls, path = controlled_kernel
    original = peer._ip

    def fail(*args):
        result = original(*args)
        if args[:2] == ("link", "set"):
            raise peer.FixtureError("partial_setup_failure")
        return result

    monkeypatch.setattr(peer, "_ip", fail)
    with pytest.raises(peer.FixtureError, match="partial_setup_failure"):
        peer._supervise(value, directory)
    receipt = json.loads((peer.ROOT / (value["id"] + ".receipt")).read_text())
    assert receipt["resources_removed"] is True
    assert receipt["failure"] == "partial_setup_failure"
    assert not (path("/sys/class/net") / value["host_link"]).exists()


@pytest.mark.parametrize(
    "name,code",
    [
        ("namespace", "fixture_namespace_conflict"),
        ("host_link", "fixture_link_conflict"),
    ],
)
def test_existing_kernel_resources_are_not_adopted_or_deleted(
    peer, controlled_kernel, name, code
):
    value, directory, calls, path = controlled_kernel
    existing = (
        path("/run/netns") / value[name]
        if name == "namespace"
        else path("/sys/class/net") / value[name]
    )
    existing.touch()
    with pytest.raises(peer.FixtureError, match=code):
        peer._supervise(value, directory)
    assert existing.exists()
    assert ("link", "del", value["host_link"]) not in calls
    assert (("netns", "del", value["namespace"]) in calls) is (name != "namespace")


@pytest.mark.parametrize(
    "resource,code",
    [
        ("ifindex", "fixture_link_identity_changed"),
        ("namespace_inode", "fixture_namespace_identity_changed"),
    ],
)
def test_changed_kernel_identity_cannot_be_removed(
    peer, controlled_kernel, resource, code
):
    value, _directory, calls, path = controlled_kernel
    target = (
        path("/sys/class/net") / value["host_link"]
        if resource == "ifindex"
        else path("/run/netns") / value["namespace"]
    )
    if resource == "ifindex":
        target.mkdir()
        (target / "ifindex").write_text("91")
    else:
        target.touch()
    with pytest.raises(peer.FixtureError, match=code):
        peer._cleanup({"identity": value, resource: -1})
    assert target.exists()
    assert not calls


def test_peer_enters_namespace_then_drops_authority_before_signalling_ready(
    peer, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    monkeypatch.setattr(peer.signal, "signal", lambda *_args: None)
    namespace = tmp_path / "namespace"
    namespace.touch()
    monkeypatch.setattr(peer.os, "open", lambda *_args: 91)
    closed, actions = [], []
    monkeypatch.setattr(peer.os, "close", closed.append)
    library = SimpleNamespace(
        setns=lambda *args: (actions.append(("setns", args)), 0)[1],
        prctl=lambda *args: (actions.append(("prctl", args)), 0)[1],
    )
    monkeypatch.setattr(peer.ctypes, "CDLL", lambda *_a, **_kw: library)
    monkeypatch.setattr(peer, "_listeners", lambda _directory: ("selection", "TLS"))
    monkeypatch.setattr(
        peer.os, "setgroups", lambda value: actions.append(("groups", value))
    )
    monkeypatch.setattr(peer.os, "setgid", lambda value: actions.append(("gid", value)))
    monkeypatch.setattr(peer.os, "setuid", lambda value: actions.append(("uid", value)))
    monkeypatch.setattr(
        peer,
        "Path",
        lambda _path: SimpleNamespace(
            read_text=lambda: "CapEff:\t00000000\nCapPrm:\t00000000\n"
        ),
    )
    monkeypatch.setattr(peer.os, "write", lambda *args: actions.append(("ready", args)))
    monkeypatch.setattr(peer, "_serve", lambda *args: actions.append(("serve", args)))
    peer._peer("fixed", tmp_path, "a" * 32, 92)
    assert [name for name, _args in actions] == [
        "setns",
        "prctl",
        "groups",
        "gid",
        "uid",
        "ready",
        "serve",
    ]
    assert closed == [91, 92]
    monkeypatch.setattr(
        peer,
        "Path",
        lambda _path: SimpleNamespace(
            read_text=lambda: "CapEff:\t00000001\nCapPrm:\t00000000\n"
        ),
    )
    with pytest.raises(peer.FixtureError, match="fixture_capabilities_retained"):
        peer._peer("fixed", tmp_path, "a" * 32, 92)
    monkeypatch.setattr(
        peer,
        "Path",
        lambda _path: SimpleNamespace(read_text=lambda: "CapEff:\t00000000\n"),
    )
    with pytest.raises(peer.FixtureError, match="fixture_capabilities_retained"):
        peer._peer("fixed", tmp_path, "a" * 32, 92)
    library.setns = lambda *_args: -1
    with pytest.raises(peer.FixtureError, match="fixture_setns_failed"):
        peer._peer("fixed", tmp_path, "a" * 32, 92)
    library.setns = lambda *_args: 0
    library.prctl = lambda *_args: -1
    with pytest.raises(peer.FixtureError, match="fixture_no_new_privileges_failed"):
        peer._peer("fixed", tmp_path, "a" * 32, 92)


def test_peer_child_exits_even_when_the_error_handshake_is_already_closed(
    peer, tmp_path, monkeypatch
):
    class Exited(BaseException):
        def __init__(self, status):
            self.status = status

    def fail(*_args):
        raise RuntimeError("peer_failed_after_close")

    monkeypatch.setattr(peer, "_peer", fail)
    monkeypatch.setattr(peer.os, "close", lambda _descriptor: None)

    def broken(*_args):
        raise BrokenPipeError("closed")

    monkeypatch.setattr(peer.os, "write", broken)
    monkeypatch.setattr(
        peer.os, "_exit", lambda status: (_ for _ in ()).throw(Exited(status))
    )
    with pytest.raises(Exited) as error:
        peer._peer_child(peer.identity("a" * 16), tmp_path, "a" * 32, 91, 92)
    assert error.value.status == 1
    monkeypatch.setattr(peer, "_peer", lambda *_args: None)
    with pytest.raises(Exited) as error:
        peer._peer_child(peer.identity("a" * 16), tmp_path, "a" * 32, 91, 92)
    assert error.value.status == 0
    with pytest.raises(peer.FixtureError, match="fixture_child_identity_invalid"):
        peer._reap(0)


def test_root_directory_requires_safe_metadata_and_creation_is_repeatable(
    peer, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    monkeypatch.setattr(peer, "ROOT", tmp_path / "root")
    original = peer.owned_metadata
    monkeypatch.setattr(
        peer,
        "owned_metadata",
        lambda value, **kwargs: original(
            SimpleNamespace(st_mode=value.st_mode, st_uid=0), **kwargs
        ),
    )
    peer._root_directory()
    peer._root_directory()
    assert peer.ROOT.is_dir()


@pytest.mark.parametrize("postfork", ["success", "receipt", "timeout"])
def test_start_parent_requires_baseline_or_reaps_failed_setup(
    peer, tmp_path, monkeypatch, postfork
):
    monkeypatch.setattr(peer.sys, "platform", "linux")
    monkeypatch.setattr(peer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(peer, "ROOT", tmp_path)
    monkeypatch.setattr(peer, "_root_directory", lambda: None)
    monkeypatch.setattr(peer.secrets, "token_hex", lambda _size: "a" * 16)
    monkeypatch.setattr(peer.os, "fork", lambda: 42)
    original_mkdir = Path.mkdir

    def mkdir(path, **kwargs):
        original_mkdir(path, **kwargs)
        if postfork == "success":
            (path / "ready.json").write_text("{}")
        if postfork == "receipt":
            (tmp_path / ("a" * 16 + ".receipt")).write_text("{}")

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(
        peer, "read_owned", lambda _path: {"failure": "fixed_setup_failure"}
    )
    monkeypatch.setattr(peer, "observe", lambda _state: observations())
    reaped = []
    monkeypatch.setattr(peer, "_reap", reaped.append)
    if postfork == "success":
        assert peer.start() == {"failure": "fixed_setup_failure"}
        assert (
            json.loads((tmp_path / ("a" * 16) / "baseline.json").read_text())
            == observations()
        )
        assert reaped == []
    else:
        if postfork == "timeout":
            clock = iter([0, 21])
            monkeypatch.setattr(peer.time, "monotonic", lambda: next(clock))
        with pytest.raises(
            peer.FixtureError,
            match="fixture_setup_failed:fixed_setup_failure"
            if postfork == "receipt"
            else "fixture_setup_timeout",
        ):
            peer.start()
        assert reaped == [42]


def test_close_requires_authenticated_ack_owned_receipt_and_terminated_supervisor(
    peer, tmp_path, monkeypatch
):
    state = {"id": "a" * 16, "nonce": "b" * 32, "supervisor": {"pid": 42, "start": 100}}
    receipt = {
        "id": state["id"],
        "outcome": "closed",
        "resources_removed": True,
        "supervisor": state["supervisor"],
    }
    monkeypatch.setattr(peer, "ROOT", tmp_path)
    (tmp_path / (state["id"] + ".receipt")).write_text("{}")
    monkeypatch.setattr(peer, "_tls", lambda *_args: {"answered": False})
    with pytest.raises(peer.FixtureError, match="fixture_close_unavailable"):
        peer.close(state)
    monkeypatch.setattr(peer, "_tls", lambda *_args: {"answered": True})
    monkeypatch.setattr(peer, "read_owned", lambda _path: {})
    with pytest.raises(peer.FixtureError, match="fixture_cleanup_incomplete"):
        peer.close(state)
    monkeypatch.setattr(peer, "read_owned", lambda _path: receipt)
    monkeypatch.setattr(
        peer,
        "process_identity",
        lambda _pid: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert peer.close(state)["supervisor_exited"] is True


def test_listener_partial_bind_failure_closes_every_owned_socket(
    peer, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    connections = []

    class Connection:
        def __init__(self):
            self.closed = False

        def bind(self, _address):
            if len(connections) == 2:
                raise OSError("injected_bind_failure")

        def listen(self, _backlog):
            pass

        def close(self):
            self.closed = True

        def fileno(self):
            return 91

    def socket(*_args):
        value = Connection()
        connections.append(value)
        return value

    class Selection:
        def __init__(self):
            self.values = []

        def register(self, connection, _events, _name):
            self.values.append(connection)

        def get_map(self):
            return {
                i: SimpleNamespace(fileobj=value) for i, value in enumerate(self.values)
            }

        def close(self):
            pass

    monkeypatch.setattr(
        peer.ssl,
        "SSLContext",
        lambda _protocol: SimpleNamespace(load_cert_chain=lambda *_args: None),
    )
    monkeypatch.setattr(peer.socket, "socket", socket)
    monkeypatch.setattr(peer.selectors, "DefaultSelector", Selection)
    with pytest.raises(OSError, match="injected_bind_failure"):
        peer._listeners(tmp_path)
    assert all(value.closed for value in connections)


def test_sudo_entry_executes_only_fixed_qualify_with_sanitized_environment(
    peer, monkeypatch
):
    from types import SimpleNamespace

    monkeypatch.setattr(peer.os, "geteuid", lambda: 501)
    calls = []
    monkeypatch.setattr(
        peer.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append((command, kwargs)),
            SimpleNamespace(returncode=7),
        )[1],
    )
    assert peer.main(["qualify"]) == 7
    assert calls[0][0][:4] == [
        "/usr/bin/sudo",
        "--non-interactive",
        sys.executable,
        "-I",
    ]
    assert calls[0][0][-1] == "qualify"
    assert calls[0][1]["env"] == peer.ENV


@pytest.mark.parametrize("stage", ["setsid", "dup2", "supervise", "success"])
def test_supervisor_child_never_returns_to_the_bootstrap_caller_after_initialization_failure(
    peer, tmp_path, monkeypatch, stage
):
    import io

    class Exited(BaseException):
        def __init__(self, status):
            self.status = status

    def failed(*_args):
        raise OSError("child_initialization_failed")

    class Null(io.BytesIO):
        def fileno(self):
            return 91

    with monkeypatch.context() as patch:
        patch.setattr(peer.os, "setsid", failed if stage == "setsid" else lambda: None)
        patch.setattr("builtins.open", lambda *_a, **_kw: Null())
        patch.setattr(
            peer.os, "dup2", failed if stage == "dup2" else lambda *_args: None
        )
        patch.setattr(
            peer.os, "_exit", lambda status: (_ for _ in ()).throw(Exited(status))
        )
        patch.setattr(
            peer, "_supervise", failed if stage == "supervise" else lambda *_args: None
        )
        with pytest.raises(Exited) as error:
            peer._supervisor_child(peer.identity("a" * 16), tmp_path)
    assert error.value.status == (0 if stage == "success" else 1)
