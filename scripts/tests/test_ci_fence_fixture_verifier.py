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
        "ruleset_hash": "c" * 64,
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
    before["counters"].update(total_violations=0, sampled_violations=0)
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
    report = report_snapshot(total=0, sampled=0)
    ready = startup_ready(report)
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
        verifier,
        "_documents",
        lambda *_a: (copy.deepcopy(report), ready, {}, json.dumps(ready).encode()),
    )

    def validate(_n, _r, value, readiness, _d, unit, *, initial=True):
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

    def validation(_n, _r, value, readiness, _d, unit, *, initial=True):
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
        verifier.assert_new_rejection(report_snapshot(), report_snapshot(), "tcp", 443)
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
    assert calls[0][0] == (
        "-e",
        verifier.VALIDATE,
        "/action/lib.cts",
        "/action/post.cts",
    )
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
    assert verifier._documents(Path("/run/fence/exact")) == ({}, {}, {}, b"{}")
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


def report_snapshot(*, total=9, sampled=4, epoch="c"):
    value = evidence()
    value.update(
        runtime_evidence_schema_version=5,
        platform_profile_id="github_hosted_workflow_bootstrap_v5",
        profile_realization_id="github_hosted_workflow_bootstrap_dns_provenance_v5",
        allow_github_artifacts=False,
        policy_hash_schema_version=9,
        policy_hash="a" * 64,
        base_ruleset_hash="b" * 64,
        ruleset_hash=epoch * 64,
    )
    value["resident_health"]["workers"] = [
        {"name": name, "status": "running"}
        for name in ["host_tcp_dns", "host_udp_dns", "process_attribution"]
    ]
    value["findings"] = []
    value["counters"] = {"total_violations": total, "sampled_violations": sampled}
    return value


def startup_ready(report):
    keys = (
        "runtime_evidence_schema_version",
        "platform_profile_id",
        "profile_realization_id",
        "allow_github_artifacts",
        "policy_hash_schema_version",
        "policy_hash",
        "base_ruleset_hash",
        "ruleset_hash",
        "protection_available",
        "resident_health",
    )
    return {
        **{key: copy.deepcopy(report[key]) for key in keys},
        "status": report["readiness_status"],
    }


def new_target(report, protocol="tcp", port=19443):
    report["findings"].append(
        {
            "classification": "rejected",
            "remote_address": "192.0.2.2",
            "protocol": protocol,
            "remote_port": port,
        }
    )
    return report


def test_legitimate_native_counter_epoch_accepts_a_new_target(verifier):
    before = report_snapshot()
    after = new_target(report_snapshot(total=0, sampled=5, epoch="d"))
    verifier.assert_new_rejection(before, after, "tcp", 19443)


def test_native_sampled_catchup_does_not_require_extra_kernel_counter_growth(verifier):
    before = report_snapshot()
    after = new_target(report_snapshot(total=9, sampled=5))
    verifier.assert_new_rejection(before, after, "tcp", 19443)


def test_initial_ready_binds_the_same_raw_read_and_later_bytes_never_change(verifier):
    report = report_snapshot()
    ready = startup_ready(report)
    raw = json.dumps(ready).encode()
    state = verifier.EvidenceState()
    state.accept(report, ready, raw)
    assert state.ready_bytes == raw
    later = report_snapshot(total=0, sampled=5, epoch="d")
    later["resident_health"]["verification_sequence"] = 10
    state.accept(later, ready, raw)
    with pytest.raises(
        verifier.VerificationError, match="fence_startup_readiness_changed"
    ):
        state.accept(later, ready, raw + b" ")


def test_every_adjacent_observation_checks_counters_before_target_polling(verifier):
    state = verifier.EvidenceState()
    initial = report_snapshot()
    ready = startup_ready(initial)
    raw = json.dumps(ready).encode()
    state.accept(initial, ready, raw)
    state.accept(report_snapshot(total=11, sampled=6), ready, raw)
    with pytest.raises(
        verifier.VerificationError, match="fence_sampled_counter_regressed"
    ):
        state.accept(report_snapshot(total=12, sampled=5), ready, raw)


@pytest.mark.parametrize("counter", [True, -1, 1.5, 2**53])
def test_invalid_native_counter_types_are_fatal_even_without_a_target(
    verifier, counter
):
    before = report_snapshot()
    after = report_snapshot(sampled=counter)
    with pytest.raises(verifier.VerificationError, match="fence_counters_invalid"):
        verifier.assert_new_rejection(before, after, "tcp", 19443)


def test_a_b_a_counter_epochs_use_adjacent_observations_not_probe_endpoints(verifier):
    before = report_snapshot(total=9, sampled=4, epoch="a")
    ready = startup_ready(before)
    raw = json.dumps(ready).encode()
    state = verifier.EvidenceState()
    state.accept(before, ready, raw)
    state.accept(report_snapshot(total=0, sampled=4, epoch="b"), ready, raw)
    after = new_target(report_snapshot(total=1, sampled=5, epoch="a"))
    state.accept(after, ready, raw)
    verifier.assert_new_rejection(before, after, "tcp", 19443)


def test_native_failure_diagnostic_is_staged_bounded_and_never_dumps_input(
    verifier, monkeypatch, capsys
):
    marker = "private-sentinel-never-log"
    report = report_snapshot()
    report["untrusted_extra"] = marker
    ready = startup_ready(report)
    monkeypatch.setattr(verifier, "require_node24", lambda _node: None)
    monkeypatch.setattr(verifier, "action_files", lambda _root: {})
    record = {
        "stage": "startup_ready",
        "reason": "Fence resident health evidence is stale or has an invalid timestamp",
        "native_code": marker,
        "extra": marker,
        "projections": [
            {
                "document": "startup_ready",
                "health": {
                    "reported_resident_status": "healthy",
                    "verification_sequence": 9,
                    "heartbeat_age_ms": 60000,
                    "critical_findings": 0,
                    "critical_codes": [marker],
                    "critical_findings_truncated": False,
                    "untrusted": marker,
                },
            }
        ],
    }
    monkeypatch.setattr(
        verifier,
        "_node",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=1, stdout=json.dumps(record).encode(), stderr=marker.encode()
        ),
    )
    with pytest.raises(
        verifier.VerificationError, match="^upstream_fence_validation_failed$"
    ):
        verifier.validate_upstream(
            Path("/node24"), Path("/action"), report, ready, {}, marker
        )
    output = capsys.readouterr().err
    assert marker not in output
    diagnostic = json.loads(output)
    assert diagnostic["stage"] == "startup_ready"
    assert diagnostic["reason"] == record["reason"]
    assert diagnostic["native_code"] is None
    assert diagnostic["unverified_health"][0]["heartbeat_age_ms"] == 60000
    assert len(output.encode()) < 8192


