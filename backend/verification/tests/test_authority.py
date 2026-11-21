"""Tests for delegated-authority checks (backend/verification/checks/authority.py,
drivers/in_process.py, __main__.py): cross-user isolation, scope withholding,
delegation attribution, and admin-only parser approval over the protected tool
dispatch with no model, driven once module-scoped, plus the checks on synthetic
evidence.
"""

from __future__ import annotations

import tempfile
import uuid
from types import SimpleNamespace

import pytest

from verification.__main__ import exit_code_for, run_in_process
from verification.checks import authority as A
from verification.config import RunConfig
from verification.drivers.in_process import (
    InProcessDriver,
    _reads_markers,
    _refused,
    _response_message,
)
from verification.evidence import CapturedEvidence
from verification.verdict import Outcome, reconcile
from verification.tests.conftest import INTEGRATION, run_async


def _config():
    return RunConfig(
        mode="in_process",
        run_id=f"__verif__{uuid.uuid4().hex[:10]}",
        out_dir=tempfile.mkdtemp(prefix="verif_authz_"),
    )


@pytest.fixture(scope="module")
def probes():
    config = _config()

    async def _go():
        driver = InProcessDriver(config)
        await driver.setup()
        try:
            return {
                "xuser": await driver.probe_cross_user(config.run_id),
                "scope": await driver.probe_scope_withheld(config.run_id),
                "deleg": driver.probe_delegation(config.run_id),
                "appr": await driver.probe_admin_approval(config.run_id),
            }
        finally:
            await driver.teardown()

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ASTRAL_ENV", "development")
        patch.setenv("DELEGATION_REQUIRED", "false")
        patch.setenv("FF_TAINT_TRACKING", "false")
        patch.setenv("FF_POLICY_ENGINE", "false")
        patch.setenv("FF_ASI_COVERAGE", "false")
        return run_async(_go())


def _verdict(check, ev):
    res = check.run(ev, {})
    refuted = check.counter_refutes(ev, {}) if res.outcome == Outcome.PASS else False
    outcome, _conf, _adv = reconcile(res.outcome, refuted, None)
    return outcome, res.reason


def _evidence(**kwargs):
    extra = kwargs.pop("extra", {})
    return CapturedEvidence(evidence_id="e", scenario_id="s", run_mode="mock_inprocess",
                            extra=extra, **kwargs)


@pytest.mark.integration
class TestDriven:
    pytestmark = INTEGRATION

    def test_us2_cross_user_isolation(self, probes):
        for check in (A.CROSS_USER, A.CHAIN):
            outcome, reason = _verdict(check, probes["xuser"])
            assert outcome == Outcome.PASS, f"{check.check_id}: {reason}"
        extra = probes["xuser"].extra
        assert extra["owner_read_ok"] is True
        assert extra["refused"] is True
        assert extra["leaked_markers"] is False
        assert probes["xuser"].run_mode == "mock_inprocess"

    def test_us2_denials_audited(self, probes):
        outcome, reason = _verdict(A.DENIALS, probes["scope"])
        assert outcome == Outcome.PASS, f"denials_audited: {reason}"
        denied = [r for r in probes["scope"].audit_rows if r["action_type"].endswith(".denied")]
        assert [(r["outcome"], r["inputs_meta"]["gate"]) for r in denied] == [
            ("failure", "tool_permission"),
        ]

    def test_us2_scope_withheld(self, probes):
        outcome, reason = _verdict(A.SCOPE, probes["scope"])
        assert outcome == Outcome.PASS, f"scope_withheld: {reason}"
        extra = probes["scope"].extra
        assert extra["withheld"] is True
        assert extra["read_success"] is False
        assert extra["control_read_ok"] is True
        assert not any(r.get("outcome") == "success" for r in probes["scope"].audit_rows)

    def test_us2_delegation_attribution(self, probes):
        outcome, reason = _verdict(A.DELEGATION, probes["deleg"])
        assert outcome == Outcome.PASS, f"delegation_attribution: {reason}"

    def test_us2_admin_only_approval(self, probes):
        outcome, reason = _verdict(A.APPROVAL, probes["appr"])
        assert outcome == Outcome.PASS, f"admin_only_approval: {reason}"

    def test_run_in_process_records_every_check_without_failures(self):
        config = _config()
        record = run_async(run_in_process(config))
        checks = {v["refs"]["check"]: v["outcome"] for v in record["verdicts"]}
        assert set(checks) == {
            "us2.cross_user_refused", "us2.denials_audited", "us2.audit_chain_unbroken",
            "us2.scope_withheld", "us2.delegation_attribution", "us2.admin_only_approval",
            "us3.no_client_construction",
        }
        assert set(checks.values()) == {"pass"}
        assert exit_code_for(record, strict=True) == 0


