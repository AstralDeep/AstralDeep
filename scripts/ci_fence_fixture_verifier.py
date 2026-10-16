"""Reads protected Fence evidence and authenticates controlled CI peer denials.
It reuses the exact pinned action's validators and checksum rejection without installing,
mutating or approving Fence; activation remains a separately qualified workflow change.
"""

from __future__ import annotations

import argparse
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

ACTION_PIN = "abc5790eac3445fe3854eea6b506aa7d31bed67c"
ACTION_HASHES = {
    "lib.cts": "d53ff329ebf8a9521dd8e60cbd88f19c41843acea915e59ffcc90de84086f818",
    "main.cts": "5491b1e11675d68f5cf0b91c9cc10a1801b39f0d99ee78ba478c22db07c48d03",
    "log.cts": "71e6224a4f9a2ecb4836a16680373ffb368857f89b2d638bcb98e5c993cbf631",
    "post.cts": "c6626a6e0141839d37d51afd06897cab77eb0818f89e891ee8481249290e7fe4",
    "bundle-manifest.json": "bf615b288ea0945943ab5767a13fd594f36e016c9ffac1538b24de46eabc7c41",
    "bin/fence": "94892cb42a43d06a0998f0d6255280911f82fa322928ecb699104a7ec3a4f5d5",
}
ENV = {"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "LANG": "C", "LC_ALL": "C"}
MAX_FILE = 64 * 1024 * 1024
VALIDATE = """
const fs = require('node:fs');
const lib = require(process.argv[1]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
lib.validateReport(input.report, true);
lib.validateReady(input.ready, input.report);
lib.validateDnsEvidence(input.dns, input.report);
lib.validateResidentUnitStatus(input.unit, input.report.resident_health.resident_pid);
process.stdout.write('validated');
"""


class VerificationError(RuntimeError):
    pass


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
    if matching_rejection(before, protocol, port):
        raise VerificationError("fence_rejection_preexisting")
    if not matching_rejection(after, protocol, port):
        raise VerificationError(f"fence_rejection_missing:{protocol}:{port}")
    first = before.get("counters", {}).get("total_violations")
    last = after.get("counters", {}).get("total_violations")
    if type(first) is not int or type(last) is not int or first < 0 or last <= first:
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


def read_protected(path, limit=MAX_FILE):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
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
    for name, expected in ACTION_HASHES.items():
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


def validate_upstream(node, root, report, ready, dns, unit):
    require_node24(node)
    action_files(root)
    payload = json.dumps(
        {"report": report, "ready": ready, "dns": dns, "unit": unit}
    ).encode()
    if len(payload) > 1024 * 1024:
        raise VerificationError("fence_evidence_too_large")
    result = _node(node, "-e", VALIDATE, str(root / "lib.cts"), payload=payload)
    if result.returncode != 0 or result.stdout != b"validated":
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
    return [
        json.loads(read_protected(directory / name, 512 * 1024))
        for name in ("report.json", "ready.json", "dns-report.json")
    ]


def validated_documents(node, root, directory, invocation):
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
    report, ready, dns = _documents(directory)
    validate_upstream(node, root, report, ready, dns, unit)
    return report, ready, dns


def verify(fixture_id, invocation, node, *, peer=None):
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
    report, ready, dns = validated_documents(node, root, directory, invocation)
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
        initial, _ready, _dns = validated_documents(node, root, directory, invocation)
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
                node, root, directory, invocation
            )
            try:
                assert_new_rejection(initial, current, protocol, port)
                break
            except VerificationError:
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
    validated_documents(node, root, directory, invocation)
    return {
        "fixture": fixture_id,
        "invocation": invocation,
        "action": ACTION_PIN,
        "network": "controlled_denials_verified",
        "integrity": "copied_agent_checksum_rejected",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--invocation", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = verify(arguments.fixture_id, arguments.invocation, runner_node24())
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