@pytest.mark.parametrize(
    "field",
    [
        "runtime_evidence_schema_version",
        "platform_profile_id",
        "profile_realization_id",
        "allow_github_artifacts",
        "policy_hash_schema_version",
        "policy_hash",
        "base_ruleset_hash",
        "protection_available",
        "readiness_status",
    ],
)
def test_current_report_must_keep_every_native_startup_identity_binding(
    verifier, field
):
    first = report_snapshot()
    ready = startup_ready(first)
    raw = json.dumps(ready).encode()
    state = verifier.EvidenceState()
    state.accept(first, ready, raw)
    changed = report_snapshot()
    changed[field] = "changed"
    with pytest.raises(
        verifier.VerificationError, match="fence_startup_identity_changed"
    ):
        state.accept(changed, ready, raw)


@pytest.mark.parametrize("change", ["pid", "workers"])
def test_original_resident_and_worker_binding_cannot_change(verifier, change):
    first = report_snapshot()
    ready = startup_ready(first)
    raw = json.dumps(ready).encode()
    state = verifier.EvidenceState()
    state.accept(first, ready, raw)
    changed = report_snapshot()
    if change == "pid":
        changed["resident_health"]["resident_pid"] += 1
    else:
        changed["resident_health"]["workers"].pop()
    with pytest.raises(
        verifier.VerificationError, match="fence_startup_resident_changed"
    ):
        state.accept(changed, ready, raw)


def test_adjacent_same_epoch_rollback_is_fatal_and_cannot_replace_last_accepted_report(
    verifier,
):
    first = report_snapshot()
    ready = startup_ready(first)
    raw = json.dumps(ready).encode()
    state = verifier.EvidenceState()
    state.accept(first, ready, raw)
    with pytest.raises(
        verifier.VerificationError, match="fence_total_counter_regressed"
    ):
        state.accept(report_snapshot(total=8, sampled=5), ready, raw)
    assert state.previous is first
    with pytest.raises(verifier.VerificationError, match="fence_counter_epoch_invalid"):
        verifier.counter_values({**first, "mode": "audit"})
    with pytest.raises(verifier.VerificationError, match="fence_counter_epoch_invalid"):
        verifier.counter_values({**first, "ruleset_hash": "bad"})


def test_valid_absence_is_the_only_pending_target_condition(verifier):
    first = report_snapshot()
    unrelated_growth = report_snapshot(total=10, sampled=5)
    with pytest.raises(verifier.RejectionPending, match="fence_rejection_missing"):
        verifier.assert_new_rejection(first, unrelated_growth, "tcp", 19443)
    target_without_sample = new_target(report_snapshot(total=10, sampled=4))
    with pytest.raises(
        verifier.VerificationError, match="fence_rejection_counter_missing"
    ) as failure:
        verifier.assert_new_rejection(first, target_without_sample, "tcp", 19443)
    assert not isinstance(failure.value, verifier.RejectionPending)


def test_same_raw_startup_buffer_is_bound_only_after_initial_native_validation(
    verifier, monkeypatch
):
    first = report_snapshot()
    ready = startup_ready(first)
    raw = json.dumps(ready).encode()
    current = [first, ready, {}, raw]
    state = verifier.EvidenceState()
    reads, validations = [], []
    monkeypatch.setattr(
        verifier, "_documents", lambda _path: (reads.append(True), tuple(current))[1]
    )
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(stdout=b"current-unit"),
    )

    def native(*_args, initial):
        validations.append(initial)
        current[3] = raw + b" "

    monkeypatch.setattr(verifier, "validate_upstream", native)
    verifier.validated_documents(
        Path("/node"), Path("/root"), Path("/runtime"), "fixed", state
    )
    assert state.ready_bytes == raw and len(reads) == 1
    with pytest.raises(
        verifier.VerificationError, match="fence_startup_readiness_changed"
    ):
        verifier.validated_documents(
            Path("/node"), Path("/root"), Path("/runtime"), "fixed", state
        )
    assert validations == [True]
    current[3] = raw
    verifier.validated_documents(
        Path("/node"), Path("/root"), Path("/runtime"), "fixed", state
    )
    assert validations == [True, False]
    fresh = verifier.EvidenceState()

    def denied(*_args, **_kwargs):
        raise verifier.VerificationError("native_rejected")

    monkeypatch.setattr(verifier, "validate_upstream", denied)
    with pytest.raises(verifier.VerificationError, match="native_rejected"):
        verifier.validated_documents(
            Path("/node"), Path("/root"), Path("/runtime"), "fixed", fresh
        )
    assert fresh.ready_bytes is None and fresh.previous is None


def test_rollback_between_probe_brackets_is_fatal_before_later_effects(monkeypatch):
    verifier, probes = verifier_fixture(monkeypatch)
    original = verifier._documents
    observations = [0]

    def documents(path):
        report, ready, dns, raw = original(path)
        observations[0] += 1
        if observations[0] == 2:
            report["counters"].update(total_violations=10, sampled_violations=5)
        return report, ready, dns, raw

    monkeypatch.setattr(verifier, "_documents", documents)
    with pytest.raises(
        verifier.VerificationError, match="fence_sampled_counter_regressed"
    ):
        verifier.verify("a" * 16, "fence-synthetic", Path("/actual-node24"))
    assert probes == ["allowed", "excluded"]


@pytest.mark.parametrize(
    "output", [b"not json", b"\xff", b"[]", b'{"stage":[],"reason":{}}']
)
def test_unexpected_native_failures_are_opaque_and_do_not_replace_the_original_failure(
    verifier, output
):
    marker = b"private-sentinel-never-log"
    result = SimpleNamespace(stdout=output, stderr=marker)
    record = verifier.native_diagnostic(result, marker)
    assert (
        record["stage"] == "unknown" and record["reason"] == "unrecognized_native_error"
    )
    assert marker.decode() not in json.dumps(record)


def test_unverified_projection_rejects_free_form_values_and_remains_bounded(verifier):
    marker = "private-sentinel-never-log"
    rows = [
        {"document": marker, "health": {}},
        {"document": "report", "health": None},
        {
            "document": "dns",
            "health": {
                "reported_resident_status": marker,
                "verification_sequence": True,
                "heartbeat_age_ms": 2**53,
                "critical_findings": 65,
                "critical_findings_truncated": marker,
                "critical_codes": [marker],
            },
        },
    ]
    record = verifier.native_diagnostic(
        SimpleNamespace(
            stdout=json.dumps({"projections": rows * 100}).encode(), stderr=b""
        ),
        b"{}",
    )
    assert len(record["unverified_health"]) == 1
    assert marker not in json.dumps(record)
    assert record["unverified_health"][0] == {
        "document": "dns",
        "reported_resident_status": "unavailable",
        "verification_sequence": None,
        "heartbeat_age_ms": None,
        "critical_findings": None,
        "critical_findings_truncated": None,
    }


V2_OLD_PIN = "abc5790eac3445fe3854eea6b506aa7d31bed67c"
V2_NEW_PIN = "f9751ca5d767dc0199e88733c2b286bd4800a008"


