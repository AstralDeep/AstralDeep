"""Check strict read-only Fence evidence and DNS refusal contracts against bounded fixtures.
Synthetic documents test parser rejection and never claim live Fence acceptance.
"""

from __future__ import annotations

import importlib
import copy
import json
from types import SimpleNamespace
from pathlib import Path
import struct

import pytest


@pytest.fixture
def verifier():
    path = Path(__file__).parents[1] / "ci_fence_fixture_verifier.py"
    assert path.is_file(), "the inert Fence evidence verifier is missing"
    return importlib.import_module("scripts.ci_fence_fixture_verifier")


def evidence():
    return {
        "mode": "block",
        "status": "protected_host_block",
        "readiness_status": "ready",
        "protection_available": True,
        "network_verification_status": "verified",
        "sudo_status": "disabled_verified",
        "container_status": "disabled_verified",
        "critical_findings": [],
        "resident_health": {"status": "healthy", "resident_pid": 42},
        "findings": [
            {
                "classification": "rejected",
                "remote_address": "192.0.2.2",
                "protocol": protocol,
                "remote_port": port,
            }
            for protocol, port in (("tcp", 19443), ("tcp", 443), ("udp", 53))
        ],
        "counters": {"total_violations": 3, "sampled_violations": 3},
    }


def test_actual_report_requires_protected_ready_consistent_resident(verifier):
    report = evidence()
    ready = {"status": "ready", "resident_health": report["resident_health"]}
    verifier.assert_report(report, ready)
    report["mode"] = "audit"
    with pytest.raises(verifier.VerificationError, match="fence_not_protected"):
        verifier.assert_report(report, ready)


def test_every_excluded_flow_requires_actual_matching_rejection(verifier):
    report = evidence()
    verifier.assert_rejections(report)
    report["findings"].pop()
    with pytest.raises(
        verifier.VerificationError, match="fence_rejection_missing:udp:53"
    ):
        verifier.assert_rejections(report)


def test_dns_refusal_requires_the_same_question_and_transaction(verifier):
    question = b"\x07outside\x07invalid\0\0\1\0\1"
    query = struct.pack("!HHHHHH", 7, 0x0100, 1, 0, 0, 0) + question
    response = struct.pack("!HHHHHH", 7, 0x8185, 1, 0, 0, 0) + question
    verifier.assert_dns_refused(query, response)
    with pytest.raises(verifier.VerificationError, match="dns_refusal_invalid"):
        verifier.assert_dns_refused(query, response[:-1])


def test_old_matching_rows_plus_unrelated_counter_never_qualify_a_new_probe(verifier):
    before = evidence()
    after = evidence()
    after["counters"]["total_violations"] += 3
    with pytest.raises(verifier.VerificationError, match="fence_rejection_preexisting"):
        verifier.assert_new_rejection(before, after, "tcp", 443)


def test_a_new_matching_row_also_requires_counter_advancement(verifier):
    before = evidence()
    before["findings"] = []
    after = evidence()
    with pytest.raises(
        verifier.VerificationError, match="fence_rejection_counter_missing"
    ):
        verifier.assert_new_rejection(before, after, "udp", 53)
    before["counters"]["total_violations"] = 0
    verifier.assert_new_rejection(before, after, "udp", 53)


def observations(offset=0, denied=False):
    return {
        "nonce": "a" * 32,
        "flows": [
            {
                "name": name,
                "source_port": 40000 + index + offset,
                "answered": not denied or name == "allowed",
                "nonce": "a" * 32 if not denied or name == "allowed" else None,
            }
            for index, name in enumerate(("allowed", "excluded", "address", "dns"))
        ],
        "health": {"allowed": 1, "excluded": 1, "address": 1, "dns": 1},
    }


