"""Exercises qualified SSH reservations with real Plane leases, sessions, credentials and audit.
Only IAM refresh and the external SSH channel are substituted; durable admission and settlement stay real.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from astralplane.repositories.history import SessionExecutionObservation
from orchestrator import remote_machines
from orchestrator.credential_manager import CredentialManager
from orchestrator.remote_effect_execution import RemoteEffectSelection, selected_effect, run_qualified
from orchestrator.remote_transport import ParamikoTransport
from orchestrator.session_store import WebSessionStore
from orchestrator.work_admission import OperationState
from orchestrator.work_reconciliation import AttemptOutcome, ReconciliationRefused
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.test_work_reconciliation_postgres import _node, _ledger, _submit_and_claim


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ=")
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ=")
    with isolated_plane_runtime("remote_effect") as runtime:
        source = SimpleNamespace(plane_runtime=runtime, plane_repositories=runtime.repositories)
        credentials = CredentialManager(db=source)
        machine = remote_machines.create_machine(source, "owner", "fixture", "ssh.test", 22, "runner", "linux", "plain")
        credentials.set_machine_credential(machine, "owner", "password", "fixture-secret")
        remote_machines.record_probe(source, "owner", machine, "ok", {"type": "ssh-ed25519", "fingerprint": "SHA256:fixture", "blob_b64": "fixture"})
        sessions = WebSessionStore(plane_runtime=runtime, plane_repositories=runtime.repositories)
        sessions.create("session", user_id="owner", access_token="fixture-access", refresh_token="fixture-refresh", hard_max_seconds=3600)
        reference = sessions.capture_execution_reference(owner_id="owner", session_id="session")
        node = _node(runtime, lambda: datetime.now(timezone.utc))
        accepted, claim = _submit_and_claim(node, "physical", "owner")
        with runtime.transaction() as tx:
            runtime.repositories.stop_epochs.bind_operation(tx, owner_id="owner", operation_id=accepted.operation_id)
        ledger = _ledger(runtime, node)
        node.set_completion_guard(ledger.completion_uncertain)
        socket = object()
        connection = SimpleNamespace(websocket=socket, registered=True, closing=False)
        orch = SimpleNamespace(runtime_composition=SimpleNamespace(plane=SimpleNamespace(runtime=runtime)),
            effect_attempts=ledger, work_admission=node, web_sessions=sessions,
            _connection_contexts={id(socket): connection})
        observation = SessionExecutionObservation(reference.state.credential, reference.state.observed_at,
                                                  reference.state.observed_at + timedelta(seconds=15))
        async def refresh(self):
            self._assert_placement()
            return observation
        monkeypatch.setattr(RemoteEffectSelection, "_refresh", refresh)
        yield SimpleNamespace(runtime=runtime, source=source, credentials=credentials, machine=machine,
            node=node, ledger=ledger, socket=socket, connection=connection, orch=orch,
            accepted=accepted, claim=claim, reference=reference, sessions=sessions)


def selection(env):
    return RemoteEffectSelection(env.orch, env.socket, env.connection, "owner", env.claim.fence,
                                 "make_directory", {"machine_id": env.machine, "path": "/fixture"}, env.reference)


def target(env):
    return remote_machines.build_effect_target(env.source, env.credentials, "owner", env.machine)


def attempts(env):
    return env.ledger.list_for_operation(partition="owner", operation_id=env.claim.fence.operation_id)


class Channel:
    def recv(self, size):
        return b""

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return 0

    def recv_stderr_ready(self):
        return False


class Client:
    def __init__(self, env, *, failure=False):
        self.env, self.failure = env, failure
        self.commands = []

    def exec_command(self, command, *, timeout):
        bound = attempts(self.env)
        assert len(bound) >= 1 and bound[-1].dispatch_id
        with self.env.runtime.transaction() as tx:
            rows = self.env.runtime.repositories.credentials.list_credentials(tx,
                owner_id="owner", agent_id="work.effect-attempts.v1", limit=10)
        assert rows
        self.commands.append(command)
        if self.failure:
            raise ConnectionError("reply lost")
        return None, SimpleNamespace(channel=Channel()), None

    def close(self):
        pass


async def run(env, monkeypatch, selected, *, failure=False, pinned=None):
    client = Client(env, failure=failure)
    transport = ParamikoTransport()
    monkeypatch.setattr(transport, "_connect", lambda target, timeout: (client, None))
    with selected_effect(selected):
        result = await asyncio.to_thread(run_qualified, transport, pinned or target(env),
            ["mkdir", "-p", "/fixture"], tool="make_directory", owner_id="owner", timeout=1)
    return result, client


@pytest.mark.asyncio
async def test_physical_command_sees_committed_reservation_and_conclusive_result(env, monkeypatch):
    selected = selection(env)
    result, client = await run(env, monkeypatch, selected)
    assert result.exit_status == 0 and len(client.commands) == 1
    assert attempts(env)[0].outcome is AttemptOutcome.APPLIED
    assert attempts(env)[0].authority_digest == target(env).authority_digest


@pytest.mark.asyncio
async def test_lost_reply_preserves_uncertainty_through_phase_and_parent_completion(env, monkeypatch):
    result, client = await run(env, monkeypatch, selection(env), failure=True)
    assert not result.ok and len(client.commands) == 1
    env.node.update_phase(env.claim.fence, "running")
    record = env.node.terminalize(env.claim.fence, state=OperationState.COMPLETED,
                                  terminal_code=None, safe_summary="Completed", retry_after_ms=None)
    assert record.state is OperationState.RETRYABLE and record.terminal_code == "effect_uncertain"
    assert not attempts(env)[0].settled


@pytest.mark.asyncio
async def test_replaying_selection_never_dispatches_a_second_command(env, monkeypatch):
    selected = selection(env)
    result, first = await run(env, monkeypatch, selected)
    assert result.ok and len(first.commands) == 1
    selected.closed = False
    result, second = await run(env, monkeypatch, selected)
    assert not result.ok and second.commands == [] and len(attempts(env)) == 1


@pytest.mark.asyncio
async def test_separate_identical_selections_have_separate_reservations(env, monkeypatch):
    for _ in range(2):
        result, client = await run(env, monkeypatch, selection(env))
        assert result.ok and len(client.commands) == 1
    assert len({attempt.dispatch_id for attempt in attempts(env)}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["credential", "delete_credential", "retrust", "delete_machine", "bulk_credentials", "purge"])
async def test_revocation_after_target_resolution_prevents_physical_send(env, monkeypatch, mutation):
    pinned = target(env)
    if mutation == "credential":
        env.credentials.set_machine_credential(env.machine, "owner", "password", "replacement")
    elif mutation == "delete_credential":
        env.credentials.delete_machine_credential(env.machine, "owner")
    elif mutation == "retrust":
        remote_machines.retrust_host_key(env.source, "owner", env.machine)
        remote_machines.record_probe(env.source, "owner", env.machine, "ok", {"type": "ssh-ed25519", "fingerprint": "SHA256:fixture", "blob_b64": "fixture"})
    elif mutation == "delete_machine":
        remote_machines.delete_machine(env.source, "owner", env.machine)
    elif mutation == "bulk_credentials":
        env.credentials.remove_machine_credentials_for_user("owner")
    else:
        remote_machines.purge_user_remote_compute(env.source, "owner", env.credentials)
    result, client = await run(env, monkeypatch, selection(env), pinned=pinned)
    assert not result.ok and not client.commands and attempts(env) == ()


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_reservation_and_dispatch(env, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(env.ledger._audit, "insert_in_transaction", failed)
    result, client = await run(env, monkeypatch, selection(env))
    assert not result.ok and not client.commands and attempts(env) == ()


@pytest.mark.asyncio
async def test_session_revoked_after_selection_prevents_reservation(env, monkeypatch):
    selected = selection(env)
    env.sessions.delete("session")
    result, client = await run(env, monkeypatch, selected)
    assert not result.ok and not client.commands and attempts(env) == ()


@pytest.mark.asyncio
async def test_expired_lease_before_sweeper_prevents_send(env, monkeypatch):
    from dataclasses import replace
    from orchestrator.work_admission import ExecutionFence
    from astralplane.repositories.work_admission import ExecutionFence as PlaneFence
    selected = selection(env)
    with env.runtime.transaction() as tx:
        replacement = env.runtime.repositories.work_admission.reselect_execution(
            tx, PlaneFence(env.claim.fence.operation_id, env.claim.fence.execution_generation,
                           env.claim.fence.execution_lease_token),
            now=datetime.now(timezone.utc) - timedelta(seconds=60), slot_lease=timedelta(seconds=1))
    selected.fence = ExecutionFence(replacement.operation_id, replacement.execution_generation,
                                    replacement.execution_lease_token)
    env.claim = replace(env.claim, fence=selected.fence)
    result, client = await run(env, monkeypatch, selected)
    assert not result.ok and not client.commands and attempts(env) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["lease", "credential", "session"])
async def test_authority_changes_during_audit_roll_back_the_reservation(env, monkeypatch, changed):
    from astralplane.repositories.work_admission import ExecutionFence as PlaneFence
    original = env.ledger._audit.insert_in_transaction

    def changing(event, *, transaction, plane_runtime):
        receipt = original(event, transaction=transaction, plane_runtime=plane_runtime)
        repos = plane_runtime.repositories
        if changed == "lease":
            repos.work_admission.reselect_execution(transaction,
                PlaneFence(env.claim.fence.operation_id, env.claim.fence.execution_generation,
                           env.claim.fence.execution_lease_token),
                now=datetime.now(timezone.utc), slot_lease=timedelta(seconds=30))
        elif changed == "credential":
            repos.credentials.delete_machine_credential(transaction, owner_id="owner", machine_id=env.machine)
        else:
            repos.history.sessions.delete_and_return(transaction, owner_id="owner", session_id="session",
                expected_incarnation_id=env.reference.state.credential.incarnation_id)
        return receipt

    monkeypatch.setattr(env.ledger._audit, "insert_in_transaction", changing)
    result, client = await run(env, monkeypatch, selection(env))
    assert not result.ok and not client.commands and attempts(env) == ()
    assert target(env).authority_digest
    assert env.sessions.capture_execution_reference(owner_id="owner", session_id="session")
    with env.runtime.transaction() as tx:
        assert env.node.assert_current_execution_lease(env.claim.fence, transaction=tx)


@pytest.mark.asyncio
async def test_uncertain_report_refinement_has_one_concurrent_conclusive_winner(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    selected = selection(env)
    await run(env, monkeypatch, selected, failure=True)
    env.node.terminalize(env.claim.fence, state=OperationState.RETRYABLE,
        terminal_code="effect_uncertain", safe_summary="Uncertain", retry_after_ms=None)
    attempt = attempts(env)[0]
    env.ledger.settle(partition="owner", operation_id=attempt.operation_id, nonce=attempt.attempt_nonce,
                      outcome=AttemptOutcome.UNCERTAIN, outcome_digest=None)
    barrier = threading.Barrier(2)
    monkeypatch.setattr("orchestrator.work_reconciliation._now_ms", lambda: attempt.recorded_at)

    def settle(outcome):
        barrier.wait(timeout=5)
        try:
            return env.ledger.settle(partition="owner", operation_id=attempt.operation_id,
                nonce=attempt.attempt_nonce, outcome=outcome,
                outcome_digest="a" * 64 if outcome is AttemptOutcome.APPLIED else None)
        except ReconciliationRefused as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(settle, outcome) for outcome in (AttemptOutcome.APPLIED, AttemptOutcome.NOT_APPLIED)]
        values = [future.result(timeout=5) for future in futures]
    assert sum(not isinstance(value, str) for value in values) == 1
    final = attempts(env)[0]
    assert final.settled and final.revision >= attempt.revision + 2
    assert env.ledger.settle(partition="owner", operation_id=attempt.operation_id,
        nonce=attempt.attempt_nonce, outcome=final.outcome, outcome_digest=final.outcome_digest) == final


@pytest.mark.asyncio
async def test_closed_selection_prevents_a_continuing_thread_from_sending(env, monkeypatch):
    import threading
    connected, release = threading.Event(), threading.Event()
    selected = selection(env)
    client = Client(env)
    transport = ParamikoTransport()
    pinned = target(env)

    def connect(target, timeout):
        connected.set()
        assert release.wait(5)
        return client, None

    monkeypatch.setattr(transport, "_connect", connect)
    with selected_effect(selected):
        pending = asyncio.create_task(asyncio.to_thread(run_qualified, transport, pinned,
            ["mkdir", "-p", "/fixture"], tool="make_directory", owner_id="owner", timeout=1))
        assert await asyncio.to_thread(connected.wait, 5)
    release.set()
    result = await asyncio.wait_for(pending, 5)
    assert not result.ok and not client.commands and attempts(env) == ()


@pytest.mark.asyncio
async def test_unpinned_target_and_wrong_owner_never_dispatch(env, monkeypatch):
    selected = selection(env)
    selected.owner_id = "foreign"
    result, client = await run(env, monkeypatch, selected)
    assert not result.ok and not client.commands and attempts(env) == ()
    pinned = target(env)
    pinned.host_key_fingerprint = None
    result, client = await run(env, monkeypatch, selection(env), pinned=pinned)
    assert not result.ok and not client.commands and attempts(env) == ()


@pytest.mark.asyncio
async def test_ordinary_probe_keeps_authority_but_corruption_fails_closed(env):
    first = target(env).authority_digest
    remote_machines.record_probe(env.source, "owner", env.machine, "ok")
    assert target(env).authority_digest == first
    with env.runtime.transaction() as tx:
        env.runtime.repositories.credentials.upsert_credential(tx, owner_id="owner",
            agent_id="remote.effect-authority.v1", credential_key="machine:" + env.machine,
            encrypted_value="invalid", updated_at=1)
    with pytest.raises(Exception, match="remote_authority_invalid"):
        target(env)


@pytest.mark.asyncio
async def test_corrupt_other_attempt_blocks_reservation_without_pruning(env, monkeypatch):
    with env.runtime.transaction() as tx:
        env.runtime.repositories.credentials.upsert_credential(tx, owner_id="owner",
            agent_id="work.effect-attempts.v1", credential_key=uuid.uuid4().hex,
            encrypted_value="invalid", updated_at=1)
    result, client = await run(env, monkeypatch, selection(env))
    assert not result.ok and not client.commands
    with pytest.raises(ReconciliationRefused, match="attempt_inventory_invalid"):
        attempts(env)
    record = env.node.terminalize(env.claim.fence, state=OperationState.COMPLETED,
                                 terminal_code=None, safe_summary="Completed", retry_after_ms=None)
    assert record.state is OperationState.RETRYABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["stopped", "cancelled", "stale", "no_audit", "no_pin"])
async def test_current_dispatch_denials_precede_send(env, monkeypatch, state):
    selected = selection(env)
    pinned = target(env)
    if state == "stopped":
        with env.runtime.transaction() as tx:
            env.runtime.repositories.stop_epochs.engage(tx, owner_id="owner", expected_revision=0,
                actor_id="owner", reason="stop", at=datetime.now(timezone.utc))
    elif state == "cancelled":
        from orchestrator.work_admission import OperationOwner, OwnerScope
        env.node.cancel(owner=OperationOwner(OwnerScope.USER, "owner", None), operation_id=env.accepted.operation_id, terminal_code="cancelled")
    elif state == "stale":
        env.node.reselect_execution(env.claim.fence)
    elif state == "no_audit":
        env.ledger._audit = None
    else:
        pinned.authority_digest = None
    result, client = await run(env, monkeypatch, selected, pinned=pinned)
    assert not result.ok and not client.commands and attempts(env) == ()


@pytest.mark.asyncio
async def test_revocation_serializes_after_committed_reservation_and_blocks_settlement(env, monkeypatch):
    import threading
    bound, release = threading.Event(), threading.Event()
    selected = selection(env)
    pinned = target(env)
    original = env.ledger._audit.insert_in_transaction

    def blocked(event, **kwargs):
        receipt = original(event, **kwargs)
        bound.set()
        assert release.wait(5)
        return receipt

    monkeypatch.setattr(env.ledger._audit, "insert_in_transaction", blocked)
    reservation = asyncio.create_task(asyncio.to_thread(selected.reserve, pinned, ["mkdir", "-p", "/fixture"]))
    assert await asyncio.to_thread(bound.wait, 5)
    mutation = asyncio.create_task(asyncio.to_thread(env.credentials.delete_machine_credential, env.machine, "owner"))
    release.set()
    await asyncio.wait_for(asyncio.gather(reservation, mutation), 5)
    assert len(attempts(env)) == 1 and not attempts(env)[0].settled
    with pytest.raises(Exception, match="remote_authority_unavailable"):
        await asyncio.to_thread(selected.settle, pinned, applied=True, result={"exit_status": 0})
    assert not attempts(env)[0].settled
