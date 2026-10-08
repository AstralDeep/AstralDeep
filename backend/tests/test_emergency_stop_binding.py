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

from orchestrator import emergency_stop_binding as binding  # noqa: E402

_SOURCE = object()


@pytest.fixture(autouse=True)
def _fake_plane_source(monkeypatch):
    monkeypatch.setattr(binding, "plane_source", lambda orch: _SOURCE)



def test_audit_hook_records_through_the_generic_recorder(monkeypatch):
    recorded = {}

    async def fake_record(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr("audit.hooks.record_generic", fake_record)
    hook = binding.build_audit_hook()
    asyncio.run(hook("emergency_stop.engage", "success", owner_id="owner-1",
                     claims={"sub": "owner-1"},
                     detail={"revision": 2, "engaged_at": 5.0, "reason": "drill"}))
    assert recorded["event_class"] == "emergency_stop"
    assert recorded["action_type"] == "emergency_stop.engage"
    assert recorded["claims"] == {"sub": "owner-1"}
    assert recorded["inputs_meta"]["revision"] == 2
    assert recorded["outcome"] == "success"


def test_audit_hook_defaults_claims_to_the_owner_and_reports_denials(monkeypatch):
    recorded = {}

    async def fake_record(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr("audit.hooks.record_generic", fake_record)
    hook = binding.build_audit_hook()
    asyncio.run(hook("emergency_stop.resume", "failure", owner_id="owner-1", claims=None,
                     detail={"denial": "emergency_stop_resume_denied"}))
    assert recorded["claims"] == {"sub": "owner-1"}
    assert recorded["outcome"] == "failure"
    assert recorded["outcome_detail"] == "emergency_stop_resume_denied"


def test_remote_responders_lists_enrolled_machine_ids(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.list_machines",
                        lambda source, owner: [
                            {"machine_id": "m-1"}, {"machine_id": "m-2"}, "junk"])
    discover = binding.build_remote_responders(SimpleNamespace())
    assert discover("owner-1") == ["m-1", "m-2"]


def test_probe_responder_reports_pending_while_executions_are_open(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_jobs.list_open",
                        lambda source, limit: [
                            {"owner_id": "owner-1", "machine_id": "m-1", "terminal": False},
                            {"owner_id": "owner-2", "machine_id": "m-1", "terminal": False}])
    observed = asyncio.run(binding.probe_responder(SimpleNamespace(), "owner-1", "remote:m-1"))
    assert observed == "pending"


def test_probe_responder_acks_only_reachable_idle_machines(monkeypatch):
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
    assert asyncio.run(binding.probe_responder(orch, "owner-1", "remote:m-1")) == "acknowledged"

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
    events = [_event("emergency_stop.resume", {"current_revision": 3}),
              _event("emergency_stop.resume",
                     {"current_revision": 2, "denial": "emergency_stop_resume_denied"}),
              _event("emergency_stop.engage", {"revision": 2})]
    repo = SimpleNamespace(list_for_user=lambda owner, **kwargs: (events, None))
    loader = binding.build_rearm_loader(SimpleNamespace(audit_repo=repo))
    assert loader("owner-1") == {"engaged": False, "revision": 3}


def test_rearm_loader_handles_missing_or_malformed_events():
    empty = SimpleNamespace(list_for_user=lambda owner, **kwargs: ([], None))
    assert binding.build_rearm_loader(SimpleNamespace(audit_repo=empty))("owner-1") is None
    assert binding.build_rearm_loader(SimpleNamespace())("owner-1") is None
    junk = SimpleNamespace(list_for_user=lambda owner, **kwargs: (
        [_event("emergency_stop.engage", {"revision": "two", "engaged_at": "nope"})], None))
    restored = binding.build_rearm_loader(SimpleNamespace(audit_repo=junk))("owner-1")
    assert restored["engaged"] is True
    assert restored["revision"] == 1
    assert restored["engaged_at"] == datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def test_mount_attaches_one_coordinator(monkeypatch):
    monkeypatch.setattr(binding, "build_remote_responders", lambda orch: lambda owner: [])
    orch = SimpleNamespace()
    coordinator = binding.mount(orch)
    assert orch.emergency_stop is coordinator
    assert coordinator.admission_allowed("owner-1") is True


def test_probe_responder_treats_execution_scan_failures_as_unreachable(monkeypatch):
    def broken_scan(limit):
        raise RuntimeError("plane down")

    monkeypatch.setattr("orchestrator.remote_jobs.list_open", lambda source, limit: broken_scan(limit))
    assert asyncio.run(binding.probe_responder(SimpleNamespace(), "owner-1",
                                               "remote:m-1")) == "unreachable"
