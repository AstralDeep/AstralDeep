"""Deterministic coverage for orchestrator/emergency_stop.py: engagement, truthful
responder states, explicit resume denials, and audit-chain re-arm after restart.
"""

import asyncio

import pytest

from orchestrator.emergency_stop import (
    EmergencyStopCoordinator,
    EmergencyStopRefused,
    LOCAL_RESPONDER,
    STATE_PARTIAL,
    STATE_RUNNING,
    STATE_STOPPED,
    STATE_UNREACHABLE,
)


class AuditLog:
    def __init__(self):
        self.events = []

    async def __call__(self, action, outcome, *, owner_id, claims=None, detail=None):
        self.events.append({"action": action, "outcome": outcome, "owner_id": owner_id,
                            "detail": dict(detail or {})})


def make_coordinator(*, remote=None, probe=None, loader=None, audit=None, clock=None):
    if remote is None:
        responders = None
    elif callable(remote):
        responders = remote
    else:
        responders = lambda owner_id: list(remote)  # noqa: E731
    return EmergencyStopCoordinator(
        clock=clock,
        audit=audit,
        remote_responders=responders,
        probe_responder=probe,
        rearm_loader=loader,
    )


def test_engage_without_remote_responders_is_immediately_stopped():
    coordinator = make_coordinator()
    status = asyncio.run(coordinator.engage("owner-1", reason="drill"))
    assert status["engaged"] is True
    assert status["state"] == STATE_STOPPED
    assert status["revision"] == 1
    assert status["reason"] == "drill"
    assert [row["responder"] for row in status["responders"]] == [LOCAL_RESPONDER]
    assert coordinator.admission_allowed("owner-1") is False
    assert coordinator.admission_allowed("owner-2") is True


def test_engage_is_idempotent_while_engaged():
    coordinator = make_coordinator()
    first = asyncio.run(coordinator.engage("owner-1"))
    second = asyncio.run(coordinator.engage("owner-1"))
    assert first["revision"] == second["revision"] == 1


def test_remote_acknowledgment_sweep_transitions_partial_to_stopped():
    probed = []

    async def probe(owner_id, responder):
        probed.append(responder)
        return "acknowledged"

    coordinator = make_coordinator(remote=["machine-a", "machine-b"], probe=probe)
    status = asyncio.run(coordinator.engage("owner-1", sweep=False))
    assert status["state"] == STATE_PARTIAL
    assert coordinator.admission_allowed("owner-1") is False
    final = asyncio.run(coordinator.verify("owner-1"))
    assert probed == ["remote:machine-a", "remote:machine-b"]
    assert final["state"] == STATE_STOPPED
    assert all(row["state"] == "acknowledged" for row in final["responders"])


def test_offline_remote_machine_stays_unreachable_until_it_acknowledges():
    async def probe(owner_id, responder):
        return "unreachable"

    coordinator = make_coordinator(remote=["machine-a"], probe=probe)
    status = asyncio.run(coordinator.engage("owner-1", sweep=False))
    assert status["state"] == STATE_PARTIAL
    verified = asyncio.run(coordinator.verify("owner-1"))
    assert verified["state"] == STATE_UNREACHABLE
    assert verified["responders"][1]["state"] == "unreachable"

    coordinator.acknowledge("owner-1", "remote:machine-a")
    recovered = asyncio.run(coordinator.verify("owner-1"))
    assert recovered["state"] == STATE_STOPPED


def test_pending_remote_execution_keeps_status_partial():
    async def probe(owner_id, responder):
        return "pending"

    coordinator = make_coordinator(remote=["machine-a"], probe=probe)
    status = asyncio.run(coordinator.engage("owner-1", sweep=False))
    verified = asyncio.run(coordinator.verify("owner-1"))
    assert status["state"] == STATE_PARTIAL
    assert verified["state"] == STATE_PARTIAL
    assert verified["responders"][1]["state"] == "pending"


def test_resume_requires_current_revision_and_engaging_owner():
    coordinator = make_coordinator()
    asyncio.run(coordinator.engage("owner-1", reason="drill"))
    with pytest.raises(EmergencyStopRefused) as stale:
        asyncio.run(coordinator.resume("owner-1", expected_revision=99))
    assert stale.value.code == "emergency_stop_stale_revision"
    assert coordinator.admission_allowed("owner-1") is False
    with pytest.raises(EmergencyStopRefused) as denied:
        asyncio.run(coordinator.resume("owner-1", expected_revision=1, actor_id="owner-2"))
    assert denied.value.code == "emergency_stop_resume_denied"
    assert coordinator.admission_allowed("owner-1") is False
    resumed = asyncio.run(coordinator.resume("owner-1", expected_revision=1, actor_id="owner-1"))
    assert resumed["engaged"] is False
    assert resumed["state"] == STATE_RUNNING
    assert coordinator.admission_allowed("owner-1") is True


