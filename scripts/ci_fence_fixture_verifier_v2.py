"""Reads protected Fence evidence for an explicitly selected immutable released bundle.
This adapter preserves controlled peer denial checks and cannot install, activate, or authorize Fence.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from types import MappingProxyType

_PROFILES = MappingProxyType(
    {
        "abc5790eac3445fe3854eea6b506aa7d31bed67c": MappingProxyType(
            {
                "pin": "abc5790eac3445fe3854eea6b506aa7d31bed67c",
                "version": "v0.10.5",
                "source_commit": "567ebd3787467b0ea8eaddc00f5ad9247ace585b",
                "historical_ready": False,
                "hashes": MappingProxyType(
                    {
                        "lib.cts": "d53ff329ebf8a9521dd8e60cbd88f19c41843acea915e59ffcc90de84086f818",
                        "main.cts": "5491b1e11675d68f5cf0b91c9cc10a1801b39f0d99ee78ba478c22db07c48d03",
                        "log.cts": "71e6224a4f9a2ecb4836a16680373ffb368857f89b2d638bcb98e5c993cbf631",
                        "post.cts": "c6626a6e0141839d37d51afd06897cab77eb0818f89e891ee8481249290e7fe4",
                        "bundle-manifest.json": "bf615b288ea0945943ab5767a13fd594f36e016c9ffac1538b24de46eabc7c41",
                        "bin/fence": "94892cb42a43d06a0998f0d6255280911f82fa322928ecb699104a7ec3a4f5d5",
                    }
                ),
            }
        ),
        "f9751ca5d767dc0199e88733c2b286bd4800a008": MappingProxyType(
            {
                "pin": "f9751ca5d767dc0199e88733c2b286bd4800a008",
                "version": "v0.12.5",
                "source_commit": "424c1ac6cc2f844986f38defc6d43150796f35f5",
                "historical_ready": True,
                "hashes": MappingProxyType(
                    {
                        "lib.cts": "27d1c3e33629a42bd0cf5daa6df9d2204899e50550c36b3b78fba6056bab7635",
                        "main.cts": "49a3bd0454d30c3cd29b880104ee4b047f6755341de89b25c4923b33e46d2791",
                        "log.cts": "b2b5c364e23671db2c27d60d6c107fe338cbeb7f80788092ef9ac0380dd16947",
                        "post.cts": "e9c0ed0860922861b494cfaaf1ecd56b9203195d1e0844358070b5f8a7e2f772",
                        "bundle-manifest.json": "c7543cb874bb7ee52497ce00724e926a3b3d4dc0fdae2f50b2404a6eca6fa005",
                        "bin/fence": "3434ef8eb32d130daf272c4794772b497a5d4390d458fd0ef73bac6612b6aa1a",
                    }
                ),
            }
        ),
    }
)
_ACTIVE_PROFILE = ContextVar("reviewed_fence_bundle", default=None)

ENV = {"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "LANG": "C", "LC_ALL": "C"}
MAX_FILE = 64 * 1024 * 1024
MAX_SAFE_INTEGER = (1 << 53) - 1
NATIVE_REASONS = frozenset(
    (
        "Fence DNS evidence contains an unexpected GitHub artifact compatibility policy",
        "Fence DNS evidence contains invalid hostname observation",
        "Fence DNS evidence contains invalid rejection reasons",
        "Fence DNS evidence contains invalid results-storage authorization",
        "Fence DNS evidence contains invalid wildcard authorization",
        "Fence DNS evidence contains invalid wildcard policy",
        "Fence DNS evidence contains invalid wildcard transport policy",
        "Fence DNS evidence contains unsorted wildcard transport policy",
        "Fence DNS evidence does not contain bounded runner provenance",
        "Fence DNS evidence does not contain bounded wildcard policy",
        "Fence DNS evidence does not contain the reviewed proxy policy",
        "Fence DNS evidence does not declare the GitHub artifact compatibility policy",
        "Fence DNS evidence does not disclose the GitHub artifact data-egress policy",
        "Fence DNS evidence does not match the resident report",
        "Fence DNS evidence must be a JSON object",
        "Fence DNS evidence resident process does not match the report",
        "Fence DNS evidence worker set does not match its container routing",
        "Fence DNS firewall failure is missing its critical finding",
        "Fence DNS rejection reasons do not match the total",
        "Fence readiness contains an unexpected GitHub artifact compatibility policy",
        "Fence readiness does not declare the GitHub artifact compatibility policy",
        "Fence readiness does not disclose the GitHub artifact data-egress policy",
        "Fence readiness does not match the resident report",
        "Fence readiness identity does not match the resident report",
        "Fence readiness must be a JSON object",
        "Fence readiness resident process does not match the report",
        "Fence report cannot enable GitHub artifact compatibility in audit mode",
        "Fence report contains an unexpected GitHub artifact compatibility policy",
        "Fence report contains critical resident findings",
        "Fence report critical findings do not match resident health",
        "Fence report does not contain a recognized readiness status",
        "Fence report does not contain bounded critical findings",
        "Fence report does not contain resident health evidence",
        "Fence report does not contain verified network state",
        "Fence report does not declare the GitHub artifact compatibility policy",
        "Fence report does not disclose the GitHub artifact data-egress policy",
        "Fence report does not select the reviewed hosted-runner profile",
        "Fence report has an unexpected status",
        "Fence report mode and control status are inconsistent",
        "Fence report must be a JSON object",
        "Fence resident health evidence is invalid or unhealthy",
        "Fence resident health evidence is stale or has an invalid timestamp",
        "Fence resident service is not active with the expected main process",
        "Fence resident service status is incomplete",
        "Fence resident service status is malformed",
        "Fence resident service status is unavailable",
        "Fence resident worker health is invalid",
        "Fence resident worker health is missing",
        "Fence resident worker set does not match the reviewed runtime",
    )
)
NATIVE_STAGES = frozenset(
    ("load_action", "parse_input", "report", "startup_ready", "dns", "unit")
)
VALIDATE = (
    "const reasons = new Set("
    + json.dumps(sorted(NATIVE_REASONS))
    + ");\n"
    + """
