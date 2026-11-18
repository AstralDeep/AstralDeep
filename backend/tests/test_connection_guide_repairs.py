"""First-connection checks remain truthful through probe failure and registration races.
The host uses the same approved component composition for shared rendering and native delivery.
"""

import json
from types import SimpleNamespace

import pytest

from orchestrator import connection_guide as cg
from tests import test_connection_guide as fixtures

production_auth = fixtures.production_auth
signed_in = fixtures.signed_in


@pytest.mark.parametrize("value", ["ftp://astral.example", "https://user:secret@astral.example", "https://a\n.example",
    "https://a .example", "https://" + "x"*2048])
def test_endpoint_rejects_invalid_or_credential_bearing_origins(value):
    assert cg._origin_of(value) == ""


def test_ipv6_origin_is_canonical_and_usable():
    assert cg._origin_of("https://[::1]:8001/path") == "https://[::1]:8001"


def test_lets_recheck_refreshes_stale_health_before_reporting(monkeypatch):
    observed = {"reachable": False, "cached": True, "refreshes": 0}
    def refresh(_):
        observed["cached"] = observed["reachable"]
        observed["refreshes"] += 1
    def snapshot(_):
        return SimpleNamespace(mode="enforce", application_ready=observed["cached"],
            component_status="healthy" if observed["cached"] else "blocked", operator_code="lets_unavailable")
    monkeypatch.setattr("orchestrator.lets_health_api.refresh_lets_reachability", refresh)
    monkeypatch.setattr("orchestrator.lets_health_api.lets_snapshot", snapshot)
    unavailable = cg._lets_check(fixtures.FakeOrch())
    assert unavailable["status"] == cg.STATUS_UNAVAILABLE and unavailable["required"] is True
    observed["reachable"] = True
    recovered = cg._lets_check(fixtures.FakeOrch())
    assert recovered["status"] == cg.STATUS_READY
    assert observed["refreshes"] == 2


def test_lets_refresh_failure_does_not_report_cached_success(monkeypatch):
    def broken(_):
        raise RuntimeError("private health refresh detail")
    monkeypatch.setattr("orchestrator.lets_health_api.refresh_lets_reachability", broken)
    monkeypatch.setattr("orchestrator.lets_health_api.lets_snapshot", lambda _: SimpleNamespace(
        mode="enforce", application_ready=True, component_status="healthy", operator_code="lets_ready"))
    check = cg._lets_check(fixtures.FakeOrch())
    assert check["status"] == cg.STATUS_UNAVAILABLE and check["required"] is True
    assert "private health refresh detail" not in json.dumps(check)
