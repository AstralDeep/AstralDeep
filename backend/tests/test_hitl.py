"""Tests tool risk classification, its feature flag, and repeated denial escalation in orchestrator.hitl."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import hitl  # noqa: E402
from orchestrator.hitl import (  # noqa: E402
    CROSS_PRINCIPAL,
    EGRESS,
    IRREVERSIBLE,
    UNTRUSTED_TAINTED,
    assess_risk,
    escalation_needed,
    is_high_risk,
    requires_confirmation,
)


def test_hitl_default_off(monkeypatch):
    monkeypatch.delenv("FF_HITL_HIGHRISK", raising=False)
    assert hitl.hitl_enabled() is False


@pytest.mark.parametrize("v", ["true", "1", "yes", "on", "  ON  ", "True"])
def test_hitl_on_values(monkeypatch, v):
    monkeypatch.setenv("FF_HITL_HIGHRISK", v)
    assert hitl.hitl_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "no", "off", "maybe", ""])
def test_hitl_off_values(monkeypatch, v):
    monkeypatch.setenv("FF_HITL_HIGHRISK", v)
    assert hitl.hitl_enabled() is False


@pytest.mark.parametrize("tool", [
    "send_email", "send_message", "fetch_page", "http_get", "http_post", "webhook",
])
def test_assess_egress_by_name(tool):
    assert assess_risk(tool) == [EGRESS]


@pytest.mark.parametrize("tool", [
    "http_delete", "send_sms", "post_tweet", "fetch_url", "upload_file",
])
def test_assess_egress_by_prefix(tool):
    assert EGRESS in assess_risk(tool)


@pytest.mark.parametrize("tool", [
    "delete_user", "drop_table", "wipe_disk", "purge_cache",
    "transfer_funds", "pay_invoice", "deploy_release",
])
def test_assess_irreversible_by_prefix(tool):
    assert IRREVERSIBLE in assess_risk(tool)


def test_assess_cross_principal_when_both_set_and_differ():
    risks = assess_risk("read_data", actor_principal="alice", target_principal="bob")
    assert risks == [CROSS_PRINCIPAL]


def test_assess_no_cross_principal_when_same():
    risks = assess_risk("read_data", actor_principal="alice", target_principal="alice")
    assert risks == []


@pytest.mark.parametrize("actor,target", [
    ("alice", None), (None, "bob"), (None, None),
])
def test_assess_no_cross_principal_when_either_missing(actor, target):
    risks = assess_risk("read_data", actor_principal=actor, target_principal=target)
    assert CROSS_PRINCIPAL not in risks


def test_assess_untrusted_tainted():
    assert assess_risk("read_data", trust="untrusted") == [UNTRUSTED_TAINTED]


def test_assess_trusted_default_is_clean():
    assert assess_risk("read_data") == []


def test_assess_combo_is_sorted():
    risks = assess_risk(
        "send_message",
        actor_principal="alice",
        target_principal="bob",
        trust="untrusted",
    )
    assert risks == sorted([EGRESS, CROSS_PRINCIPAL, UNTRUSTED_TAINTED])
    assert risks == [CROSS_PRINCIPAL, EGRESS, UNTRUSTED_TAINTED]


def test_assess_transfer_is_irreversible_only():
    risks = assess_risk("transfer_funds")
    assert risks == [IRREVERSIBLE]


def test_assess_is_deterministic():
    a = assess_risk("send_email", actor_principal="x", target_principal="y", trust="untrusted")
    b = assess_risk("send_email", actor_principal="x", target_principal="y", trust="untrusted")
    assert a == b


def test_requires_confirmation_empty():
    assert requires_confirmation([]) is False


def test_requires_confirmation_nonempty():
    assert requires_confirmation([EGRESS]) is True


def test_is_high_risk_irreversible_alone():
    assert is_high_risk([IRREVERSIBLE]) is True


def test_is_high_risk_cross_principal_alone():
    assert is_high_risk([CROSS_PRINCIPAL]) is True


def test_is_high_risk_single_egress_is_not_high():
    assert is_high_risk([EGRESS]) is False


def test_is_high_risk_single_untrusted_is_not_high():
    assert is_high_risk([UNTRUSTED_TAINTED]) is False


def test_is_high_risk_two_risks_is_compounded():
    assert is_high_risk([EGRESS, UNTRUSTED_TAINTED]) is True


def test_is_high_risk_empty_is_false():
    assert is_high_risk([]) is False


def test_escalation_high_risk_at_threshold():
    assert escalation_needed([IRREVERSIBLE], denied_attempts=2, denial_threshold=2) is True


def test_escalation_high_risk_above_threshold():
    assert escalation_needed([IRREVERSIBLE], denied_attempts=5) is True


def test_escalation_high_risk_below_threshold():
    assert escalation_needed([IRREVERSIBLE], denied_attempts=1, denial_threshold=2) is False


def test_escalation_low_risk_never_escalates():
    assert escalation_needed([EGRESS], denied_attempts=10) is False


def test_escalation_no_risk_never_escalates():
    assert escalation_needed([], denied_attempts=10) is False


def test_escalation_compounded_risk_escalates():
    assert escalation_needed([EGRESS, UNTRUSTED_TAINTED], denied_attempts=2) is True