const fs = require('node:fs');
const crypto = require('node:crypto');
let stage = 'load_action';
let input;
let post;
try {
    const lib = require(process.argv[1]);
    post = require(process.argv[2]);
    stage = 'parse_input';
    input = JSON.parse(fs.readFileSync(0, 'utf8'));
    stage = 'report';
    lib.validateReport(input.report, true);
    if (input.ready_phase !== null) {
        stage = 'startup_ready';
        lib.validateReady(input.ready, input.report, input.ready_phase);
    }
    stage = 'dns';
    lib.validateDnsEvidence(input.dns, input.report);
    stage = 'unit';
    lib.validateResidentUnitStatus(input.unit, input.report.resident_health.resident_pid);
    process.stdout.write('validated');
} catch (error) {
    const message = error instanceof Error ? error.message : '';
    const diagnostic = {stage, reason: reasons.has(message) ? message : 'unrecognized_native_error',
                        native_code: null, message_sha256: crypto.createHash('sha256').update(message).digest('hex'),
                        message_bytes: Buffer.byteLength(message), projections: []};
    if (post && input && typeof input === 'object') {
        for (const [document, value, source] of [['report', input.report, 'resident'],
                ['startup_ready', input.ready, 'resident'], ['dns', input.dns, 'dns']]) {
            try {
                const projected = post.postFailureDiagnostic(value, source);
                const health = JSON.parse(projected.slice('Fence failure diagnostic (unverified): '.length));
                diagnostic.projections.push({document, health});
            } catch {}
        }
    }
    process.stdout.write(JSON.stringify(diagnostic));
    process.exitCode = 1;
}
"""
)


class VerificationError(RuntimeError):
    pass


class RejectionPending(VerificationError):
    pass


def reviewed_profile(action_pin):
    if not isinstance(action_pin, str) or action_pin not in _PROFILES:
        raise VerificationError("unreviewed_fence_action")
    return _PROFILES[action_pin]


def current_profile():
    action_pin = _ACTIVE_PROFILE.get()
    if action_pin is None:
        raise VerificationError("reviewed_fence_profile_required")
    return reviewed_profile(action_pin)


@contextmanager
def _profile_scope(action_pin):
    profile = reviewed_profile(action_pin)
    active = _ACTIVE_PROFILE.get()
    if active is not None and active != action_pin:
        raise VerificationError("fence_profile_changed")
    token = _ACTIVE_PROFILE.set(action_pin)
    try:
        yield profile
    finally:
        _ACTIVE_PROFILE.reset(token)


def counter_values(report):
    counters = report.get("counters")
    if not isinstance(counters, dict) or any(
        type(counters.get(name)) is not int
        or not 0 <= counters[name] <= MAX_SAFE_INTEGER
        for name in ("total_violations", "sampled_violations")
    ):
        raise VerificationError("fence_counters_invalid")
    if (
        report.get("mode") != "block"
        or re.fullmatch("[0-9a-f]{64}", str(report.get("ruleset_hash", ""))) is None
    ):
        raise VerificationError("fence_counter_epoch_invalid")
    return counters["total_violations"], counters["sampled_violations"]


def assert_counter_transition(before, after):
    first_total, first_sampled = counter_values(before)
    last_total, last_sampled = counter_values(after)
    if last_sampled < first_sampled:
        raise VerificationError("fence_sampled_counter_regressed")
    if last_total < first_total and after["ruleset_hash"] == before["ruleset_hash"]:
        raise VerificationError("fence_total_counter_regressed")


def assert_startup_binding(ready, report):
    fields = (
        "mode",
        "runtime_evidence_schema_version",
        "platform_profile_id",
        "profile_realization_id",
        "allow_github_artifacts",
        "policy_hash_schema_version",
        "policy_hash",
        "base_ruleset_hash",
        "protection_available",
    )
    if any(
        name not in ready or name not in report or ready[name] != report[name]
        for name in fields
    ) or ready.get("status") != report.get("readiness_status"):
        raise VerificationError("fence_startup_identity_changed")
    startup = ready["resident_health"]
    current = report["resident_health"]
    if startup["resident_pid"] != current["resident_pid"] or sorted(
        worker["name"] for worker in startup["workers"]
    ) != sorted(worker["name"] for worker in current["workers"]):
        raise VerificationError("fence_startup_resident_changed")


class EvidenceState:
    def __init__(self):
        self.action_pin = current_profile()["pin"]
        self.ready_bytes = None
        self.previous = None

    def assert_profile(self):
        if current_profile()["pin"] != self.action_pin:
            raise VerificationError("fence_profile_changed")

    def check_ready(self, raw_ready):
        self.assert_profile()
        if self.ready_bytes is not None and raw_ready != self.ready_bytes:
            raise VerificationError("fence_startup_readiness_changed")

    def accept(self, report, ready, raw_ready):
        self.check_ready(raw_ready)
        assert_startup_binding(ready, report)
        counter_values(report)
        if self.previous is not None:
            assert_counter_transition(self.previous, report)
        self.ready_bytes = raw_ready
        self.previous = report


def _peer_module():
    path = Path(__file__).with_name("ci_network_fixture.py")
    specification = importlib.util.spec_from_file_location("ci_peer_fixture", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def assert_report(report, ready):
    expected = {
        "mode": "block",
        "status": "protected_host_block",
        "readiness_status": "ready",
        "protection_available": True,
        "network_verification_status": "verified",
        "sudo_status": "disabled_verified",
        "container_status": "disabled_verified",
    }
    if not isinstance(report, dict) or any(
        report.get(key) != value for key, value in expected.items()
    ):
        raise VerificationError("fence_not_protected")
    health = report.get("resident_health", {})
    if (
        report.get("critical_findings") != []
        or health.get("status") != "healthy"
        or type(health.get("resident_pid")) is not int
        or health["resident_pid"] < 1
        or not isinstance(ready, dict)
        or ready.get("status") != "ready"
        or ready.get("mode") != report.get("mode")
        or ready.get("resident_health", {}).get("resident_pid")
        != health["resident_pid"]
    ):
        raise VerificationError("fence_resident_inconsistent")


def assert_rejections(report):
    findings = report.get("findings")
    if not isinstance(findings, list) or len(findings) > 4096:
        raise VerificationError("fence_findings_invalid")
    for protocol, port in (("tcp", 19443), ("tcp", 443), ("udp", 53)):
        if not any(
            isinstance(row, dict)
            and row.get("classification") == "rejected"
            and row.get("remote_address") == "192.0.2.2"
            and row.get("protocol") == protocol
            and row.get("remote_port") == port
            for row in findings
        ):
            raise VerificationError(f"fence_rejection_missing:{protocol}:{port}")


def matching_rejection(report, protocol, port):
    rows = report.get("findings")
    if not isinstance(rows, list) or len(rows) > 4096:
        raise VerificationError("fence_findings_invalid")
    return any(
        isinstance(row, dict)
        and row.get("classification") == "rejected"
        and row.get("remote_address") == "192.0.2.2"
        and row.get("protocol") == protocol
        and row.get("remote_port") == port
        for row in rows
    )


def assert_new_rejection(before, after, protocol, port):
    counter_values(before)
    counter_values(after)
    if matching_rejection(before, protocol, port):
        raise VerificationError("fence_rejection_preexisting")
    if not matching_rejection(after, protocol, port):
        raise RejectionPending(f"fence_rejection_missing:{protocol}:{port}")
    if (
        after["counters"]["sampled_violations"]
        <= before["counters"]["sampled_violations"]
    ):
        raise VerificationError("fence_rejection_counter_missing")


def assert_dns_refused(query, response):
    if len(query) < 12 or len(response) < 12:
        raise VerificationError("dns_refusal_invalid")
    identifier, flags, questions, answers, authority, additional = struct.unpack(
        "!HHHHHH", response[:12]
    )
    if (
        response[:2] != query[:2]
        or flags & 0x8000 == 0
        or flags & 0xF != 5
        or (questions, answers, authority, additional) != (1, 0, 0, 0)
        or response[12:] != query[12:]
        or identifier != int.from_bytes(query[:2], "big")
    ):
        raise VerificationError("dns_refusal_invalid")


def read_protected(path, limit=MAX_FILE, *, expected_mode=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
            or expected_mode is not None
            and stat.S_IMODE(metadata.st_mode) != expected_mode
        ):
            raise VerificationError("untrusted_fence_file")
        if metadata.st_size > limit:
            raise VerificationError("fence_file_too_large")
        value = bytearray()
        while len(value) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(value)))
            if not chunk:
                return bytes(value)
            value.extend(chunk)
        raise VerificationError("fence_file_too_large")
    finally:
        os.close(descriptor)


def action_files(root):
    files = {}
    for name, expected in current_profile()["hashes"].items():
        data = read_protected(root / name)
        if hashlib.sha256(data).hexdigest() != expected:
            raise VerificationError("fence_action_identity_changed:" + name)
        files[name] = data
    return files


def _node(node, *arguments, payload=None):
    result = subprocess.run(
        [str(node), *arguments],
        input=payload,
        env=ENV,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if len(result.stdout) + len(result.stderr) > 65536:
        raise VerificationError("fence_node_output_unbounded")
    return result


def require_node24(node):
    result = _node(node, "--version")
    if (
        result.returncode != 0
        or re.fullmatch(rb"v24\.[0-9]+\.[0-9]+\s*", result.stdout) is None
    ):
        raise VerificationError("fence_requires_actual_node24")


def runner_node24():
    parent = os.getppid()
    for _depth in range(16):
        executable = Path("/proc") / str(parent) / "exe"
        resolved = executable.resolve(strict=True)
        if resolved.name == "Runner.Worker":
            node = resolved.parent.parent / "externals/node24/bin/node"
            require_node24(node)
            return node
        fields = (
            (Path("/proc") / str(parent) / "stat").read_text().rsplit(")", 1)[1].split()
        )
        parent = int(fields[1])
        if parent <= 1:
            break
    raise VerificationError("runner_node24_unavailable")


def safe_integer(value, minimum=0, maximum=MAX_SAFE_INTEGER):
    return value if type(value) is int and minimum <= value <= maximum else None


def native_diagnostic(result, payload):
    output = result.stdout
    try:
        value = json.loads(output)
    except (ValueError, UnicodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    stage = value.get("stage")
    reason = value.get("reason")
    message_digest = value.get("message_sha256")
    stderr = getattr(result, "stderr", b"")
    diagnostic = {
        "event": "upstream_fence_validation_failed",
        "stage": stage
        if isinstance(stage, str) and stage in NATIVE_STAGES
        else "unknown",
        "reason": reason
        if isinstance(reason, str) and reason in NATIVE_REASONS
        else "unrecognized_native_error",
        "native_code": None,
        "message_sha256": message_digest
        if isinstance(message_digest, str)
        and re.fullmatch("[0-9a-f]{64}", message_digest)
        else None,
        "message_bytes": safe_integer(value.get("message_bytes")),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
        "native_output_sha256": hashlib.sha256(output).hexdigest(),
        "native_output_bytes": len(output),
        "native_stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "native_stderr_bytes": len(stderr),
        "unverified_health": [],
    }
    projections = value.get("projections")
    if isinstance(projections, list):
        for row in projections[:3]:
            if not isinstance(row, dict) or row.get("document") not in (
                "report",
                "startup_ready",
                "dns",
            ):
                continue
            health = row.get("health")
            if not isinstance(health, dict):
                continue
            status = health.get("reported_resident_status")
            truncated = health.get("critical_findings_truncated")
            diagnostic["unverified_health"].append(
                {
                    "document": row["document"],
                    "reported_resident_status": status
                    if status in ("healthy", "critical", "starting")
                    else "unavailable",
                    "verification_sequence": safe_integer(
                        health.get("verification_sequence"), 1
                    ),
                    "heartbeat_age_ms": safe_integer(
                        health.get("heartbeat_age_ms"), -MAX_SAFE_INTEGER
                    ),
                    "critical_findings": safe_integer(
                        health.get("critical_findings"), maximum=64
                    ),
                    "critical_findings_truncated": truncated
                    if type(truncated) is bool
                    else None,
                }
            )
    return diagnostic


def validate_upstream(node, root, report, ready, dns, unit, *, initial=True):
    profile = current_profile()
    require_node24(node)
    action_files(root)
    phase = "setup" if initial else "post" if profile["historical_ready"] else None
    payload = json.dumps(
        {
            "report": report,
            "ready": ready,
            "dns": dns,
            "unit": unit,
            "ready_phase": phase,
        }
    ).encode()
    if len(payload) > 1024 * 1024:
        raise VerificationError("fence_evidence_too_large")
    result = _node(
        node,
        "-e",
        VALIDATE,
        str(root / "lib.cts"),
        str(root / "post.cts"),
        payload=payload,
    )
    if result.returncode != 0 or result.stdout != b"validated":
        print(
            json.dumps(native_diagnostic(result, payload), sort_keys=True),
            file=sys.stderr,
        )
        raise VerificationError("upstream_fence_validation_failed")
    assert_report(report, ready)


def assert_bundle_rejected(node, root):
    require_node24(node)
    files = action_files(root)
    with tempfile.TemporaryDirectory(prefix="ci-fence-integrity-") as temporary:
        copied = Path(temporary)
        (copied / "bin").mkdir()
        for name, data in files.items():
            (copied / name).write_bytes(data)
        binary = bytearray(files["bin/fence"])
        binary[-1] ^= 1
        (copied / "bin/fence").write_bytes(binary)
        result = _node(node, str(copied / "main.cts"))
        expected = b"bundled Fence binary checksum does not match its manifest"
        if result.returncode == 0 or expected not in result.stderr:
            raise VerificationError("fence_integrity_rejection_not_observed")


def _documents(directory):
    raw = [
        read_protected(directory / "report.json", 512 * 1024),
        read_protected(directory / "ready.json", 64 * 1024, expected_mode=0o644),
        read_protected(directory / "dns-report.json", 512 * 1024),
    ]
    return *(json.loads(value.decode("utf-8")) for value in raw), raw[1]


def validated_documents(node, root, directory, invocation, state=None):
    if state is None:
        state = EvidenceState()
    state.assert_profile()
    unit = subprocess.run(
        [
            "/usr/bin/systemctl",
            "show",
            "fence-" + invocation + ".service",
            "--no-pager",
            "--property=ActiveState",
            "--property=MainPID",
            "--property=SubState",
        ],
        env=ENV,
        capture_output=True,
        timeout=5,
        check=True,
    ).stdout.decode()
    report, ready, dns, raw_ready = _documents(directory)
    state.check_ready(raw_ready)
    validate_upstream(
        node, root, report, ready, dns, unit, initial=state.ready_bytes is None
    )
    state.accept(report, ready, raw_ready)
    return report, ready, dns


def verify(fixture_id, invocation, node, *, action_pin, peer=None):
    with _profile_scope(action_pin):
        return _verify(fixture_id, invocation, node, peer=peer)


def _verify(fixture_id, invocation, node, *, peer=None):
    if (
        not isinstance(invocation, str)
        or re.fullmatch("[a-z0-9][a-z0-9-]{0,63}", invocation) is None
    ):
        raise VerificationError("fence_invocation_invalid")
    if peer is None:
        peer = _peer_module()
    state = peer.read_owned(peer._directory(fixture_id) / "ready.json")
    before = peer.read_owned(peer._directory(fixture_id) / "baseline.json")
    peer.assert_baseline(before)
    directory = Path("/run/fence") / invocation
    root = Path("/run/fence-launcher") / invocation / "action"
    evidence = EvidenceState()
    report, ready, dns = validated_documents(
        node, root, directory, invocation, evidence
    )
    for protocol, port in (("tcp", 19443), ("tcp", 443), ("udp", 53)):
        if matching_rejection(report, protocol, port):
            raise VerificationError("fence_rejection_preexisting")
    previous = [flow["source_port"] for flow in before["flows"]]
    allowed = peer.probe_flow(state, "allowed", previous)
    peer.assert_flow_boundary(
        before["nonce"], previous, allowed, before["health"], before["health"]
    )
    for name, protocol, port in (
        ("excluded", "tcp", 19443),
        ("address", "tcp", 443),
        ("dns", "udp", 53),
    ):
        initial, _ready, _dns = validated_documents(
            node, root, directory, invocation, evidence
        )
        if matching_rejection(initial, protocol, port):
            raise VerificationError("fence_rejection_preexisting")
        health_before = peer._tls(state, "allowed", "/health")
        flow = peer.probe_flow(state, name, previous)
        health_after = peer._tls(state, "allowed", "/health")
        if not health_before["answered"] or not health_after["answered"]:
            raise VerificationError("fence_peer_health_unavailable")
        peer.assert_flow_boundary(
            before["nonce"],
            previous,
            flow,
            health_before["health"],
            health_after["health"],
        )
        deadline = time.monotonic() + 20
        while True:
            current, _ready, _dns = validated_documents(
                node, root, directory, invocation, evidence
            )
            try:
                assert_new_rejection(initial, current, protocol, port)
                break
            except RejectionPending:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
    question = b"\x0eoutside-policy\x05fence\x07invalid\0\0\1\0\1"
    query = struct.pack("!HHHHHH", 0x4242, 0x0100, 1, 0, 0, 0) + question
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(2)
        connection.connect(("127.0.0.1", 53))
        connection.send(query)
        assert_dns_refused(query, connection.recv(4096))
    assert_bundle_rejected(node, root)
    validated_documents(node, root, directory, invocation, evidence)
    return {
        "fixture": fixture_id,
        "invocation": invocation,
        "action": current_profile()["pin"],
        "network": "controlled_denials_verified",
        "integrity": "copied_agent_checksum_rejected",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--action-pin", required=True, choices=tuple(_PROFILES))
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--invocation", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = verify(
            arguments.fixture_id,
            arguments.invocation,
            runner_node24(),
            action_pin=arguments.action_pin,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        VerificationError,
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps({"error": type(exc).__name__, "code": str(exc)[:120]}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