def test_resume_without_engagement_is_refused():
    coordinator = make_coordinator()
    with pytest.raises(EmergencyStopRefused) as exc:
        asyncio.run(coordinator.resume("owner-1", expected_revision=1))
    assert exc.value.code == "emergency_stop_not_engaged"


def test_invalid_reason_and_revision_are_refused():
    coordinator = make_coordinator()
    with pytest.raises(EmergencyStopRefused) as reason:
        asyncio.run(coordinator.engage("owner-1", reason="x" * 281))
    assert reason.value.code == "emergency_stop_invalid"
    asyncio.run(coordinator.engage("owner-1"))
    with pytest.raises(EmergencyStopRefused) as revision:
        asyncio.run(coordinator.resume("owner-1", expected_revision="one"))
    assert revision.value.code == "emergency_stop_invalid"


def test_engage_and_resume_are_audited_with_denials():
    audit = AuditLog()
    coordinator = make_coordinator(audit=audit)
    asyncio.run(coordinator.engage("owner-1", reason="drill"))
    with pytest.raises(EmergencyStopRefused):
        asyncio.run(coordinator.resume("owner-1", expected_revision=1, actor_id="owner-2"))
    asyncio.run(coordinator.resume("owner-1", expected_revision=1, actor_id="owner-1"))
    actions = [(event["action"], event["outcome"]) for event in audit.events]
    assert actions == [
        ("emergency_stop.engage", "success"),
        ("emergency_stop.resume", "failure"),
        ("emergency_stop.resume", "success"),
    ]
    denial_event = audit.events[1]
    assert denial_event["detail"]["denial"] == "emergency_stop_resume_denied"


def test_durable_rearm_restores_engagement_after_restart():
    durable = {"engaged": True, "revision": 4, "engaged_at": 1234.0,
               "engaged_by": "owner-1", "reason": "prior session"}
    coordinator = make_coordinator(loader=lambda owner_id: durable, remote=["machine-a"])
    assert coordinator.admission_allowed("owner-1") is False
    status = coordinator.status("owner-1")
    assert status["engaged"] is True
    assert status["revision"] == 4
    assert status["engaged_by"] == "owner-1"
    assert status["reason"] == "prior session"
    assert status["responders"][0]["state"] == "acknowledged"
    assert status["responders"][1]["state"] == "pending"
    resumed = asyncio.run(coordinator.resume("owner-1", expected_revision=4, actor_id="owner-1"))
    assert resumed["engaged"] is False


def test_rearm_loader_is_consulted_once_per_owner():
    calls = []

    def loader(owner_id):
        calls.append(owner_id)
        return None

    coordinator = make_coordinator(loader=loader)
    for _ in range(3):
        assert coordinator.admission_allowed("owner-1") is True
    assert calls == ["owner-1"]


def test_unreadable_durable_state_fails_closed():
    def loader(owner_id):
        raise RuntimeError("audit unavailable")

    coordinator = make_coordinator(loader=loader)
    assert coordinator.admission_allowed("owner-1") is False
    status = coordinator.status("owner-1")
    assert status["engaged"] is True
    assert status["state"] == STATE_UNREACHABLE


def test_revision_continues_after_resume_via_durable_trail():
    events = []

    async def audit(action, outcome, *, owner_id, claims=None, detail=None):
        events.append({"action": action, "detail": dict(detail or {})})

    def loader(owner_id):
        for event in reversed(events):
            if event["action"] == "emergency_stop.engage":
                return {"engaged": True, "revision": event["detail"]["revision"],
                        "engaged_at": 1.0, "engaged_by": owner_id, "reason": ""}
            if event["action"] == "emergency_stop.resume" and not event["detail"].get("denial"):
                return {"engaged": False,
                        "revision": event["detail"]["current_revision"]}
        return None

    coordinator = make_coordinator(audit=audit, loader=loader)
    first = asyncio.run(coordinator.engage("owner-1"))
    assert first["revision"] == 1
    asyncio.run(coordinator.resume("owner-1", expected_revision=1, actor_id="owner-1"))
    second = asyncio.run(coordinator.engage("owner-1"))
    assert second["revision"] == 2
    with pytest.raises(EmergencyStopRefused) as stale:
        asyncio.run(coordinator.resume("owner-1", expected_revision=1, actor_id="owner-1"))
    assert stale.value.code == "emergency_stop_stale_revision"


