"""Delegated-authority conformance checks (backend/verification/checks/base.py,
evidence.py): cross-user isolation, audit-chain integrity, scope withholding,
delegation attribution, and admin-only approval, each with an adversarial counter.
"""

from __future__ import annotations

from typing import Any, Dict

from verification.checks.base import Check, CheckResult, no, ok, register, unsure
from verification.evidence import CapturedEvidence

_DENY_ACTION = "attachment_reference_denied"


def _xuser_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    x = ev.extra or {}
    leaked = bool(x.get("leaked_markers"))
    ws_size = int(x.get("attacker_workspace_size", 0) or 0)
    if not leaked and ws_size == 0:
        return ok("us2.cross_user_refused",
                  "foreign attachment yielded no data to the other user (no leakage)")
    return no(
        "us2.cross_user_refused",
        f"cross-user isolation breach (leaked={leaked}, attacker_workspace={ws_size})",
    )


def _xuser_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    x = ev.extra or {}
    if x.get("leaked_markers") or int(x.get("attacker_workspace_size", 0) or 0) > 0:
        return ok("us2.cross_user_refused.counter", "evidence of leakage found")
    return no("us2.cross_user_refused.counter", "no leakage found")


def _denials_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    rows = [r for r in ev.audit_rows if r.get("action_type") == _DENY_ACTION]
    if rows and all(r.get("outcome") == "failure" for r in rows):
        return ok("us2.denials_audited", f"{len(rows)} denial(s) recorded as failures",
                  event_class=sorted({r.get("event_class") for r in rows}))
    if not (ev.extra or {}).get("leaked_markers"):
        return unsure(
            "us2.denials_audited",
            "FINDING: cross-user denial enforced but not recorded in the audit trail "
            "(product _attach_turn_attachments._audit_drop omits correlation_id/started_at)",
        )
    return no("us2.denials_audited", "expected an audited attachment_reference_denied failure")


def _chain_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if ev.audit_chain_ok is True:
        return ok("us2.audit_chain_unbroken", "per-user audit chain verified unbroken")
    return no("us2.audit_chain_unbroken", f"chain broken/uncheckable: {ev.audit_chain_ok}")


def _chain_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if ev.audit_chain_ok is not True:
        return ok("us2.audit_chain_unbroken.counter", f"chain not clean: {ev.audit_chain_ok}")
    return no("us2.audit_chain_unbroken.counter", "chain is clean")


def _scope_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    x = ev.extra or {}
    if x.get("withheld") and not x.get("read_success"):
        return ok("us2.scope_withheld", "ungranted-scope tool was not executed on the user's behalf")
    return no("us2.scope_withheld", "a tool ran despite the scope being revoked")


def _scope_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if (ev.extra or {}).get("read_success"):
        return ok("us2.scope_withheld.counter", "tool executed despite revoked scope")
    return no("us2.scope_withheld.counter", "no tool executed under revoked scope")


def _deleg_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    x = ev.extra or {}
    actor = x.get("actor_user_id")
    principal = x.get("auth_principal")
    act_sub = x.get("act_sub")
    if actor and principal and actor != principal and principal == act_sub and actor == x.get("sub"):
        return ok("us2.delegation_attribution",
                  f"agent acts as scoped delegate of the user (on-behalf-of={actor}, acting={principal})")
    return no("us2.delegation_attribution",
              f"delegation attribution not established (actor={actor}, principal={principal})")


def _deleg_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    x = ev.extra or {}
    if x.get("actor_user_id") and x.get("actor_user_id") == x.get("auth_principal"):
        return ok("us2.delegation_attribution.counter", "agent assumed the user's own identity")
    return no("us2.delegation_attribution.counter", "acting agent is distinct from the user")


def _approval_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    x = ev.extra or {}
    if x.get("owner_refused") and x.get("other_refused") and x.get("rejected_audited"):
        return ok("us2.admin_only_approval",
                  "non-admin approval refused (incl. uploader self-approval) and audited")
    return no(
        "us2.admin_only_approval",
        f"approval gate weak (owner_refused={x.get('owner_refused')}, "
        f"other_refused={x.get('other_refused')}, audited={x.get('rejected_audited')})",
    )


def _approval_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    x = ev.extra or {}
    if not x.get("owner_refused") or not x.get("other_refused"):
        return ok("us2.admin_only_approval.counter", "a non-admin approval slipped through")
    return no("us2.admin_only_approval.counter", "all non-admin approvals refused")


CROSS_USER = Check("us2.cross_user_refused", "delegated_authority", _xuser_run, _xuser_counter)
DENIALS = Check("us2.denials_audited", "delegated_authority", _denials_run, None)
CHAIN = Check("us2.audit_chain_unbroken", "delegated_authority", _chain_run, _chain_counter)
SCOPE = Check("us2.scope_withheld", "delegated_authority", _scope_run, _scope_counter)
DELEGATION = Check("us2.delegation_attribution", "delegated_authority", _deleg_run, _deleg_counter)
APPROVAL = Check("us2.admin_only_approval", "delegated_authority", _approval_run, _approval_counter)

for _c in (CROSS_USER, DENIALS, CHAIN, SCOPE, DELEGATION, APPROVAL):
    register(_c)