def verifier_fixture(
    monkeypatch, *, stale=False, die_on_probe=False, baseline_answered=True
):
    verifier = importlib.import_module("scripts.ci_fence_fixture_verifier")
    peer = importlib.import_module("scripts.ci_network_fixture")
    health = {"status": "healthy", "resident_pid": 42}
    report = {
        "mode": "block",
        "status": "protected_host_block",
        "readiness_status": "ready",
        "protection_available": True,
        "network_verification_status": "verified",
        "sudo_status": "disabled_verified",
        "container_status": "disabled_verified",
        "critical_findings": [],
        "resident_health": health,
        "findings": [],
        "counters": {"total_violations": 0, "sampled_violations": 0},
    }
    ready = {"status": "ready", "resident_health": health}
    ports = {"excluded": ("tcp", 19443), "address": ("tcp", 443), "dns": ("udp", 53)}

    def row(protocol, port):
        return {
            "timestamp": "unix-ms:1700000000000",
            "mode": "block",
            "classification": "rejected",
            "remote_address": peer.PEER,
            "protocol": protocol,
            "remote_port": port,
        }

    if stale:
        report["findings"] = [row(protocol, port) for protocol, port in ports.values()]
        report["counters"].update(total_violations=6, sampled_violations=3)
    before = observations()
    if not baseline_answered:
        before["flows"][1]["answered"] = False
        before["flows"][1]["nonce"] = None
    active = [True]
    probes = []
    state = {"id": "a" * 16, "nonce": "a" * 32}
    monkeypatch.setattr(
        peer,
        "read_owned",
        lambda path: before if path.name == "baseline.json" else state,
    )

    def probe(_state, name, _previous=()):
        probes.append(name)
        if name in ports:
            if die_on_probe:
                active[0] = False
            protocol, port = ports[name]
            report["findings"].append(row(protocol, port))
            report["counters"]["total_violations"] += 1
            report["counters"]["sampled_violations"] += 1
        return next(
            flow for flow in observations(20, True)["flows"] if flow["name"] == name
        )

    monkeypatch.setattr(peer, "probe_flow", probe)
    monkeypatch.setattr(
        peer,
        "_tls",
        lambda *_a, **_k: {
            "answered": True,
            "nonce": "a" * 32,
            "health": before["health"],
        },
    )
    monkeypatch.setattr(verifier, "_peer_module", lambda: peer)
    monkeypatch.setattr(
        verifier, "_documents", lambda *_a: (copy.deepcopy(report), ready, {})
    )

    def validate(_n, _r, value, readiness, _d, unit):
        verifier.assert_report(value, readiness)
        if "ActiveState=active" not in unit:
            raise verifier.VerificationError("resident_exited")

    def unit_status(*_args, **_kwargs):
        return SimpleNamespace(
            stdout=(
                b"ActiveState=active\nMainPID=42\nSubState=running\n"
                if active[0]
                else b"ActiveState=inactive\nMainPID=0\nSubState=dead\n"
            )
        )

    class Resolver:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, _value):
            pass

        def connect(self, _address):
            pass

        def send(self, query):
            self.query = query

        def recv(self, _limit):
            return (
                self.query[:2]
                + struct.pack("!HHHHH", 0x8185, 1, 0, 0, 0)
                + self.query[12:]
            )

    monkeypatch.setattr(verifier, "validate_upstream", validate)
    monkeypatch.setattr(verifier, "assert_bundle_rejected", lambda *_a: None)
    monkeypatch.setattr(verifier.subprocess, "run", unit_status)
    monkeypatch.setattr(verifier.socket, "socket", lambda *_a: Resolver())
    return verifier, probes


def test_old_matching_findings_are_rejected_before_new_probes(monkeypatch):
    verifier, probes = verifier_fixture(monkeypatch, stale=True)
    with pytest.raises(verifier.VerificationError, match="fence_rejection_preexisting"):
        verifier.verify("a" * 16, "fence-synthetic", Path("/actual-node24"))
    assert probes == []


def test_actual_current_unit_is_checked_after_probe(monkeypatch):
    verifier, _probes = verifier_fixture(monkeypatch, die_on_probe=True)
    with pytest.raises(verifier.VerificationError, match="resident_exited"):
        verifier.verify("a" * 16, "fence-synthetic", Path("/actual-node24"))


def test_new_bracketed_findings_can_pass_the_adapter_positive_control(monkeypatch):
    verifier, probes = verifier_fixture(monkeypatch)
    result = verifier.verify("a" * 16, "fence-synthetic", Path("/actual-node24"))
    assert result["network"] == "controlled_denials_verified"
    assert probes == ["allowed", "excluded", "address", "dns"]