def _v2_evidence():
    return {
        "mode": "block",
        "status": "protected_host_block",
        "readiness_status": "ready",
        "protection_available": True,
        "network_verification_status": "verified",
        "sudo_status": "disabled_verified",
        "container_status": "disabled_verified",
        "critical_findings": [],
        "ruleset_hash": "c" * 64,
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


def _v2_observations(offset=0, denied=False):
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


def _v2_verifier_fixture(
    monkeypatch, *, stale=False, die_on_probe=False, baseline_answered=True
):
    _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
    peer = importlib.import_module("scripts.ci_network_fixture")
    report = _v2_report_snapshot(total=0, sampled=0)
    ready = _v2_startup_ready(report)
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
    before = _v2_observations()
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
            (
                flow
                for flow in _v2_observations(20, True)["flows"]
                if flow["name"] == name
            )
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
    monkeypatch.setattr(_v2_verifier, "_peer_module", lambda: peer)
    monkeypatch.setattr(
        _v2_verifier,
        "_documents",
        lambda *_a: (copy.deepcopy(report), ready, {}, json.dumps(ready).encode()),
    )

    def validate(_n, _r, value, readiness, _d, unit, *, initial=True):
        _v2_verifier.assert_report(value, readiness)
        if "ActiveState=active" not in unit:
            raise _v2_verifier.VerificationError("resident_exited")

    def unit_status(*_args, **_kwargs):
        return SimpleNamespace(
            stdout=b"ActiveState=active\nMainPID=42\nSubState=running\n"
            if active[0]
            else b"ActiveState=inactive\nMainPID=0\nSubState=dead\n"
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
                + struct.pack("!HHHHH", 33157, 1, 0, 0, 0)
                + self.query[12:]
            )

    monkeypatch.setattr(_v2_verifier, "validate_upstream", validate)
    monkeypatch.setattr(_v2_verifier, "assert_bundle_rejected", lambda *_a: None)
    monkeypatch.setattr(_v2_verifier.subprocess, "run", unit_status)
    monkeypatch.setattr(_v2_verifier.socket, "socket", lambda *_a: Resolver())
    return (_v2_verifier, probes)


def _v2_trusted_stat(monkeypatch, verifier):
    original = verifier.os.fstat

    def fstat(descriptor):
        value = original(descriptor)
        return SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_size=value.st_size)

    monkeypatch.setattr(verifier.os, "fstat", fstat)


def _v2_report_snapshot(*, total=9, sampled=4, epoch="c"):
    value = _v2_evidence()
    value.update(
        runtime_evidence_schema_version=5,
        platform_profile_id="github_hosted_workflow_bootstrap_v5",
        profile_realization_id="github_hosted_workflow_bootstrap_dns_provenance_v5",
        allow_github_artifacts=False,
        policy_hash_schema_version=9,
        policy_hash="a" * 64,
        base_ruleset_hash="b" * 64,
        ruleset_hash=epoch * 64,
    )
    value["resident_health"]["workers"] = [
        {"name": name, "status": "running"}
        for name in ["host_tcp_dns", "host_udp_dns", "process_attribution"]
    ]
    value["findings"] = []
    value["counters"] = {"total_violations": total, "sampled_violations": sampled}
    return value


def _v2_startup_ready(report):
    keys = (
        "mode",
        "runtime_evidence_schema_version",
        "platform_profile_id",
        "profile_realization_id",
        "allow_github_artifacts",
        "policy_hash_schema_version",
        "policy_hash",
        "base_ruleset_hash",
        "ruleset_hash",
        "protection_available",
        "resident_health",
    )
    return {
        **{key: copy.deepcopy(report[key]) for key in keys},
        "status": report["readiness_status"],
    }


def _v2_new_target(report, protocol="tcp", port=19443):
    report["findings"].append(
        {
            "classification": "rejected",
            "remote_address": "192.0.2.2",
            "protocol": protocol,
            "remote_port": port,
        }
    )
    return report