def test_engage_without_event_loop_skips_sweep_but_verify_drives_it():
    async def probe(owner_id, responder):
        return "acknowledged"

    coordinator = make_coordinator(remote=["machine-a"], probe=probe)
    status = asyncio.run(coordinator.engage("owner-1"))
    assert status["state"] == STATE_PARTIAL
    final = asyncio.run(coordinator.verify("owner-1"))
    assert final["state"] == STATE_STOPPED


def test_probe_failures_mark_responder_unreachable():
    async def probe(owner_id, responder):
        raise RuntimeError("socket down")

    coordinator = make_coordinator(remote=["machine-a"], probe=probe)
    status = asyncio.run(coordinator.engage("owner-1", sweep=False))
    verified = asyncio.run(coordinator.verify("owner-1"))
    assert status["state"] == STATE_PARTIAL
    assert verified["state"] == STATE_UNREACHABLE


def test_responder_discovery_failures_preserve_an_unknown_inventory():
    def broken(owner_id):
        raise RuntimeError("plane down")

    coordinator = make_coordinator(remote=broken)
    status = asyncio.run(coordinator.engage("owner-1"))
    assert status["state"] == STATE_UNREACHABLE
    assert status["responders"][1]["kind"] == "discovery"
    assert coordinator.admission_allowed("owner-1") is False


def test_non_list_and_duplicate_responder_discovery_is_normalized():
    coordinator = make_coordinator(remote=lambda owner: "not-a-list")
    status = asyncio.run(coordinator.engage("owner-1"))
    assert status["state"] == STATE_UNREACHABLE

    deduplicating = make_coordinator(remote=lambda owner: ["m-1", "m-1", "m-2"])
    status = asyncio.run(deduplicating.engage("owner-1"))
    assert [row["responder"] for row in status["responders"]] == [
        LOCAL_RESPONDER, "remote:m-1", "remote:m-2"]


def test_audit_failures_never_block_the_stop():
    async def broken_audit(*_args, **_kwargs):
        raise RuntimeError("audit down")

    coordinator = make_coordinator(audit=broken_audit)
    with pytest.raises(EmergencyStopRefused) as error:
        asyncio.run(coordinator.engage("owner-1", reason="drill"))
    assert error.value.code == "emergency_stop_unavailable"
    assert coordinator.status("owner-1")["state"] == STATE_UNREACHABLE
    assert coordinator.admission_allowed("owner-1") is False


def test_responder_updates_reject_unknown_or_stale_targets():
    coordinator = make_coordinator(remote=["m-1"])
    asyncio.run(coordinator.engage("owner-1", sweep=False))
    assert coordinator.acknowledge("owner-1", "remote:missing") is False
    assert coordinator.acknowledge("owner-2", LOCAL_RESPONDER) is False
    assert coordinator.acknowledge("owner-1", LOCAL_RESPONDER) is True
    assert coordinator.mark_unreachable("owner-1", LOCAL_RESPONDER) is False
    with pytest.raises(EmergencyStopRefused):
        coordinator._set_responder("owner-1", LOCAL_RESPONDER, "bogus")


def test_verify_without_engagement_reports_running():
    coordinator = make_coordinator(remote=["m-1"])

    async def probe(owner_id, responder):
        raise AssertionError("no responder probe should run while running")

    coordinator._probe_responder = probe
    status = asyncio.run(coordinator.verify("owner-1"))
    assert status["engaged"] is False
    assert status["state"] == STATE_RUNNING


def test_garbage_probe_observations_are_treated_as_unreachable():
    async def probe(owner_id, responder):
        return "flaky-garbage"

    coordinator = make_coordinator(remote=["m-1"], probe=probe)
    asyncio.run(coordinator.engage("owner-1", sweep=False))
    status = asyncio.run(coordinator.verify("owner-1"))
    assert status["state"] == STATE_UNREACHABLE