def test_verified_memory_peer_is_used_without_reopening_source(monkeypatch):
    verifier, probes = verifier_fixture(monkeypatch)
    loaded = verifier._peer_module()
    monkeypatch.setattr(
        verifier, "_peer_module", lambda: pytest.fail("reopened peer source")
    )
    result = verifier.verify(
        "a" * 16, "fence-synthetic", Path("/actual-node24"), peer=loaded
    )
    assert result["network"] == "controlled_denials_verified"
    assert probes == ["allowed", "excluded", "address", "dns"]


def test_absent_successful_baseline_cannot_qualify_later_denials(monkeypatch):
    verifier, _probes = verifier_fixture(monkeypatch, baseline_answered=False)
    with pytest.raises(RuntimeError, match="baseline_unreachable:excluded"):
        verifier.verify("a" * 16, "fence-synthetic", Path("/actual-node24"))


def test_current_unit_is_checked_again_after_checksum_probe(monkeypatch):
    verifier, _probes = verifier_fixture(monkeypatch)
    inactive = [False]

    def validation(_n, _r, value, readiness, _d, unit):
        verifier.assert_report(value, readiness)
        if inactive[0]:
            raise verifier.VerificationError("resident_exited_after_integrity")

    monkeypatch.setattr(verifier, "validate_upstream", validation)
    monkeypatch.setattr(
        verifier, "assert_bundle_rejected", lambda *_a: inactive.__setitem__(0, True)
    )
    with pytest.raises(
        verifier.VerificationError, match="resident_exited_after_integrity"
    ):
        verifier.verify("a" * 16, "fence-synthetic", Path("/actual-node24"))


def test_cleanup_receipt_does_not_accept_a_still_executing_original_supervisor(
    tmp_path, monkeypatch
):
    peer = importlib.import_module("scripts.ci_network_fixture")
    state = {"id": "a" * 16, "nonce": "b" * 32, "supervisor": {"pid": 42, "start": 100}}
    receipt = {
        "id": state["id"],
        "outcome": "closed",
        "resources_removed": True,
        "supervisor": state["supervisor"],
    }
    monkeypatch.setattr(peer, "ROOT", tmp_path)
    (tmp_path / (state["id"] + ".receipt")).write_text(json.dumps(receipt))
    monkeypatch.setattr(peer, "_tls", lambda *_a: {"answered": True})
    monkeypatch.setattr(peer, "read_owned", lambda _path: receipt)
    monkeypatch.setattr(peer, "process_identity", lambda _pid: (100, "S"))
    clock = iter([0, 11])
    monkeypatch.setattr(peer.time, "monotonic", lambda: next(clock))
    with pytest.raises(peer.FixtureError, match="fixture_supervisor_still_running"):
        peer.close(state)


def test_terminal_pid_reuse_is_safe_and_never_signalled(monkeypatch):
    peer = importlib.import_module("scripts.ci_network_fixture")
    monkeypatch.setattr(peer, "process_identity", lambda _pid: (101, "S"))
    monkeypatch.setattr(
        peer.os,
        "kill",
        lambda *_a: pytest.fail("read-back PID must never be signalled"),
    )
    assert peer.process_exited({"pid": 42, "start": 100}) is True


def test_peer_fallback_loads_the_actual_sibling_module(verifier):
    assert verifier._peer_module().identity("a" * 16)["peer"] == "192.0.2.2"


@pytest.mark.parametrize(
    "change",
    [
        lambda report, ready: report.update(critical_findings=["failure"]),
        lambda report, ready: report["resident_health"].update(resident_pid=True),
        lambda report, ready: ready.update(status="stale"),
    ],
)
def test_inconsistent_or_critical_resident_is_denied(verifier, change):
    report = evidence()
    ready = {"status": "ready", "resident_health": dict(report["resident_health"])}
    change(report, ready)
    with pytest.raises(verifier.VerificationError, match="fence_resident_inconsistent"):
        verifier.assert_report(report, ready)


def test_malformed_findings_and_missing_new_matching_row_are_denied(verifier):
    with pytest.raises(verifier.VerificationError, match="fence_findings_invalid"):
        verifier.assert_rejections({"findings": None})
    with pytest.raises(verifier.VerificationError, match="fence_findings_invalid"):
        verifier.matching_rejection({"findings": [None] * 4097}, "tcp", 443)
    with pytest.raises(
        verifier.VerificationError, match="fence_rejection_missing:tcp:443"
    ):
        verifier.assert_new_rejection({"findings": []}, {"findings": []}, "tcp", 443)
    with pytest.raises(verifier.VerificationError, match="dns_refusal_invalid"):
        verifier.assert_dns_refused(b"", b"")