class TestDormantFenceContract:
    @pytest.fixture
    def verifier(self):
        return importlib.import_module("scripts.ci_fence_fixture_verifier_v2")

    @pytest.fixture(autouse=True)
    def explicit_old_profile(self):
        module = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        with module._profile_scope(V2_OLD_PIN):
            yield

    def test_actual_report_requires_protected_ready_consistent_resident(self, verifier):
        report = _v2_evidence()
        ready = {
            "status": "ready",
            "mode": "block",
            "resident_health": report["resident_health"],
        }
        verifier.assert_report(report, ready)
        report["mode"] = "audit"
        with pytest.raises(verifier.VerificationError, match="fence_not_protected"):
            verifier.assert_report(report, ready)

    def test_every_excluded_flow_requires_actual_matching_rejection(self, verifier):
        report = _v2_evidence()
        verifier.assert_rejections(report)
        report["findings"].pop()
        with pytest.raises(
            verifier.VerificationError, match="fence_rejection_missing:udp:53"
        ):
            verifier.assert_rejections(report)

    def test_dns_refusal_requires_the_same_question_and_transaction(self, verifier):
        question = b"\x07outside\x07invalid\x00\x00\x01\x00\x01"
        query = struct.pack("!HHHHHH", 7, 256, 1, 0, 0, 0) + question
        response = struct.pack("!HHHHHH", 7, 33157, 1, 0, 0, 0) + question
        verifier.assert_dns_refused(query, response)
        with pytest.raises(verifier.VerificationError, match="dns_refusal_invalid"):
            verifier.assert_dns_refused(query, response[:-1])

    def test_old_matching_rows_plus_unrelated_counter_never_qualify_a_new_probe(
        self, verifier
    ):
        before = _v2_evidence()
        after = _v2_evidence()
        after["counters"]["total_violations"] += 3
        with pytest.raises(
            verifier.VerificationError, match="fence_rejection_preexisting"
        ):
            verifier.assert_new_rejection(before, after, "tcp", 443)

    def test_a_new_matching_row_also_requires_counter_advancement(self, verifier):
        before = _v2_evidence()
        before["findings"] = []
        after = _v2_evidence()
        with pytest.raises(
            verifier.VerificationError, match="fence_rejection_counter_missing"
        ):
            verifier.assert_new_rejection(before, after, "udp", 53)
        before["counters"].update(total_violations=0, sampled_violations=0)
        verifier.assert_new_rejection(before, after, "udp", 53)

    def test_old_matching_findings_are_rejected_before_new_probes(self, monkeypatch):
        verifier, probes = _v2_verifier_fixture(monkeypatch, stale=True)
        with pytest.raises(
            verifier.VerificationError, match="fence_rejection_preexisting"
        ):
            verifier.verify(
                "a" * 16,
                "fence-synthetic",
                Path("/actual-node24"),
                action_pin=V2_OLD_PIN,
            )
        assert probes == []

    def test_actual_current_unit_is_checked_after_probe(self, monkeypatch):
        verifier, _probes = _v2_verifier_fixture(monkeypatch, die_on_probe=True)
        with pytest.raises(verifier.VerificationError, match="resident_exited"):
            verifier.verify(
                "a" * 16,
                "fence-synthetic",
                Path("/actual-node24"),
                action_pin=V2_OLD_PIN,
            )

    def test_new_bracketed_findings_can_pass_the_adapter_positive_control(
        self, monkeypatch
    ):
        verifier, probes = _v2_verifier_fixture(monkeypatch)
        result = verifier.verify(
            "a" * 16, "fence-synthetic", Path("/actual-node24"), action_pin=V2_OLD_PIN
        )
        assert result["network"] == "controlled_denials_verified"
        assert probes == ["allowed", "excluded", "address", "dns"]

    def test_verified_memory_peer_is_used_without_reopening_source(self, monkeypatch):
        verifier, probes = _v2_verifier_fixture(monkeypatch)
        loaded = verifier._peer_module()
        monkeypatch.setattr(
            verifier, "_peer_module", lambda: pytest.fail("reopened peer source")
        )
        result = verifier.verify(
            "a" * 16,
            "fence-synthetic",
            Path("/actual-node24"),
            peer=loaded,
            action_pin=V2_OLD_PIN,
        )
        assert result["network"] == "controlled_denials_verified"
        assert probes == ["allowed", "excluded", "address", "dns"]

    def test_absent_successful_baseline_cannot_qualify_later_denials(self, monkeypatch):
        verifier, _probes = _v2_verifier_fixture(monkeypatch, baseline_answered=False)
        with pytest.raises(RuntimeError, match="baseline_unreachable:excluded"):
            verifier.verify(
                "a" * 16,
                "fence-synthetic",
                Path("/actual-node24"),
                action_pin=V2_OLD_PIN,
            )

    def test_current_unit_is_checked_again_after_checksum_probe(self, monkeypatch):
        verifier, _probes = _v2_verifier_fixture(monkeypatch)
        inactive = [False]

        def validation(_n, _r, value, readiness, _d, unit, *, initial=True):
            verifier.assert_report(value, readiness)
            if inactive[0]:
                raise verifier.VerificationError("resident_exited_after_integrity")

        monkeypatch.setattr(verifier, "validate_upstream", validation)
        monkeypatch.setattr(
            verifier,
            "assert_bundle_rejected",
            lambda *_a: inactive.__setitem__(0, True),
        )
        with pytest.raises(
            verifier.VerificationError, match="resident_exited_after_integrity"
        ):
            verifier.verify(
                "a" * 16,
                "fence-synthetic",
                Path("/actual-node24"),
                action_pin=V2_OLD_PIN,
            )

    def test_cleanup_receipt_does_not_accept_a_still_executing_original_supervisor(
        self, tmp_path, monkeypatch
    ):
        peer = importlib.import_module("scripts.ci_network_fixture")
        state = {
            "id": "a" * 16,
            "nonce": "b" * 32,
            "supervisor": {"pid": 42, "start": 100},
        }
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

    def test_terminal_pid_reuse_is_safe_and_never_signalled(self, monkeypatch):
        peer = importlib.import_module("scripts.ci_network_fixture")
        monkeypatch.setattr(peer, "process_identity", lambda _pid: (101, "S"))
        monkeypatch.setattr(
            peer.os,
            "kill",
            lambda *_a: pytest.fail("read-back PID must never be signalled"),
        )
        assert peer.process_exited({"pid": 42, "start": 100}) is True

    def test_peer_fallback_loads_the_actual_sibling_module(self, verifier):
        assert verifier._peer_module().identity("a" * 16)["peer"] == "192.0.2.2"

    @pytest.mark.parametrize(
        "change",
        [
            lambda report, ready: report.update(critical_findings=["failure"]),
            lambda report, ready: report["resident_health"].update(resident_pid=True),
            lambda report, ready: ready.update(status="stale"),
        ],
    )
    def test_inconsistent_or_critical_resident_is_denied(self, verifier, change):
        report = _v2_evidence()
        ready = {"status": "ready", "resident_health": dict(report["resident_health"])}
        change(report, ready)
        with pytest.raises(
            verifier.VerificationError, match="fence_resident_inconsistent"
        ):
            verifier.assert_report(report, ready)

    def test_malformed_findings_and_missing_new_matching_row_are_denied(self, verifier):
        with pytest.raises(verifier.VerificationError, match="fence_findings_invalid"):
            verifier.assert_rejections({"findings": None})
        with pytest.raises(verifier.VerificationError, match="fence_findings_invalid"):
            verifier.matching_rejection({"findings": [None] * 4097}, "tcp", 443)
        with pytest.raises(
            verifier.VerificationError, match="fence_rejection_missing:tcp:443"
        ):
            verifier.assert_new_rejection(
                _v2_report_snapshot(), _v2_report_snapshot(), "tcp", 443
            )
        with pytest.raises(verifier.VerificationError, match="dns_refusal_invalid"):
            verifier.assert_dns_refused(b"", b"")

    def test_protected_reader_bounds_and_refuses_symlinks_untrusted_or_writable_files(
        self, verifier, tmp_path, monkeypatch
    ):
        import stat

        path = tmp_path / "report.json"
        path.write_bytes(b"valid")
        _v2_trusted_stat(monkeypatch, verifier)
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
                st_mode=stat.S_IFREG | 438, st_uid=0, st_size=5
            ),
        )
        with pytest.raises(verifier.VerificationError, match="untrusted_fence_file"):
            verifier.read_protected(path)

    def test_protected_reader_detects_growth_after_metadata_check(
        self, verifier, tmp_path, monkeypatch
    ):
        import stat

        path = tmp_path / "report.json"
        path.write_bytes(b"123456")
        monkeypatch.setattr(
            verifier.os,
            "fstat",
            lambda _descriptor: SimpleNamespace(
                st_mode=stat.S_IFREG | 420, st_uid=0, st_size=1
            ),
        )
        with pytest.raises(verifier.VerificationError, match="fence_file_too_large"):
            verifier.read_protected(path, 5)

    def test_action_files_verify_every_byte_and_refuse_changed_runtime(
        self, verifier, tmp_path, monkeypatch
    ):
        import hashlib

        path = tmp_path / "lib.cts"
        path.write_bytes(b"reviewed runtime")
        _v2_trusted_stat(monkeypatch, verifier)
        monkeypatch.setattr(
            verifier,
            "current_profile",
            lambda: {
                "hashes": {"lib.cts": hashlib.sha256(b"reviewed runtime").hexdigest()}
            },
        )
        assert verifier.action_files(tmp_path) == {"lib.cts": b"reviewed runtime"}
        path.write_bytes(b"changed runtime")
        with pytest.raises(
            verifier.VerificationError, match="fence_action_identity_changed:lib.cts"
        ):
            verifier.action_files(tmp_path)

    def test_node_calls_are_bounded_sanitized_and_require_real_major(
        self, verifier, monkeypatch
    ):
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
        with pytest.raises(
            verifier.VerificationError, match="fence_node_output_unbounded"
        ):
            verifier._node(Path("/real-node"), "--version")

    def test_upstream_adapter_calls_exact_full_validators_and_rejects_bad_or_unbounded_evidence(
        self, verifier, monkeypatch
    ):
        report = _v2_evidence()
        ready = {
            "status": "ready",
            "mode": "block",
            "resident_health": report["resident_health"],
        }
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
        assert calls[0][0] == (
            "-e",
            verifier.VALIDATE,
            "/action/lib.cts",
            "/action/post.cts",
        )
        assert all(
            (
                name in verifier.VALIDATE
                for name in (
                    "validateReport",
                    "validateReady",
                    "validateDnsEvidence",
                    "validateResidentUnitStatus",
                )
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
        with pytest.raises(
            verifier.VerificationError, match="fence_evidence_too_large"
        ):
            verifier.validate_upstream(
                Path("/node24"),
                Path("/action"),
                report,
                ready,
                {"unbounded": "x" * (1024 * 1024)},
                "active",
            )

    def test_checksum_diagnostic_only_mutates_its_private_copy_and_requires_the_exact_rejection(
        self, verifier, monkeypatch
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

    def test_documents_load_only_exact_fixed_paths(self, verifier, monkeypatch):
        seen = []
        monkeypatch.setattr(
            verifier,
            "read_protected",
            lambda path, limit, **kw: (
                seen.append((path.name, limit, kw.get("expected_mode"))),
                b"{}",
            )[1],
        )
        assert verifier._documents(Path("/run/fence/exact")) == ({}, {}, {}, b"{}")
        assert seen == [
            ("report.json", 512 * 1024, None),
            ("ready.json", 64 * 1024, 420),
            ("dns-report.json", 512 * 1024, None),
        ]

    def test_runner_node_is_discovered_only_from_an_actual_worker_ancestor(
        self, verifier, tmp_path, monkeypatch
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
        with pytest.raises(
            verifier.VerificationError, match="runner_node24_unavailable"
        ):
            verifier.runner_node24()

    def test_invocation_and_cli_fail_closed_without_setup_mutation(
        self, verifier, monkeypatch, capsys
    ):
        with pytest.raises(
            verifier.VerificationError, match="fence_invocation_invalid"
        ):
            verifier.verify(
                "a" * 16, "../escape", Path("/node24"), action_pin=V2_OLD_PIN
            )
        monkeypatch.setattr(verifier, "runner_node24", lambda: Path("/node24"))
        monkeypatch.setattr(verifier, "verify", lambda *_a, **_kw: {"verified": True})
        assert (
            verifier.main(
                [
                    "--action-pin",
                    V2_OLD_PIN,
                    "--fixture-id",
                    "a" * 16,
                    "--invocation",
                    "fixed",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out) == {"verified": True}
        monkeypatch.setattr(
            verifier,
            "verify",
            lambda *_a, **_kw: (_ for _ in ()).throw(
                verifier.VerificationError("fence_not_protected")
            ),
        )
        assert (
            verifier.main(
                [
                    "--action-pin",
                    V2_OLD_PIN,
                    "--fixture-id",
                    "a" * 16,
                    "--invocation",
                    "fixed",
                ]
            )
            == 1
        )
        assert json.loads(capsys.readouterr().err)["code"] == "fence_not_protected"

    def test_legitimate_native_counter_epoch_accepts_a_new_target(self, verifier):
        before = _v2_report_snapshot()
        after = _v2_new_target(_v2_report_snapshot(total=0, sampled=5, epoch="d"))
        verifier.assert_new_rejection(before, after, "tcp", 19443)

    def test_native_sampled_catchup_does_not_require_extra_kernel_counter_growth(
        self, verifier
    ):
        before = _v2_report_snapshot()
        after = _v2_new_target(_v2_report_snapshot(total=9, sampled=5))
        verifier.assert_new_rejection(before, after, "tcp", 19443)

    def test_initial_ready_binds_the_same_raw_read_and_later_bytes_never_change(
        self, verifier
    ):
        report = _v2_report_snapshot()
        ready = _v2_startup_ready(report)
        raw = json.dumps(ready).encode()
        state = verifier.EvidenceState()
        state.accept(report, ready, raw)
        assert state.ready_bytes == raw
        later = _v2_report_snapshot(total=0, sampled=5, epoch="d")
        later["resident_health"]["verification_sequence"] = 10
        state.accept(later, ready, raw)
        with pytest.raises(
            verifier.VerificationError, match="fence_startup_readiness_changed"
        ):
            state.accept(later, ready, raw + b" ")

    def test_every_adjacent_observation_checks_counters_before_target_polling(
        self, verifier
    ):
        state = verifier.EvidenceState()
        initial = _v2_report_snapshot()
        ready = _v2_startup_ready(initial)
        raw = json.dumps(ready).encode()
        state.accept(initial, ready, raw)
        state.accept(_v2_report_snapshot(total=11, sampled=6), ready, raw)
        with pytest.raises(
            verifier.VerificationError, match="fence_sampled_counter_regressed"
        ):
            state.accept(_v2_report_snapshot(total=12, sampled=5), ready, raw)

    @pytest.mark.parametrize("counter", [True, -1, 1.5, 2**53])
    def test_invalid_native_counter_types_are_fatal_even_without_a_target(
        self, verifier, counter
    ):
        before = _v2_report_snapshot()
        after = _v2_report_snapshot(sampled=counter)
        with pytest.raises(verifier.VerificationError, match="fence_counters_invalid"):
            verifier.assert_new_rejection(before, after, "tcp", 19443)

    def test_a_b_a_counter_epochs_use_adjacent_observations_not_probe_endpoints(
        self, verifier
    ):
        before = _v2_report_snapshot(total=9, sampled=4, epoch="a")
        ready = _v2_startup_ready(before)
        raw = json.dumps(ready).encode()
        state = verifier.EvidenceState()
        state.accept(before, ready, raw)
        state.accept(_v2_report_snapshot(total=0, sampled=4, epoch="b"), ready, raw)
        after = _v2_new_target(_v2_report_snapshot(total=1, sampled=5, epoch="a"))
        state.accept(after, ready, raw)
        verifier.assert_new_rejection(before, after, "tcp", 19443)

    def test_native_failure_diagnostic_is_staged_bounded_and_never_dumps_input(
        self, verifier, monkeypatch, capsys
    ):
        marker = "private-sentinel-never-log"
        report = _v2_report_snapshot()
        report["untrusted_extra"] = marker
        ready = _v2_startup_ready(report)
        monkeypatch.setattr(verifier, "require_node24", lambda _node: None)
        monkeypatch.setattr(verifier, "action_files", lambda _root: {})
        record = {
            "stage": "startup_ready",
            "reason": "Fence resident health evidence is stale or has an invalid timestamp",
            "native_code": marker,
            "extra": marker,
            "projections": [
                {
                    "document": "startup_ready",
                    "health": {
                        "reported_resident_status": "healthy",
                        "verification_sequence": 9,
                        "heartbeat_age_ms": 60000,
                        "critical_findings": 0,
                        "critical_codes": [marker],
                        "critical_findings_truncated": False,
                        "untrusted": marker,
                    },
                }
            ],
        }
        monkeypatch.setattr(
            verifier,
            "_node",
            lambda *_a, **_kw: SimpleNamespace(
                returncode=1, stdout=json.dumps(record).encode(), stderr=marker.encode()
            ),
        )
        with pytest.raises(
            verifier.VerificationError, match="^upstream_fence_validation_failed$"
        ):
            verifier.validate_upstream(
                Path("/node24"), Path("/action"), report, ready, {}, marker
            )
        output = capsys.readouterr().err
        assert marker not in output
        diagnostic = json.loads(output)
        assert diagnostic["stage"] == "startup_ready"
        assert diagnostic["reason"] == record["reason"]
        assert diagnostic["native_code"] is None
        assert diagnostic["unverified_health"][0]["heartbeat_age_ms"] == 60000
        assert len(output.encode()) < 8192

    @pytest.mark.parametrize(
        "field",
        [
            "runtime_evidence_schema_version",
            "platform_profile_id",
            "profile_realization_id",
            "allow_github_artifacts",
            "policy_hash_schema_version",
            "policy_hash",
            "base_ruleset_hash",
            "protection_available",
            "readiness_status",
        ],
    )
    def test_current_report_must_keep_every_native_startup_identity_binding(
        self, verifier, field
    ):
        first = _v2_report_snapshot()
        ready = _v2_startup_ready(first)
        raw = json.dumps(ready).encode()
        state = verifier.EvidenceState()
        state.accept(first, ready, raw)
        changed = _v2_report_snapshot()
        changed[field] = "changed"
        with pytest.raises(
            verifier.VerificationError, match="fence_startup_identity_changed"
        ):
            state.accept(changed, ready, raw)

    @pytest.mark.parametrize("change", ["pid", "workers"])
    def test_original_resident_and_worker_binding_cannot_change(self, verifier, change):
        first = _v2_report_snapshot()
        ready = _v2_startup_ready(first)
        raw = json.dumps(ready).encode()
        state = verifier.EvidenceState()
        state.accept(first, ready, raw)
        changed = _v2_report_snapshot()
        if change == "pid":
            changed["resident_health"]["resident_pid"] += 1
        else:
            changed["resident_health"]["workers"].pop()
        with pytest.raises(
            verifier.VerificationError, match="fence_startup_resident_changed"
        ):
            state.accept(changed, ready, raw)

    def test_adjacent_same_epoch_rollback_is_fatal_and_cannot_replace_last_accepted_report(
        self, verifier
    ):
        first = _v2_report_snapshot()
        ready = _v2_startup_ready(first)
        raw = json.dumps(ready).encode()
        state = verifier.EvidenceState()
        state.accept(first, ready, raw)
        with pytest.raises(
            verifier.VerificationError, match="fence_total_counter_regressed"
        ):
            state.accept(_v2_report_snapshot(total=8, sampled=5), ready, raw)
        assert state.previous is first
        with pytest.raises(
            verifier.VerificationError, match="fence_counter_epoch_invalid"
        ):
            verifier.counter_values({**first, "mode": "audit"})
        with pytest.raises(
            verifier.VerificationError, match="fence_counter_epoch_invalid"
        ):
            verifier.counter_values({**first, "ruleset_hash": "bad"})

    def test_valid_absence_is_the_only_pending_target_condition(self, verifier):
        first = _v2_report_snapshot()
        unrelated_growth = _v2_report_snapshot(total=10, sampled=5)
        with pytest.raises(verifier.RejectionPending, match="fence_rejection_missing"):
            verifier.assert_new_rejection(first, unrelated_growth, "tcp", 19443)
        target_without_sample = _v2_new_target(_v2_report_snapshot(total=10, sampled=4))
        with pytest.raises(
            verifier.VerificationError, match="fence_rejection_counter_missing"
        ) as failure:
            verifier.assert_new_rejection(first, target_without_sample, "tcp", 19443)
        assert not isinstance(failure.value, verifier.RejectionPending)

    def test_same_raw_startup_buffer_is_bound_only_after_initial_native_validation(
        self, verifier, monkeypatch
    ):
        first = _v2_report_snapshot()
        ready = _v2_startup_ready(first)
        raw = json.dumps(ready).encode()
        current = [first, ready, {}, raw]
        state = verifier.EvidenceState()
        reads, validations = ([], [])
        monkeypatch.setattr(
            verifier,
            "_documents",
            lambda _path: (reads.append(True), tuple(current))[1],
        )
        monkeypatch.setattr(
            verifier.subprocess,
            "run",
            lambda *_a, **_kw: SimpleNamespace(stdout=b"current-unit"),
        )

        def native(*_args, initial):
            validations.append(initial)
            current[3] = raw + b" "

        monkeypatch.setattr(verifier, "validate_upstream", native)
        verifier.validated_documents(
            Path("/node"), Path("/root"), Path("/runtime"), "fixed", state
        )
        assert state.ready_bytes == raw and len(reads) == 1
        with pytest.raises(
            verifier.VerificationError, match="fence_startup_readiness_changed"
        ):
            verifier.validated_documents(
                Path("/node"), Path("/root"), Path("/runtime"), "fixed", state
            )
        assert validations == [True]
        current[3] = raw
        verifier.validated_documents(
            Path("/node"), Path("/root"), Path("/runtime"), "fixed", state
        )
        assert validations == [True, False]
        fresh = verifier.EvidenceState()

        def denied(*_args, **_kwargs):
            raise verifier.VerificationError("native_rejected")

        monkeypatch.setattr(verifier, "validate_upstream", denied)
        with pytest.raises(verifier.VerificationError, match="native_rejected"):
            verifier.validated_documents(
                Path("/node"), Path("/root"), Path("/runtime"), "fixed", fresh
            )
        assert fresh.ready_bytes is None and fresh.previous is None

    def test_rollback_between_probe_brackets_is_fatal_before_later_effects(
        self, monkeypatch
    ):
        verifier, probes = _v2_verifier_fixture(monkeypatch)
        original = verifier._documents
        _v2_observations = [0]

        def documents(path):
            report, ready, dns, raw = original(path)
            _v2_observations[0] += 1
            if _v2_observations[0] == 2:
                report["counters"].update(total_violations=10, sampled_violations=5)
            return (report, ready, dns, raw)

        monkeypatch.setattr(verifier, "_documents", documents)
        with pytest.raises(
            verifier.VerificationError, match="fence_sampled_counter_regressed"
        ):
            verifier.verify(
                "a" * 16,
                "fence-synthetic",
                Path("/actual-node24"),
                action_pin=V2_OLD_PIN,
            )
        assert probes == ["allowed", "excluded"]

    @pytest.mark.parametrize(
        "output", [b"not json", b"\xff", b"[]", b'{"stage":[],"reason":{}}']
    )
    def test_unexpected_native_failures_are_opaque_and_do_not_replace_the_original_failure(
        self, verifier, output
    ):
        marker = b"private-sentinel-never-log"
        result = SimpleNamespace(stdout=output, stderr=marker)
        record = verifier.native_diagnostic(result, marker)
        assert (
            record["stage"] == "unknown"
            and record["reason"] == "unrecognized_native_error"
        )
        assert marker.decode() not in json.dumps(record)

    def test_unverified_projection_rejects_free_form_values_and_remains_bounded(
        self, verifier
    ):
        marker = "private-sentinel-never-log"
        rows = [
            {"document": marker, "health": {}},
            {"document": "report", "health": None},
            {
                "document": "dns",
                "health": {
                    "reported_resident_status": marker,
                    "verification_sequence": True,
                    "heartbeat_age_ms": 2**53,
                    "critical_findings": 65,
                    "critical_findings_truncated": marker,
                    "critical_codes": [marker],
                },
            },
        ]
        record = verifier.native_diagnostic(
            SimpleNamespace(
                stdout=json.dumps({"projections": rows * 100}).encode(), stderr=b""
            ),
            b"{}",
        )
        assert len(record["unverified_health"]) == 1
        assert marker not in json.dumps(record)
        assert record["unverified_health"][0] == {
            "document": "dns",
            "reported_resident_status": "unavailable",
            "verification_sequence": None,
            "heartbeat_age_ms": None,
            "critical_findings": None,
            "critical_findings_truncated": None,
        }


class TestDormantFenceProfiles:
    def test_reviewed_caller_must_select_an_exact_supported_bundle(self):
        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        for pin in (
            "",
            None,
            "v0.12.5",
            "latest",
            "424c1ac6cc2f844986f38defc6d43150796f35f5",
            "a" * 40,
        ):
            with pytest.raises(
                _v2_verifier.VerificationError, match="unreviewed_fence_action"
            ):
                _v2_verifier.reviewed_profile(pin)
        assert _v2_verifier.reviewed_profile(V2_OLD_PIN)["version"] == "v0.10.5"
        assert _v2_verifier.reviewed_profile(V2_NEW_PIN)["version"] == "v0.12.5"

    def test_no_implicit_profile_can_run_the_verifier(self):
        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        with pytest.raises(TypeError):
            _v2_verifier.verify("a" * 16, "fixture", None)

    def test_profile_is_required_and_environment_cannot_select_one(self, monkeypatch):
        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        monkeypatch.setenv("FENCE_ACTION_PIN", V2_NEW_PIN)
        with pytest.raises(
            _v2_verifier.VerificationError, match="reviewed_fence_profile_required"
        ):
            _v2_verifier.current_profile()
        with pytest.raises(SystemExit) as missing:
            _v2_verifier.main(["--fixture-id", "a" * 16, "--invocation", "fixed"])
        assert missing.value.code == 2

    def test_profile_scope_rejects_switching_and_resets_after_failure(self):
        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        with pytest.raises(RuntimeError, match="interrupted"):
            with _v2_verifier._profile_scope(V2_OLD_PIN):
                with _v2_verifier._profile_scope(V2_OLD_PIN):
                    assert _v2_verifier.current_profile()["pin"] == V2_OLD_PIN
                with pytest.raises(
                    _v2_verifier.VerificationError, match="fence_profile_changed"
                ):
                    with _v2_verifier._profile_scope(V2_NEW_PIN):
                        pass
                assert _v2_verifier.current_profile()["pin"] == V2_OLD_PIN
                raise RuntimeError("interrupted")
        with pytest.raises(
            _v2_verifier.VerificationError, match="reviewed_fence_profile_required"
        ):
            _v2_verifier.current_profile()

    def test_parallel_reviewed_callers_keep_independent_profiles(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        barrier = Barrier(2)

        def observe(pin):
            with _v2_verifier._profile_scope(pin):
                barrier.wait()
                return _v2_verifier.current_profile()["pin"]

        with ThreadPoolExecutor(max_workers=2) as workers:
            assert list(workers.map(observe, [V2_OLD_PIN, V2_NEW_PIN])) == [
                V2_OLD_PIN,
                V2_NEW_PIN,
            ]
        with pytest.raises(_v2_verifier.VerificationError):
            _v2_verifier.current_profile()

    def test_reviewed_profile_and_hashes_cannot_be_reconfigured(self):
        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        profile = _v2_verifier.reviewed_profile(V2_NEW_PIN)
        with pytest.raises(TypeError):
            profile["pin"] = V2_OLD_PIN
        with pytest.raises(TypeError):
            profile["hashes"]["bin/fence"] = "0" * 64
        assert (
            profile["hashes"]["bin/fence"]
            == "3434ef8eb32d130daf272c4794772b497a5d4390d458fd0ef73bac6612b6aa1a"
        )

    def test_unknown_caller_profile_is_refused_before_peer_or_evidence_access(
        self, monkeypatch
    ):
        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")

        def forbidden():
            raise AssertionError("unreviewed caller reached peer access")

        monkeypatch.setattr(_v2_verifier, "_peer_module", forbidden)
        with pytest.raises(
            _v2_verifier.VerificationError, match="unreviewed_fence_action"
        ):
            _v2_verifier.verify("a" * 16, "fixed", None, action_pin="unreviewed")

    @pytest.mark.parametrize(
        "pin,initial,phase",
        [
            (V2_OLD_PIN, True, "setup"),
            (V2_OLD_PIN, False, None),
            (V2_NEW_PIN, True, "setup"),
            (V2_NEW_PIN, False, "post"),
        ],
    )
    def test_native_readiness_uses_only_the_selected_bundles_reviewed_phase(
        self, monkeypatch, pin, initial, phase
    ):
        import json
        from pathlib import Path
        from types import SimpleNamespace

        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        report = _v2_report_snapshot()
        observed = []
        monkeypatch.setattr(_v2_verifier, "require_node24", lambda _node: None)
        monkeypatch.setattr(_v2_verifier, "action_files", lambda _root: {})

        def node(_node, *_args, payload):
            observed.append(json.loads(payload))
            return SimpleNamespace(returncode=0, stdout=b"validated")

        monkeypatch.setattr(_v2_verifier, "_node", node)
        with _v2_verifier._profile_scope(pin):
            _v2_verifier.validate_upstream(
                Path("/node24"),
                Path("/action"),
                report,
                _v2_startup_ready(report),
                {},
                "unit",
                initial=initial,
            )
        assert observed[0]["ready_phase"] == phase

    def test_startup_state_cannot_be_reused_for_a_different_bundle(self):
        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        with _v2_verifier._profile_scope(V2_OLD_PIN):
            state = _v2_verifier.EvidenceState()
        with _v2_verifier._profile_scope(V2_NEW_PIN):
            with pytest.raises(
                _v2_verifier.VerificationError, match="fence_profile_changed"
            ):
                state.check_ready(b"{}")

    @pytest.mark.parametrize("mode", [292, 436, 2468])
    def test_committed_ready_marker_requires_its_exact_safe_mode(
        self, tmp_path, monkeypatch, mode
    ):
        import stat
        from types import SimpleNamespace

        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        marker = tmp_path / "ready.json"
        marker.write_bytes(b"{}")
        monkeypatch.setattr(
            _v2_verifier.os,
            "fstat",
            lambda _fd: SimpleNamespace(
                st_mode=stat.S_IFREG | mode, st_uid=0, st_size=2
            ),
        )
        with pytest.raises(
            _v2_verifier.VerificationError, match="untrusted_fence_file"
        ):
            _v2_verifier.read_protected(marker, 65536, expected_mode=420)

    def test_committed_ready_marker_is_bounded_to_64_kib(self, tmp_path, monkeypatch):
        import stat
        from types import SimpleNamespace

        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        marker = tmp_path / "ready.json"
        marker.write_bytes(b" " * 65537)
        monkeypatch.setattr(
            _v2_verifier.os,
            "fstat",
            lambda _fd: SimpleNamespace(
                st_mode=stat.S_IFREG | 420, st_uid=0, st_size=65537
            ),
        )
        with pytest.raises(
            _v2_verifier.VerificationError, match="fence_file_too_large"
        ):
            _v2_verifier.read_protected(marker, 65536, expected_mode=420)

    def test_ready_mode_is_bound_to_the_live_report(self):
        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        report = _v2_report_snapshot()
        ready = _v2_startup_ready(report)
        ready["mode"] = "audit"
        with pytest.raises(
            _v2_verifier.VerificationError, match="fence_startup_identity_changed"
        ):
            _v2_verifier.assert_startup_binding(ready, report)

    @pytest.mark.parametrize("selected", [V2_OLD_PIN, V2_NEW_PIN])
    def test_protected_runtime_never_autoselects_or_accepts_a_mixed_bundle(
        self, monkeypatch, tmp_path, selected
    ):
        import hashlib
        from types import MappingProxyType

        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        names = (
            "lib.cts",
            "main.cts",
            "log.cts",
            "post.cts",
            "bundle-manifest.json",
            "bin/fence",
        )
        profiles = {}
        contents = {}
        for pin, version in ((V2_OLD_PIN, "old"), (V2_NEW_PIN, "new")):
            contents[pin] = {name: (version + ":" + name).encode() for name in names}
            profiles[pin] = {
                "pin": pin,
                "hashes": {
                    name: hashlib.sha256(data).hexdigest()
                    for name, data in contents[pin].items()
                },
            }
        monkeypatch.setattr(_v2_verifier, "_PROFILES", MappingProxyType(profiles))
        active = dict(contents[selected])
        monkeypatch.setattr(
            _v2_verifier,
            "read_protected",
            lambda path: active[path.relative_to(tmp_path).as_posix()],
        )
        other = V2_NEW_PIN if selected == V2_OLD_PIN else V2_OLD_PIN
        with _v2_verifier._profile_scope(selected):
            assert _v2_verifier.action_files(tmp_path) == contents[selected]
            active.update(contents[other])
            with pytest.raises(
                _v2_verifier.VerificationError,
                match="fence_action_identity_changed:lib.cts",
            ):
                _v2_verifier.action_files(tmp_path)
            active.update(contents[selected])
            active["bin/fence"] = contents[other]["bin/fence"]
            with pytest.raises(
                _v2_verifier.VerificationError,
                match="fence_action_identity_changed:bin/fence",
            ):
                _v2_verifier.action_files(tmp_path)

    def test_dormant_source_loads_from_an_authenticated_buffer_without_candidate_imports(
        self,
    ):
        import types
        from pathlib import Path

        source = Path(__file__).parents[1] / "ci_fence_fixture_verifier_v2.py"
        module = types.ModuleType("reviewed_dormant_verifier")
        module.__file__ = str(source)
        exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
        with module._profile_scope(V2_NEW_PIN):
            assert module.current_profile()["pin"] == V2_NEW_PIN
        with pytest.raises(
            module.VerificationError, match="reviewed_fence_profile_required"
        ):
            module.current_profile()

    def test_profile_drift_is_refused_before_reading_service_or_runtime(
        self, monkeypatch
    ):
        from pathlib import Path

        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        with _v2_verifier._profile_scope(V2_OLD_PIN):
            state = _v2_verifier.EvidenceState()

        def forbidden(*_args, **_kwargs):
            raise AssertionError("profile drift reached service or file access")

        monkeypatch.setattr(_v2_verifier.subprocess, "run", forbidden)
        monkeypatch.setattr(_v2_verifier, "_documents", forbidden)
        with _v2_verifier._profile_scope(V2_NEW_PIN):
            with pytest.raises(
                _v2_verifier.VerificationError, match="fence_profile_changed"
            ):
                _v2_verifier.validated_documents(
                    Path("/node"),
                    Path("/action"),
                    Path("/run/fence/fixed"),
                    "fixed",
                    state,
                )

    def test_old_native_readiness_also_requires_explicit_mode_binding(
        self, monkeypatch
    ):
        from pathlib import Path
        from types import SimpleNamespace

        _v2_verifier = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        report = _v2_report_snapshot()
        ready = _v2_startup_ready(report)
        ready["mode"] = "audit"
        monkeypatch.setattr(_v2_verifier, "require_node24", lambda _node: None)
        monkeypatch.setattr(_v2_verifier, "action_files", lambda _root: {})
        monkeypatch.setattr(
            _v2_verifier,
            "_node",
            lambda *_args, **_kw: SimpleNamespace(returncode=0, stdout=b"validated"),
        )
        with _v2_verifier._profile_scope(V2_OLD_PIN):
            with pytest.raises(
                _v2_verifier.VerificationError, match="fence_resident_inconsistent"
            ):
                _v2_verifier.validate_upstream(
                    Path("/node"), Path("/action"), report, ready, {}, "unit"
                )

    def test_dormant_inventory_matches_the_two_compiled_reviewed_bundles(self):
        module = importlib.import_module("scripts.ci_fence_fixture_verifier_v2")
        root = Path(__file__).parents[2]
        inventory = json.loads((root / "tooling/ci-peer/inputs-v2.json").read_text())
        assert inventory["status"] == "dormant_not_activated"
        assert (
            inventory["selection"]
            == "explicit_reviewed_caller_complete_action_pin_only"
        )
        profiles = {
            item["fence_action"].removeprefix("openai/fence@"): item
            for item in inventory["fence_profiles"]
        }
        assert set(profiles) == {V2_OLD_PIN, V2_NEW_PIN}
        for pin, recorded in profiles.items():
            reviewed = module.reviewed_profile(pin)
            assert recorded["fence_version"] == reviewed["version"]
            assert recorded["source_commit"] == reviewed["source_commit"]
            assert recorded["action_sha256"] == reviewed["hashes"]