def test_rearm_engagement_refuses_corrupt_state():
    durable = {"engaged": True, "revision": "nine", "engaged_at": None,
               "engaged_by": None, "reason": None}

    def clock():
        return 2000.0

    coordinator = make_coordinator(loader=lambda owner: durable, clock=clock)
    status = coordinator.status("owner-1")
    assert status["engaged"] is True
    assert status["state"] == STATE_UNREACHABLE
    assert status["revision"] == 0
    assert coordinator.admission_allowed("owner-1") is False


def test_durable_revision_read_failures_never_reset_the_epoch():
    def loader(owner_id):
        raise RuntimeError("audit down")

    coordinator = make_coordinator(loader=loader)
    with pytest.raises(EmergencyStopRefused):
        asyncio.run(coordinator.engage("owner-1"))
    assert coordinator.admission_allowed("owner-1") is False


@pytest.mark.parametrize("inventory", [["m-1", ""], ["m-1", 5], list(range(65)),
                                      [f"m-{index}" for index in range(65)]])
def test_incomplete_inventory_never_reports_all_responders_stopped(inventory):
    coordinator = make_coordinator(remote=inventory)
    status = asyncio.run(coordinator.engage("owner-1", sweep=False))
    assert status["state"] == STATE_UNREACHABLE
    assert coordinator.admission_allowed("owner-1") is False


def test_resume_audit_failure_keeps_the_durable_stop_and_allows_a_later_retry():
    fail = False
    events = []

    async def audit(action, outcome, **kwargs):
        if fail:
            raise RuntimeError("storage unavailable")
        events.append((action, outcome, kwargs["detail"]))

    coordinator = make_coordinator(audit=audit)
    asyncio.run(coordinator.engage("owner-1"))
    fail = True
    with pytest.raises(EmergencyStopRefused):
        asyncio.run(coordinator.resume("owner-1", expected_revision=1))
    assert coordinator.admission_allowed("owner-1") is False
    assert len(events) == 1
    fail = False
    assert asyncio.run(coordinator.resume("owner-1", expected_revision=1))["engaged"] is False


def test_failed_engage_audit_requires_a_successful_retry_before_resume():
    fail = True

    async def audit(*args, **kwargs):
        if fail:
            raise RuntimeError("storage unavailable")

    coordinator = make_coordinator(audit=audit)
    with pytest.raises(EmergencyStopRefused):
        asyncio.run(coordinator.engage("owner-1"))
    with pytest.raises(EmergencyStopRefused):
        asyncio.run(coordinator.resume("owner-1", expected_revision=1))
    assert coordinator.admission_allowed("owner-1") is False
    fail = False
    assert asyncio.run(coordinator.engage("owner-1"))["revision"] == 1
    assert asyncio.run(coordinator.resume("owner-1", expected_revision=1))["engaged"] is False


def test_revision_remains_monotonic_without_a_rearm_loader():
    coordinator = make_coordinator()
    asyncio.run(coordinator.engage("owner-1"))
    asyncio.run(coordinator.resume("owner-1", expected_revision=1))
    assert asyncio.run(coordinator.engage("owner-1"))["revision"] == 2
    with pytest.raises(EmergencyStopRefused):
        asyncio.run(coordinator.resume("owner-1", expected_revision=1))


def test_late_sweep_result_cannot_acknowledge_a_new_stop_revision():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def probe(owner, responder):
            started.set()
            await release.wait()
            return "acknowledged"

        coordinator = make_coordinator(remote=["m-1"], probe=probe)
        await coordinator.engage("owner-1", sweep=False)
        sweep = asyncio.create_task(coordinator.verify("owner-1"))
        await started.wait()
        await coordinator.resume("owner-1", expected_revision=1)
        await coordinator.engage("owner-1", sweep=False)
        release.set()
        await sweep
        status = coordinator.status("owner-1")
        assert status["revision"] == 2
        assert status["state"] == STATE_PARTIAL

    asyncio.run(scenario())


def test_resume_serializes_new_engage_and_keeps_admission_closed_during_persistence():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def audit(action, outcome, **kwargs):
            if action == "emergency_stop.resume":
                started.set()
                await release.wait()

        coordinator = make_coordinator(audit=audit)
        await coordinator.engage("owner-1", sweep=False)
        resume = asyncio.create_task(coordinator.resume("owner-1", expected_revision=1))
        await started.wait()
        assert coordinator.admission_allowed("owner-1") is False
        engage = asyncio.create_task(coordinator.engage("owner-1", sweep=False))
        release.set()
        assert (await resume)["engaged"] is False
        assert (await engage)["revision"] == 2
        assert coordinator.admission_allowed("owner-1") is False

    asyncio.run(scenario())


