"""Runs the whole conformance suite under sandbox-exec on this Mac and requires every case to pass, then weakens the rendered profile one rule at a time and requires the matching cases to fail.
The weakening patches the renderer inside the test process only, so nothing reachable from run() or the CLI can loosen a profile.
"""

from __future__ import annotations

import os
import sys

import pytest
from astral_exec import seatbelt
from astral_exec.constants import SANDBOX_EXEC
from astral_exec.conformance import FAIL, PASS, run_suite

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC),
    reason="Seatbelt runs need macOS with /usr/bin/sandbox-exec",
)

WEAKENINGS = {
    "reads": (
        '(allow file-read* (subpath "/"))\n',
        [
            "fs.read_outside_policy_denied",
            "fs.read_node_data_dir_denied",
            "fs.read_home_denied",
            "fs.list_outside_denied",
            "fs.keychain_denied",
        ],
    ),
    "writes": (
        '(allow file-write* (subpath "/"))\n',
        [
            "fs.write_outside_workspace_denied",
            "fs.write_read_root_denied",
            "fs.write_home_startup_denied",
            "fs.symlink_escape_denied",
            "fs.rename_escape_denied",
        ],
    ),
    "network": (
        "(allow network*)\n",
        [
            "net.public_address_denied",
            "net.loopback_ports_denied",
            "net.udp_denied",
            "net.unix_socket_denied",
            "net.listen_denied",
            "proxy.other_sandbox_ports_unreachable",
        ],
    ),
    "executables": (
        '(allow process-exec* (subpath "{workspace}"))\n(allow file-map-executable (subpath "{workspace}"))\n',
        [
            "fs.workspace_map_executable_denied",
            "fs.workspace_exec_script_denied",
            "fs.workspace_exec_binary_denied",
        ],
    ),
    "fork": ("(allow process-fork)\n", ["proc.single_process_by_default"]),
    "mach": (
        '(allow mach-lookup (global-name "com.apple.dnssd.service" "com.apple.trustd" "com.apple.logd"))\n',
        ["net.dns_service_denied"],
    ),
}


def test_the_suite_passes_under_seatbelt(tmp_path):
    results = run_suite("seatbelt", workdir=str(tmp_path.resolve()))
    report = "\n".join(f"{r.result} {r.case} {r.detail}" for r in results)
    assert results and {r.result for r in results} == {PASS}, report
    names = {r.case for r in results}
    assert {
        "fs.keychain_denied",
        "net.mdns_responder_denied",
        "limit.memory",
        "refusal.app_sandboxed",
    } <= names


@pytest.mark.parametrize("name", sorted(WEAKENINGS))
def test_a_weakened_profile_fails_the_matching_cases(name, monkeypatch, tmp_path):
    addition, expected = WEAKENINGS[name]
    real = seatbelt.render

    def weakened(policy, context):
        return real(policy, context) + addition.format(
            workspace=policy.document["workspace"]
        )

    monkeypatch.setattr(seatbelt, "render", weakened)
    results = run_suite("seatbelt", workdir=str(tmp_path.resolve()), only=expected)
    outcome = {r.case: r.result for r in results}
    assert outcome == {case: FAIL for case in expected}, results
