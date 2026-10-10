"""Host-wiring coverage for orchestrator/emergency_stop_binding.py: the audit hook,
enrolled-remote responder discovery, probe-based acknowledgment with open-execution
awareness, and the audit-backed durable re-arm loader.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from audit.schemas import AuditEventDTO  # noqa: E402
from orchestrator import emergency_stop_binding as binding  # noqa: E402

_SOURCE = object()


@pytest.fixture(autouse=True)
def _fake_plane_source(monkeypatch):
    monkeypatch.setattr(binding, "plane_source", lambda orch: _SOURCE)



def _receipt(event, **changes):
    return AuditEventDTO(**(event.model_dump(exclude={"actor_user_id", "auth_principal"})
                           | {"event_id": "event-1", "recorded_at": datetime.now(UTC)} | changes))


def test_audit_hook_requires_a_matching_durable_receipt():
    recorded = []

    def insert(event):
        recorded.append(event)
        return _receipt(event)

    hook = binding.build_audit_hook(SimpleNamespace(audit_repo=SimpleNamespace(insert=insert)))
    asyncio.run(hook("emergency_stop.engage", "success", owner_id="owner-1",
                     claims={"sub": "owner-1"},
                     detail={"revision": 2, "engaged_at": 5.0, "reason": "drill"}))
    event, = recorded
    assert event.event_class == "emergency_stop"
    assert event.action_type == "emergency_stop.engage"
    assert event.actor_user_id == event.auth_principal == "owner-1"
    assert event.inputs_meta["revision"] == 2
    assert event.outcome == "success"


def test_audit_hook_defaults_claims_to_the_owner_and_reports_denials():
    recorded = []

    def insert(event):
        recorded.append(event)
        return _receipt(event)

    hook = binding.build_audit_hook(SimpleNamespace(audit_repo=SimpleNamespace(insert=insert)))
    asyncio.run(hook("emergency_stop.resume", "failure", owner_id="owner-1", claims=None,
                     detail={"denial": "emergency_stop_resume_denied"}))
    assert recorded[0].actor_user_id == "owner-1"
    assert recorded[0].outcome == "failure"
    assert recorded[0].outcome_detail == "emergency_stop_resume_denied"


def test_remote_responders_lists_enrolled_machine_ids(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.list_machines",
                        lambda source, owner: [
                            {"machine_id": "m-1"}, {"machine_id": "m-2"}])
    discover = binding.build_remote_responders(SimpleNamespace())
    assert discover("owner-1") == ["m-1", "m-2"]


def test_probe_responder_reports_pending_while_executions_are_open(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_jobs.list_open",
                        lambda source, limit: [
                            {"owner_id": "owner-1", "machine_id": "m-1", "terminal": False},
                            {"owner_id": "owner-2", "machine_id": "m-1", "terminal": False}])
    observed = asyncio.run(binding.probe_responder(SimpleNamespace(), "owner-1", "remote:m-1"))
    assert observed == "pending"


def test_probe_responder_keeps_reachable_idle_machines_pending_without_a_stop_receipt(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_jobs.list_open", lambda source, limit: [])
    monkeypatch.setattr("orchestrator.remote_machines.build_target",
                        lambda source, credmgr, owner, machine: SimpleNamespace(machine_id=machine))

    class _Result:
        ok = True

    class _Transport:
        def probe(self, target, *, timeout):
            return _Result()

    monkeypatch.setattr("orchestrator.remote_transport.get_transport", lambda: _Transport())
    orch = SimpleNamespace(credential_manager=None)
    assert asyncio.run(binding.probe_responder(orch, "owner-1", "remote:m-1")) == "pending"

    class _DownTransport:
        def probe(self, target, *, timeout):
            raise RuntimeError("network down")

    monkeypatch.setattr("orchestrator.remote_transport.get_transport", lambda: _DownTransport())
    assert asyncio.run(binding.probe_responder(orch, "owner-1", "remote:m-1")) == "unreachable"


def test_probe_responder_treats_unknown_machine_and_local_responders_truthfully(monkeypatch):
    def broken_target(*_args):
        raise RuntimeError("machine deleted")

    monkeypatch.setattr("orchestrator.remote_jobs.list_open", lambda source, limit: [])
    monkeypatch.setattr("orchestrator.remote_machines.build_target", broken_target)
    assert asyncio.run(binding.probe_responder(SimpleNamespace(), "owner-1",
                                               "remote:gone")) == "unreachable"
    assert asyncio.run(binding.probe_responder(SimpleNamespace(), "owner-1",
                                               "local.orchestrator")) == "acknowledged"


def _event(action, meta=None, recorded=None):
    return SimpleNamespace(action_type=action, inputs_meta=meta or {},
                           actor_user_id="owner-1",
                           event_class="emergency_stop",
                           outcome="failure" if (meta or {}).get("denial") else "success",
                           recorded_at=recorded or datetime(2026, 1, 1, tzinfo=UTC))


def test_rearm_loader_rebuilds_engagement_from_the_audit_trail():
    events = [_event("emergency_stop.engage",
                     {"revision": 3, "engaged_at": "2026-01-01T00:00:00+00:00",
                      "reason": "drill"}),
              _event("emergency_stop.resume", {"current_revision": 2})]
    repo = SimpleNamespace(list_for_user=lambda owner, **kwargs: (events, None))
    loader = binding.build_rearm_loader(SimpleNamespace(audit_repo=repo))
    assert loader("owner-1") == {
        "engaged": True, "revision": 3, "engaged_at": datetime(
            2026, 1, 1, tzinfo=UTC).timestamp(),
        "engaged_by": "owner-1", "reason": "drill",
    }


def test_rearm_loader_reports_resumed_state_and_ignores_denials():
    events = [_event("emergency_stop.resume", {"current_revision": 3, "expected_revision": 3}),
              _event("emergency_stop.resume",
                     {"current_revision": 2, "denial": "emergency_stop_resume_denied"}),
              _event("emergency_stop.engage", {"revision": 2})]
    repo = SimpleNamespace(list_for_user=lambda owner, **kwargs: (events, None))
    loader = binding.build_rearm_loader(SimpleNamespace(audit_repo=repo))
    assert loader("owner-1") == {"engaged": False, "revision": 3}


def test_rearm_loader_handles_missing_or_malformed_events():
    empty = SimpleNamespace(list_for_user=lambda owner, **kwargs: ([], None))
    assert binding.build_rearm_loader(SimpleNamespace(audit_repo=empty))("owner-1") is None
    with pytest.raises(RuntimeError):
        binding.build_rearm_loader(SimpleNamespace())("owner-1")
    junk = SimpleNamespace(list_for_user=lambda owner, **kwargs: (
        [_event("emergency_stop.engage", {"revision": "two", "engaged_at": "nope"})], None))
    with pytest.raises(RuntimeError):
        binding.build_rearm_loader(SimpleNamespace(audit_repo=junk))("owner-1")


def test_mount_attaches_one_coordinator(monkeypatch):
    monkeypatch.setattr(binding, "build_remote_responders", lambda orch: lambda owner: [])
    orch = SimpleNamespace(audit_repo=SimpleNamespace(list_for_user=lambda *a, **kw: ([], None)))
    coordinator = binding.mount(orch)
    assert orch.emergency_stop is coordinator
    assert coordinator.admission_allowed("owner-1") is True


def test_probe_responder_treats_execution_scan_failures_as_unreachable(monkeypatch):
    def broken_scan(limit):
        raise RuntimeError("plane down")

    monkeypatch.setattr("orchestrator.remote_jobs.list_open", lambda source, limit: broken_scan(limit))
    assert asyncio.run(binding.probe_responder(SimpleNamespace(), "owner-1",
                                               "remote:m-1")) == "unreachable"


@pytest.mark.parametrize("changes", [{"event_id": ""}, {"event_class": "settings"},
                                     {"action_type": "emergency_stop.resume"},
                                     {"outcome": "failure"}, {"correlation_id": "foreign"},
                                     {"inputs_meta": {"revision": 9}}])
def test_audit_hook_rejects_mismatched_receipts(changes):
    repo = SimpleNamespace(insert=lambda event: _receipt(event, **changes))
    hook = binding.build_audit_hook(SimpleNamespace(audit_repo=repo))
    with pytest.raises(RuntimeError):
        asyncio.run(hook(binding.ENGAGE_ACTION, "success", owner_id="owner-1"))


@pytest.mark.parametrize("receipt", [None, object()])
def test_audit_hook_rejects_absent_durable_receipts(receipt):
    repo = SimpleNamespace(insert=lambda event: receipt)
    with pytest.raises(RuntimeError):
        asyncio.run(binding.build_audit_hook(SimpleNamespace(audit_repo=repo))(
            binding.ENGAGE_ACTION, "success", owner_id="owner-1"))


def test_audit_hook_rejects_absent_repository_foreign_claims_and_insert_failure(monkeypatch):
    calls = []

    def insert(event):
        calls.append(event)
        raise RuntimeError("durable store unavailable")

    def no_retry(*args, **kwargs):
        raise AssertionError("best-effort retry recorder must not be called")

    monkeypatch.setattr("audit.hooks.record_generic", no_retry)
    hook = binding.build_audit_hook(SimpleNamespace(audit_repo=SimpleNamespace(insert=insert)))
    with pytest.raises(RuntimeError):
        asyncio.run(hook(binding.RESUME_ACTION, "success", owner_id="owner-1"))
    assert len(calls) == 1
    with pytest.raises(RuntimeError):
        asyncio.run(hook(binding.RESUME_ACTION, "success", owner_id="owner-1",
                         claims={"sub": "foreign"}))
    assert len(calls) == 1
    with pytest.raises(RuntimeError):
        asyncio.run(binding.build_audit_hook(SimpleNamespace())(
            binding.ENGAGE_ACTION, "success", owner_id="owner-1"))


@pytest.mark.parametrize("rows", [None, ["junk"], [{}], [{"machine_id": ""}]])
def test_remote_discovery_never_discards_malformed_inventory(monkeypatch, rows):
    monkeypatch.setattr("orchestrator.remote_machines.list_machines", lambda *args: rows)
    with pytest.raises(RuntimeError):
        binding.build_remote_responders(SimpleNamespace())("owner-1")


def test_rearm_loader_paginates_past_acknowledgments_and_failed_transitions():
    calls = []
    stop = _event(binding.ENGAGE_ACTION, {"revision": 4, "engaged_at": 0, "reason": "drill"})

    def list_page(owner, **kwargs):
        calls.append(kwargs["cursor"])
        if kwargs["cursor"] is None:
            return [_event(binding.ACK_ACTION), _event(binding.RESUME_ACTION,
                    {"denial": "emergency_stop_resume_denied"})], "next-page"
        return [stop], None

    loader = binding.build_rearm_loader(SimpleNamespace(
        audit_repo=SimpleNamespace(list_for_user=list_page)))
    assert loader("owner-1")["revision"] == 4
    assert calls == [None, "next-page"]


@pytest.mark.parametrize("page", [(None, None), ([], "next"), ([], 5),
                                  ([_event(binding.ACK_ACTION)] * 26, None)])
def test_rearm_loader_rejects_malformed_pages(page):
    loader = binding.build_rearm_loader(SimpleNamespace(
        audit_repo=SimpleNamespace(list_for_user=lambda *a, **kw: page)))
    with pytest.raises(RuntimeError):
        loader("owner-1")


def test_rearm_loader_refuses_repeated_cursors_and_exhausted_history():
    for repeating in (True, False):
        count = 0

        def list_page(owner, **kwargs):
            nonlocal count
            count += 1
            return [_event(binding.ACK_ACTION)], "same" if repeating else f"page-{count}"

        loader = binding.build_rearm_loader(SimpleNamespace(
            audit_repo=SimpleNamespace(list_for_user=list_page)))
        with pytest.raises(RuntimeError):
            loader("owner-1")
        assert count <= binding._SCAN_PAGES


@pytest.mark.parametrize("meta", [{"current_revision": 2, "expected_revision": 1},
                                  {"current_revision": True, "expected_revision": True},
                                  {"current_revision": 0, "expected_revision": 0},
                                  {"current_revision": 2, "expected_revision": 2, "denial": "bad"}])
def test_rearm_loader_rejects_invalid_successful_resume(meta):
    event = _event(binding.RESUME_ACTION, meta)
    event.outcome = "success"
    loader = binding.build_rearm_loader(SimpleNamespace(
        audit_repo=SimpleNamespace(list_for_user=lambda *a, **kw: ([event], None))))
    with pytest.raises(RuntimeError):
        loader("owner-1")


@pytest.mark.parametrize("timestamp", [None, True, "bad", "2026-01-01T00:00:00",
                                       float("nan"), float("inf"), -1, 10**400,
                                       "1960-01-01T00:00:00+00:00"])
def test_rearm_loader_rejects_invalid_transition_time(timestamp):
    event = _event(binding.ENGAGE_ACTION, {"revision": 1, "engaged_at": timestamp})
    loader = binding.build_rearm_loader(SimpleNamespace(
        audit_repo=SimpleNamespace(list_for_user=lambda *a, **kw: ([event], None))))
    with pytest.raises(RuntimeError):
        loader("owner-1")