def trusted_stat(monkeypatch, verifier):
    original = verifier.os.fstat

    def fstat(descriptor):
        value = original(descriptor)
        return SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_size=value.st_size)

    monkeypatch.setattr(verifier.os, "fstat", fstat)


def test_protected_reader_bounds_and_refuses_symlinks_untrusted_or_writable_files(
    verifier, tmp_path, monkeypatch
):
    import stat

    path = tmp_path / "report.json"
    path.write_bytes(b"valid")
    trusted_stat(monkeypatch, verifier)
    assert verifier.read_protected(path) == b"valid"
    with pytest.raises(verifier.VerificationError, match="fence_file_too_large"):
        verifier.read_protected(path, 4)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        verifier.read_protected(link)
    monkeypatch.setattr(
        verifier.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o666, st_uid=0, st_size=5
        ),
    )
    with pytest.raises(verifier.VerificationError, match="untrusted_fence_file"):
        verifier.read_protected(path)


def test_protected_reader_detects_growth_after_metadata_check(
    verifier, tmp_path, monkeypatch
):
    import stat

    path = tmp_path / "report.json"
    path.write_bytes(b"123456")
    monkeypatch.setattr(
        verifier.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644, st_uid=0, st_size=1
        ),
    )
    with pytest.raises(verifier.VerificationError, match="fence_file_too_large"):
        verifier.read_protected(path, 5)


def test_action_files_verify_every_byte_and_refuse_changed_runtime(
    verifier, tmp_path, monkeypatch
):
    import hashlib

    path = tmp_path / "lib.cts"
    path.write_bytes(b"reviewed runtime")
    trusted_stat(monkeypatch, verifier)
    monkeypatch.setattr(
        verifier,
        "ACTION_HASHES",
        {"lib.cts": hashlib.sha256(path.read_bytes()).hexdigest()},
    )
    assert verifier.action_files(tmp_path) == {"lib.cts": b"reviewed runtime"}
    path.write_bytes(b"changed runtime")
    with pytest.raises(
        verifier.VerificationError, match="fence_action_identity_changed:lib.cts"
    ):
        verifier.action_files(tmp_path)


def test_node_calls_are_bounded_sanitized_and_require_real_major(verifier, monkeypatch):
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout=b"v24.21.0\n", stderr=b""),
            SimpleNamespace(returncode=0, stdout=b"v22.0.0\n", stderr=b""),
            SimpleNamespace(returncode=1, stdout=b"", stderr=b"x" * 65537),
        ]
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return next(results)

    monkeypatch.setattr(verifier.subprocess, "run", run)
    verifier.require_node24(Path("/real-node"))
    assert calls[0][1]["env"] == verifier.ENV
    assert calls[0][1]["timeout"] == 10
    with pytest.raises(
        verifier.VerificationError, match="fence_requires_actual_node24"
    ):
        verifier.require_node24(Path("/old-node"))
    with pytest.raises(verifier.VerificationError, match="fence_node_output_unbounded"):
        verifier._node(Path("/real-node"), "--version")


def test_upstream_adapter_calls_exact_full_validators_and_rejects_bad_or_unbounded_evidence(
    verifier, monkeypatch
):
    report = evidence()
    ready = {"status": "ready", "resident_health": report["resident_health"]}
    monkeypatch.setattr(verifier, "require_node24", lambda _node: None)
    monkeypatch.setattr(verifier, "action_files", lambda _root: {})
    calls = []

    def node(_node, *arguments, payload=None):
        calls.append((arguments, json.loads(payload)))
        return SimpleNamespace(returncode=0, stdout=b"validated")

    monkeypatch.setattr(verifier, "_node", node)
    verifier.validate_upstream(
        Path("/node24"), Path("/action"), report, ready, {}, "active"
    )
    assert calls[0][0] == ("-e", verifier.VALIDATE, "/action/lib.cts")
    assert all(
        name in verifier.VALIDATE
        for name in (
            "validateReport",
            "validateReady",
            "validateDnsEvidence",
            "validateResidentUnitStatus",
        )
    )
    monkeypatch.setattr(
        verifier,
        "_node",
        lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout=b"validated"),
    )
    with pytest.raises(
        verifier.VerificationError, match="upstream_fence_validation_failed"
    ):
        verifier.validate_upstream(
            Path("/node24"), Path("/action"), report, ready, {}, "active"
        )
    with pytest.raises(verifier.VerificationError, match="fence_evidence_too_large"):
        verifier.validate_upstream(
            Path("/node24"),
            Path("/action"),
            report,
            ready,
            {"unbounded": "x" * (1024 * 1024)},
            "active",
        )