def test_response_helpers_classify_refusals_and_reads():
    refused_by_gate = SimpleNamespace(result=None, error={"message": "restricted"})
    refused_by_tool = SimpleNamespace(result={"error": {"code": "not_found"}}, error=None)
    read = SimpleNamespace(result={"rows": [["Groceries"]]}, error=None)
    assert _refused(refused_by_gate) and _refused(refused_by_tool) and not _refused(read)
    assert _reads_markers(read, ["Groceries"]) is True
    assert _reads_markers(read, ["Other"]) is False
    assert _reads_markers(refused_by_tool, ["not_found"]) is False
    assert _response_message(refused_by_gate) == {
        "type": "tool_response", "result": None, "error": {"message": "restricted"},
    }


def test_cross_user_check_requires_owner_control_and_no_leak():
    good = _evidence(extra={"owner_read_ok": True, "refused": True, "leaked_markers": False})
    assert _verdict(A.CROSS_USER, good)[0] == Outcome.PASS
    no_control = _evidence(extra={"owner_read_ok": False, "refused": True, "leaked_markers": False})
    assert _verdict(A.CROSS_USER, no_control)[0] == Outcome.FAIL
    leaked = _evidence(extra={"owner_read_ok": True, "refused": False, "leaked_markers": True})
    assert _verdict(A.CROSS_USER, leaked)[0] == Outcome.FAIL
    assert A.CROSS_USER.counter(leaked, {}).outcome == Outcome.PASS
    assert A.CROSS_USER.counter(good, {}).outcome == Outcome.FAIL
    unrefused = _evidence(extra={"owner_read_ok": True, "refused": False, "leaked_markers": False})
    assert _verdict(A.CROSS_USER, unrefused)[0] == Outcome.FAIL


def test_denials_check_requires_a_tool_permission_denial_row():
    row = {"action_type": "tool.read_spreadsheet.denied", "outcome": "failure",
           "event_class": "agent_tool_call", "inputs_meta": {"gate": "tool_permission"}}
    assert _verdict(A.DENIALS, _evidence(audit_rows=[row]))[0] == Outcome.PASS
    other_gate = {**row, "inputs_meta": {"gate": "agent_identity"}}
    assert _verdict(A.DENIALS, _evidence(audit_rows=[other_gate]))[0] == Outcome.FAIL
    succeeded = {**row, "outcome": "success"}
    assert _verdict(A.DENIALS, _evidence(audit_rows=[succeeded]))[0] == Outcome.FAIL
    unrecorded = {"action_type": "tool.read_spreadsheet.end", "outcome": "success"}
    assert _verdict(A.DENIALS, _evidence(audit_rows=[unrecorded]))[0] == Outcome.FAIL
    assert _verdict(A.DENIALS, _evidence())[0] == Outcome.FAIL


def test_scope_check_requires_withholding_and_a_granted_control_read():
    good = _evidence(extra={"withheld": True, "read_success": False, "control_read_ok": True})
    assert _verdict(A.SCOPE, good)[0] == Outcome.PASS
    no_control = _evidence(extra={"withheld": True, "read_success": False, "control_read_ok": False})
    assert _verdict(A.SCOPE, no_control)[0] == Outcome.FAIL
    ran = _evidence(extra={"withheld": False, "read_success": True, "control_read_ok": True})
    assert _verdict(A.SCOPE, ran)[0] == Outcome.FAIL
    assert A.SCOPE.counter(ran, {}).outcome == Outcome.PASS
