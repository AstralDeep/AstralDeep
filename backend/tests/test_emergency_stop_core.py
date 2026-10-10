"""Unit coverage for the emergency-stop coordinator: durable persistence before success
acknowledgment, revision fencing, owner-only resume, fail-closed re-arm, and denial
auditing.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.emergency_stop import (
    EmergencyStopCoordinator,
    EmergencyStopRefused,
    RESUME_ACTION,
    STATE_PERSISTENCE_PENDING,
    STATE_RUNNING,
    STATE_STOPPED,
)

OWNER = "stop-owner-1"
OTHER = "stop-owner-2"


class DurableStore:
    def __init__(self, *, fail: bool = False):
        self.events: list[tuple[str, str, dict]] = []
        self.fail = fail

    def persist(self, owner_id: str, action: str, detail: dict) -> None:
        if self.fail:
            raise OSError("durable write unavailable")
        self.events.append((owner_id, action, dict(detail)))

    def load(self, owner_id: str) -> dict | None:
        engaged = None
        resumed = None
        for event_owner, action, detail in self.events:
            if event_owner != owner_id:
                continue
            if action == "emergency_stop.engage":
                engaged = detail
                resumed = None
            elif action == "emergency_stop.resume":
                resumed = detail
        if resumed is not None:
            return {"engaged": False,
                    "revision": engaged.get("revision") if engaged else 0}
        if engaged is not None:
            return {
                "engaged": True,
                "revision": engaged.get("revision"),
                "engaged_at": 1000.0,
                "engaged_by": owner_id,
                "reason": str(engaged.get("reason") or ""),
            }
        return {"engaged": False, "revision": 0}


def _coordinator(store: DurableStore | None = None, **hooks) -> tuple[
        EmergencyStopCoordinator, DurableStore]:
    resolved = store if store is not None else DurableStore()
    coordinator = EmergencyStopCoordinator(
        persist=resolved.persist,
        load_durable=resolved.load,
        audit_denial=hooks.pop("audit_denial", None),
        **hooks,
    )
    return coordinator, resolved


def test_running_status_reports_revision_zero():
    coordinator, _store = _coordinator()
    status = coordinator.status(OWNER)
    assert status == {
        "engaged": False, "state": STATE_RUNNING, "revision": 0,
        "engaged_at": None, "engaged_by": None, "reason": None,
        "persisted": False, "interrupted": None,
    }


def test_engage_persists_before_acknowledgment_and_gates_admission():
    coordinator, store = _coordinator()
    status = asyncio.run(coordinator.engage(OWNER, reason="  drill   run "))
    assert status["engaged"] is True
    assert status["state"] == STATE_STOPPED
    assert status["persisted"] is True
    assert status["reason"] == "drill run"
    assert status["revision"] == 1
    assert [(owner, action) for owner, action, _ in store.events] == [
        (OWNER, "emergency_stop.engage")]
    assert coordinator.admission_allowed(OWNER) is False
    assert coordinator.admission_allowed(OTHER) is True


def test_engage_is_idempotent_once_persisted():
    coordinator, store = _coordinator()
    first = asyncio.run(coordinator.engage(OWNER, reason="one"))
    second = asyncio.run(coordinator.engage(OWNER, reason="two"))
    assert second["revision"] == first["revision"] == 1
    assert second["reason"] == "one"
    assert len(store.events) == 1


def test_unpersisted_engage_reports_pending_and_retry_persists_same_revision():
    store = DurableStore(fail=True)
    coordinator = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    with pytest.raises(EmergencyStopRefused) as refused:
        asyncio.run(coordinator.engage(OWNER, reason="drill"))
    assert refused.value.code == "emergency_stop_persistence_failed"
    assert refused.value.status_code == 503
    pending = coordinator.status(OWNER)
    assert pending["engaged"] is True
    assert pending["state"] == STATE_PERSISTENCE_PENDING
    assert pending["persisted"] is False
    assert coordinator.admission_allowed(OWNER) is False
    store.fail = False
    retry = asyncio.run(coordinator.engage(OWNER, reason="drill"))
    assert retry["state"] == STATE_STOPPED
    assert retry["persisted"] is True
    assert retry["revision"] == pending["revision"]
    assert len(store.events) == 1


def test_engage_without_persistence_refuses_and_stays_engaged_locally():
    coordinator = EmergencyStopCoordinator()
    with pytest.raises(EmergencyStopRefused) as refused:
        asyncio.run(coordinator.engage(OWNER))
    assert refused.value.code == "emergency_stop_unavailable"
    assert coordinator.admission_allowed(OWNER) is False


def test_denials_are_audited():
    denials: list[tuple[str, str, str]] = []

    async def audit(action, outcome, *, owner_id, detail=None):
        denials.append((action, outcome, str((detail or {}).get("denial"))))

    coordinator = EmergencyStopCoordinator(audit_denial=audit)
    with pytest.raises(EmergencyStopRefused):
        asyncio.run(coordinator.engage(OWNER))
    assert denials == [("emergency_stop.engage", "failure",
                        "emergency_stop_unavailable")]


def test_resume_requires_engagement_current_revision_and_engaging_owner():
    coordinator, _store = _coordinator()
    with pytest.raises(EmergencyStopRefused) as missing:
        asyncio.run(coordinator.resume(OWNER, expected_revision=1))
    assert missing.value.code == "emergency_stop_not_engaged"
    assert missing.value.status_code == 409
    engaged = asyncio.run(coordinator.engage(OWNER))
    with pytest.raises(EmergencyStopRefused) as stale:
        asyncio.run(coordinator.resume(OWNER, expected_revision=engaged["revision"] + 1))
    assert stale.value.code == "emergency_stop_stale_revision"
    with pytest.raises(EmergencyStopRefused) as foreign:
        asyncio.run(coordinator.resume(OWNER, expected_revision=engaged["revision"],
                                       actor_id=OTHER))
    assert foreign.value.code == "emergency_stop_resume_denied"
    assert foreign.value.status_code == 403
    resumed = asyncio.run(coordinator.resume(
        OWNER, expected_revision=engaged["revision"], actor_id=OWNER))
    assert resumed["engaged"] is False
    assert resumed["state"] == STATE_RUNNING
    assert coordinator.admission_allowed(OWNER) is True


def test_resume_persistence_failure_keeps_the_stop_engaged():
    store = DurableStore()
    coordinator = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    asyncio.run(coordinator.engage(OWNER))
    store.fail = True
    with pytest.raises(EmergencyStopRefused) as refused:
        asyncio.run(coordinator.resume(OWNER, expected_revision=1, actor_id=OWNER))
    assert refused.value.code == "emergency_stop_persistence_failed"
    assert coordinator.admission_allowed(OWNER) is False
    store.fail = False
    resumed = asyncio.run(coordinator.resume(OWNER, expected_revision=1, actor_id=OWNER))
    assert resumed["engaged"] is False


def test_revision_is_monotonic_across_stop_resume_cycles():
    coordinator, store = _coordinator()
    first = asyncio.run(coordinator.engage(OWNER))
    asyncio.run(coordinator.resume(OWNER, expected_revision=first["revision"],
                                   actor_id=OWNER))
    second = asyncio.run(coordinator.engage(OWNER))
    assert second["revision"] == first["revision"] + 1
    assert [(owner, action) for owner, action, _ in store.events] == [
        (OWNER, "emergency_stop.engage"),
        (OWNER, RESUME_ACTION),
        (OWNER, "emergency_stop.engage"),
    ]


def test_invalid_engage_inputs_are_refused():
    coordinator, store = _coordinator()
    with pytest.raises(EmergencyStopRefused) as empty:
        asyncio.run(coordinator.engage(""))
    assert empty.value.code == "emergency_stop_invalid"
    with pytest.raises(EmergencyStopRefused) as wrong_type:
        asyncio.run(coordinator.engage(OWNER, reason=7))
    assert wrong_type.value.code == "emergency_stop_invalid"
    with pytest.raises(EmergencyStopRefused) as too_long:
        asyncio.run(coordinator.engage(OWNER, reason="x" * 281))
    assert too_long.value.code == "emergency_stop_invalid"
    assert store.events == []


def test_resume_revision_validation():
    coordinator, _store = _coordinator()
    for bad in (None, "1", 0, -1, True, 1.0):
        with pytest.raises(EmergencyStopRefused) as invalid:
            asyncio.run(coordinator.resume(OWNER, expected_revision=bad))
        assert invalid.value.code == "emergency_stop_invalid"


def test_interruption_hook_runs_and_failures_do_not_block_engagement():
    observed: list[str] = []

    async def interrupt(owner_id: str) -> dict:
        observed.append(owner_id)
        return {"tasks": 2, "sessions": 1}

    coordinator, _store = _coordinator(interrupt=interrupt)
    status = asyncio.run(coordinator.engage(OWNER))
    assert observed == [OWNER]
    assert status["interrupted"] == {"tasks": 2, "sessions": 1}

    def broken(owner_id: str) -> dict:
        raise RuntimeError("interruption unavailable")

    coordinator_broken, _store_broken = _coordinator(interrupt=broken)
    status = asyncio.run(coordinator_broken.engage(OWNER))
    assert status["persisted"] is True
    assert status["interrupted"] == {"error": "interrupt_failed"}


def test_unreadable_durable_state_fails_admission_closed_but_reports_status():
    def broken_load(owner_id: str) -> dict:
        raise OSError("durable store down")

    coordinator = EmergencyStopCoordinator(
        persist=DurableStore().persist, load_durable=broken_load)
    assert coordinator.admission_allowed(OWNER) is False
    status = coordinator.status(OWNER)
    assert status["engaged"] is True
    assert status["state"] == "durable_unavailable"
    with pytest.raises(EmergencyStopRefused) as refused:
        asyncio.run(coordinator.engage(OWNER))
    assert refused.value.code == "emergency_stop_unavailable"


def test_legacy_and_empty_owners_are_never_gated():
    coordinator, _store = _coordinator()
    assert coordinator.admission_allowed("legacy") is True
    assert coordinator.admission_allowed("") is True
    assert coordinator.admission_allowed(None) is True


def test_restart_re_arms_the_stop_from_durable_state():
    store = DurableStore()
    first = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    engaged = asyncio.run(first.engage(OWNER, reason="persisted"))
    survivor = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    assert survivor.admission_allowed(OWNER) is False
    status = survivor.status(OWNER)
    assert status["engaged"] is True
    assert status["state"] == STATE_STOPPED
    assert status["revision"] == engaged["revision"]
    assert status["reason"] == "persisted"
    re_engaged = asyncio.run(survivor.engage(OWNER, reason="again"))
    assert re_engaged["revision"] == engaged["revision"]
    assert re_engaged["reason"] == "persisted"
    resumed = asyncio.run(survivor.resume(
        OWNER, expected_revision=engaged["revision"], actor_id=OWNER))
    assert resumed["engaged"] is False
    next_cycle = asyncio.run(survivor.engage(OWNER, reason="second cycle"))
    assert next_cycle["revision"] == engaged["revision"] + 1
    assert next_cycle["reason"] == "second cycle"


def test_resume_after_restart_clears_the_re_armed_stop():
    store = DurableStore()
    first = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    engaged = asyncio.run(first.engage(OWNER))
    survivor = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    resumed = asyncio.run(survivor.resume(
        OWNER, expected_revision=engaged["revision"], actor_id=OWNER))
    assert resumed["engaged"] is False
    assert survivor.admission_allowed(OWNER) is True


def test_refresh_re_reads_durable_state():
    store = DurableStore()
    coordinator = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    assert coordinator.refresh(OWNER)["engaged"] is False
    store.events.append((OWNER, "emergency_stop.engage",
                         {"revision": 3, "engaged_at": "1970-01-01T00:16:40+00:00",
                          "engaged_by": OWNER, "reason": "external"}))
    refreshed = coordinator.refresh(OWNER)
    assert refreshed["engaged"] is True
    assert refreshed["revision"] == 3
    assert refreshed["engaged_by"] == OWNER
    assert coordinator.admission_allowed(OWNER) is False


def test_durable_revision_fallbacks_tolerate_malformed_values():
    store = DurableStore()
    coordinator = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    store.events.append((OWNER, "emergency_stop.engage",
                         {"revision": "bogus", "reason": ""}))
    status = coordinator.refresh(OWNER)
    assert status["engaged"] is True
    assert status["revision"] == 1


def test_refresh_keeps_a_higher_in_memory_revision_than_durable():
    store = DurableStore()
    coordinator = EmergencyStopCoordinator(persist=store.persist, load_durable=store.load)
    assert coordinator.status(OWNER)["engaged"] is False
    store.fail = True
    with pytest.raises(EmergencyStopRefused):
        asyncio.run(coordinator.engage(OWNER, reason="local"))
    pending = coordinator.status(OWNER)
    assert pending["revision"] == 1 and pending["persisted"] is False
    store.fail = False
    store.events.append((OWNER, "emergency_stop.engage",
                         {"revision": 1, "reason": "local"}))
    refreshed = coordinator.refresh(OWNER)
    assert refreshed["revision"] == 1
    assert refreshed["persisted"] is True

    store.events.append((OTHER, "emergency_stop.engage", {"revision": 4}))
    assert coordinator.refresh(OTHER)["revision"] == 4
    assert coordinator.admission_allowed(OWNER) is False
    assert coordinator.admission_allowed(OTHER) is False


def test_persistence_hook_raising_refused_propagates_its_code():
    def refusing_persist(owner_id, action, detail):
        raise EmergencyStopRefused("emergency_stop_unavailable", 503)

    coordinator = EmergencyStopCoordinator(persist=refusing_persist)
    with pytest.raises(EmergencyStopRefused) as refused:
        asyncio.run(coordinator.engage(OWNER))
    assert refused.value.code == "emergency_stop_unavailable"
    assert coordinator.status(OWNER)["state"] == STATE_PERSISTENCE_PENDING


def test_denial_audit_hook_failure_does_not_break_the_stop():
    def broken_audit(*args, **kwargs):
        raise RuntimeError("audit sink down")

    store = DurableStore(fail=True)
    coordinator = EmergencyStopCoordinator(
        persist=store.persist, load_durable=store.load, audit_denial=broken_audit)
    with pytest.raises(EmergencyStopRefused):
        asyncio.run(coordinator.engage(OWNER))
    assert coordinator.admission_allowed(OWNER) is False


def test_state_vanishing_during_engage_is_refused():
    store = DurableStore()

    def racing_persist(owner_id, action, detail):
        store.persist(owner_id, action, detail)
        coordinator._states.pop(OWNER, None)

    coordinator = EmergencyStopCoordinator(
        persist=racing_persist, load_durable=store.load)
    with pytest.raises(EmergencyStopRefused) as refused:
        asyncio.run(coordinator.engage(OWNER))
    assert refused.value.code == "emergency_stop_unavailable"
