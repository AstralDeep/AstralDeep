"""Tests for the backend-only-UI check (backend/verification/checks/thin_client.py,
AstralProjection webrender): the real client.js has no construction logic, and the
check separates a clean surface from one with a framework or per-type switch.
"""

from __future__ import annotations

from verification.checks.thin_client import NO_CLIENT_CONSTRUCTION, inspect_client_surface
from verification.evidence import CapturedEvidence
from verification.verdict import Outcome, reconcile


def _evidence(inspection):
    return CapturedEvidence(
        evidence_id="e", scenario_id="client:surface", run_mode="mock_inprocess",
        client_inspection=inspection,
    )


def _verdict(ev):
    res = NO_CLIENT_CONSTRUCTION.run(ev, {})
    refuted = NO_CLIENT_CONSTRUCTION.counter_refutes(ev, {}) if res.outcome == Outcome.PASS else False
    return reconcile(res.outcome, refuted, None)[0]


def test_client_surface_has_no_construction_logic():
    insp = inspect_client_surface()
    assert insp.get("readable"), f"client.js unreadable: {insp}"
    assert not insp["framework_import"], "client must not import a rendering framework"
    assert not insp["type_switch"], "client must not construct components by type"
    assert insp["injects_server_html"], "client must inject server HTML generically"
    assert insp["forwards_actions"], "client must forward ui_event actions"


def test_real_client_passes_the_check():
    assert _verdict(_evidence(inspect_client_surface())) == Outcome.PASS


def test_check_flags_construction_logic_and_unreadable_surface():
    clean = inspect_client_surface()
    assert _verdict(_evidence({**clean, "framework_import": True})) == Outcome.FAIL
    assert _verdict(_evidence({**clean, "type_switch": True})) == Outcome.FAIL
    assert _verdict(_evidence({**clean, "injects_server_html": False})) == Outcome.UNCERTAIN
    assert _verdict(_evidence({"readable": False, "error": "missing"})) == Outcome.UNCERTAIN
    assert NO_CLIENT_CONSTRUCTION.counter(_evidence({**clean, "type_switch": True}), {}).outcome == Outcome.PASS