def test_checksum_diagnostic_only_mutates_its_private_copy_and_requires_the_exact_rejection(
    verifier, monkeypatch
):
    files = {"main.cts": b"actual command seam", "bin/fence": b"binary"}
    monkeypatch.setattr(verifier, "require_node24", lambda _node: None)
    monkeypatch.setattr(verifier, "action_files", lambda _root: files)
    observed = []

    def node(_node, target):
        copied = Path(target).parent
        assert (copied / "bin/fence").read_bytes() == b"binarx"
        observed.append(copied)
        return SimpleNamespace(
            returncode=1,
            stderr=b"bundled Fence binary checksum does not match its manifest",
        )

    monkeypatch.setattr(verifier, "_node", node)
    verifier.assert_bundle_rejected(Path("/node24"), Path("/live-action"))
    assert files["bin/fence"] == b"binary"
    assert not observed[0].exists()
    monkeypatch.setattr(
        verifier,
        "_node",
        lambda *_a: SimpleNamespace(returncode=1, stderr=b"unsupported platform"),
    )
    with pytest.raises(
        verifier.VerificationError, match="fence_integrity_rejection_not_observed"
    ):
        verifier.assert_bundle_rejected(Path("/node24"), Path("/live-action"))


def test_documents_load_only_exact_fixed_paths(verifier, monkeypatch):
    seen = []
    monkeypatch.setattr(
        verifier,
        "read_protected",
        lambda path, limit: (seen.append((path.name, limit)), b"{}")[1],
    )
    assert verifier._documents(Path("/run/fence/exact")) == [{}, {}, {}]
    assert seen == [
        (name, 512 * 1024) for name in ("report.json", "ready.json", "dns-report.json")
    ]


def test_runner_node_is_discovered_only_from_an_actual_worker_ancestor(
    verifier, tmp_path, monkeypatch
):
    root = tmp_path
    worker = root / "runner/bin/Runner.Worker"
    worker.parent.mkdir(parents=True)
    worker.touch()
    proc = root / "proc/21"
    proc.mkdir(parents=True)
    (proc / "exe").symlink_to(worker)
    original = verifier.Path
    monkeypatch.setattr(
        verifier,
        "Path",
        lambda value: root / "proc" if value == "/proc" else original(value),
    )
    monkeypatch.setattr(verifier.os, "getppid", lambda: 21)
    seen = []
    monkeypatch.setattr(verifier, "require_node24", seen.append)
    assert verifier.runner_node24() == root / "runner/externals/node24/bin/node"
    assert len(seen) == 1
    (proc / "exe").unlink()
    ordinary = root / "ordinary"
    ordinary.touch()
    (proc / "exe").symlink_to(ordinary)
    (proc / "stat").write_text("21 (ordinary process) S 1 " + "0 " * 20)
    with pytest.raises(verifier.VerificationError, match="runner_node24_unavailable"):
        verifier.runner_node24()


def test_invocation_and_cli_fail_closed_without_setup_mutation(
    verifier, monkeypatch, capsys
):
    with pytest.raises(verifier.VerificationError, match="fence_invocation_invalid"):
        verifier.verify("a" * 16, "../escape", Path("/node24"))
    monkeypatch.setattr(verifier, "runner_node24", lambda: Path("/node24"))
    monkeypatch.setattr(verifier, "verify", lambda *_a: {"verified": True})
    assert verifier.main(["--fixture-id", "a" * 16, "--invocation", "fixed"]) == 0
    assert json.loads(capsys.readouterr().out) == {"verified": True}
    monkeypatch.setattr(
        verifier,
        "verify",
        lambda *_a: (_ for _ in ()).throw(
            verifier.VerificationError("fence_not_protected")
        ),
    )
    assert verifier.main(["--fixture-id", "a" * 16, "--invocation", "fixed"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "fence_not_protected"