def test_resume_cancellation_waits_for_commit_and_applies_durable_transition():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def audit(action, outcome, **kwargs):
            if action == "emergency_stop.resume":
                started.set()
                await release.wait()

        coordinator = make_coordinator(audit=audit)
        await coordinator.engage("owner-1", sweep=False)
        resume = asyncio.create_task(coordinator.resume("owner-1", expected_revision=1))
        await started.wait()
        assert coordinator.admission_allowed("owner-1") is False
        resume.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await resume
        assert coordinator.admission_allowed("owner-1") is True
        assert (await coordinator.engage("owner-1", sweep=False))["revision"] == 2

    asyncio.run(scenario())


def test_repeated_resume_cancellation_cannot_abandon_inflight_audit():
    async def scenario():
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def audit(action, outcome, **kwargs):
            if action == "emergency_stop.resume":
                started.set()
                await release.wait()
                completed.set()

        coordinator = make_coordinator(audit=audit)
        await coordinator.engage("owner-1", sweep=False)
        resume = asyncio.create_task(coordinator.resume("owner-1", expected_revision=1))
        await started.wait()
        resume.cancel()
        await asyncio.sleep(0)
        resume.cancel()
        await asyncio.sleep(0)
        assert not resume.done()
        assert coordinator.admission_allowed("owner-1") is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await resume
        assert completed.is_set()
        assert coordinator.admission_allowed("owner-1") is True

    asyncio.run(scenario())


def test_cancelled_resume_followed_by_stop_rearms_the_new_durable_epoch():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        durable = None

        async def audit(action, outcome, *, owner_id, detail, **kwargs):
            nonlocal durable
            if action == "emergency_stop.resume":
                started.set()
                await release.wait()
                durable = {"engaged": False, "revision": detail["current_revision"]}
            else:
                durable = {"engaged": True, "engaged_by": owner_id, **detail}

        coordinator = make_coordinator(audit=audit, loader=lambda owner: durable)
        await coordinator.engage("owner-1", sweep=False)
        resume = asyncio.create_task(coordinator.resume("owner-1", expected_revision=1))
        await started.wait()
        resume.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await resume
        assert coordinator.admission_allowed("owner-1") is True
        assert (await coordinator.engage("owner-1", sweep=False))["revision"] == 2
        restarted = make_coordinator(loader=lambda owner: durable)
        assert restarted.status("owner-1")["revision"] == 2
        assert restarted.admission_allowed("owner-1") is False

    asyncio.run(scenario())


def test_cancelled_failed_resume_retains_the_local_stop():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def audit(action, outcome, **kwargs):
            if action == "emergency_stop.resume":
                started.set()
                await release.wait()
                raise RuntimeError("durable store unavailable")

        coordinator = make_coordinator(audit=audit)
        await coordinator.engage("owner-1", sweep=False)
        resume = asyncio.create_task(coordinator.resume("owner-1", expected_revision=1))
        await started.wait()
        resume.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await resume
        assert coordinator.admission_allowed("owner-1") is False
        assert coordinator.status("owner-1")["revision"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("update", [{"revision": True}, {"revision": 0}, {"revision": "2"},
                                    {"engaged": "true"}, {"engaged_at": float("nan")},
                                    {"engaged_at": -1}, {"engaged_at": 10**400},
                                    {"engaged_by": "foreign"},
                                    {"reason": None}])
def test_corrupt_durable_state_never_reopens_admission(update):
    durable = {"engaged": True, "revision": 2, "engaged_at": 1.0,
               "engaged_by": "owner-1", "reason": "drill"} | update
    coordinator = make_coordinator(loader=lambda owner: durable)
    assert coordinator.admission_allowed("owner-1") is False
    assert coordinator.status("owner-1")["state"] == STATE_UNREACHABLE


def test_engage_launches_a_background_sweep_when_a_loop_is_running():
    done = asyncio.Event()

    async def probe(owner_id, responder):
        done.set()
        return "acknowledged"

    coordinator = make_coordinator(remote=["m-1"], probe=probe)

    async def scenario():
        status = await coordinator.engage("owner-1")
        await asyncio.wait_for(done.wait(), timeout=2)
        return status

    status = asyncio.run(scenario())
    assert status["state"] == STATE_PARTIAL
    assert coordinator.status("owner-1")["state"] == STATE_STOPPED
