"""Exercises context_authority.py against signed human identities and the existing PostgreSQL authority fixtures.
Inherited readers retain their original turn, consent, and operation lease without gaining write authority.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import auth, context_authority as module, turn_guidance_authority as guidance
from orchestrator.chain_authority import ChainBudget
from orchestrator.orchestrator import Orchestrator
from orchestrator.work_admission import (
    AdmissionClass, OperationOwner, OperationRequest, OperationState, OwnerScope,
    WorkAdmissionCoordinator,
)
from persistent_agents.models import AssignmentError
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_human_request_authority_088 import human as human, selected
from tests.test_turn_guidance_machine_088 import bind as bind_machine, machine as machine
from tests.test_user_skill_facade_088 import catalog as catalog
from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def admitted_turn(human, bound, fixture, *, cookie=True, operation=False):
    host, socket, chat = human[2], object(), str(uuid4())
    caller = await selected(human, bound, fixture, cookie=cookie)
    origin = await guidance.capture_turn_guidance_from_human(caller, expected_orchestrator=host)
    binding = guidance.bind_http_guidance(origin, expected_orchestrator=host, websocket=socket, chat_id=chat)
    if operation:
        coordinator = await asyncio.to_thread(WorkAdmissionCoordinator.from_plane,
            plane_runtime=caller.runtime, slot_lease=timedelta(minutes=5))
        host.work_admission = coordinator
        request = OperationRequest(
            "connection_frame", AdmissionClass.INTERACTIVE, OperationOwner(OwnerScope.USER, fixture[1], None),
            uuid4(), "context_authority", str(uuid4()), "a" * 64, chat, None, uuid4(), uuid4(),
        )
        accepted = await asyncio.to_thread(coordinator.submit, request)
        claimed = await asyncio.to_thread(coordinator.claim_operation, AdmissionClass.INTERACTIVE, accepted.operation_id)
        assert claimed is not None
        operation_binding = guidance._OperationBinding.capture(host, origin, claimed.operation, claimed.fence)
        binding = replace(binding, operations=(operation_binding,))
    try:
        with guidance.use_turn_guidance(binding, expected_orchestrator=host):
            yield host, socket, chat, binding
    finally:
        origin.close()


async def acquire(turn):
    host, socket, chat, _ = turn
    return await module.capture_context_authority(orchestrator=host, websocket=socket, chat_id=chat)


async def verify(lease, turn):
    host, socket, chat, _ = turn
    return await lease.verify(orchestrator=host, websocket=socket, chat_id=chat)


async def test_parent_and_inherited_child_return_exact_stable_read_proof(human, bound, fixture):
    async with admitted_turn(human, bound, fixture, operation=True) as turn:
        host, socket, chat, binding = turn
        lease = await acquire(turn)
        assert lease.owner_id == fixture[1] and lease.chat_id == chat
        assert fixture[1] not in repr(lease) and fixture[3]() not in repr(lease)
        with module.use_context_authority(lease):
            expected = await verify(lease, turn)
            assert expected.owner_id == fixture[1] and expected.chat_id == chat
            assert expected.operation_fences == (binding.operations[0].fence,)
            assert expected.operation_records[0].operation_id == binding.operations[0].record.operation_id
            assert expected.operation_records[0].normalized_input_digest == "a" * 64
            identities = {expected.stable_identity}
            assert lease.assert_current(orchestrator=host, websocket=socket, chat_id=chat) is None
            assert fixture[1] not in repr(expected) and "2026" not in repr(expected)
            async def child():
                inherited = module.current_context_authority(orchestrator=host, websocket=socket, chat_id=chat)
                assert inherited is lease
                observed = await verify(inherited, turn)
                with pytest.raises(AssignmentError):
                    await guidance.acquire_turn_guidance_reader(expected_orchestrator=host,
                                                               websocket=socket, chat_id=chat)
                with pytest.raises(AssignmentError, match="human_write_required"):
                    inherited.require_write()
                return observed
            observed = await asyncio.create_task(child())
            assert observed.stable_identity in identities
            assert observed.stable_identity == expected.stable_identity
            assert observed.operation_records == expected.operation_records
            with pytest.raises(FrozenInstanceError):
                expected.owner_id = "forged"
            assert not hasattr(lease, "transaction") and not hasattr(lease, "repositories")
        with pytest.raises(AssignmentError):
            await verify(lease, turn)
        assert fixture[-1] == []


@pytest.mark.parametrize("cookie", [True, False])
async def test_legitimate_existing_http_binding_preserves_its_empty_operation_scope(human, bound, fixture, cookie):
    async with admitted_turn(human, bound, fixture, cookie=cookie) as turn:
        lease = await acquire(turn)
        with module.use_context_authority(lease):
            proof = await verify(lease, turn)
            assert proof.operation_records == () and proof.operation_fences == ()
            assert proof.stable_identity == (fixture[1], turn[2], ())
            assert fixture[-1] == []


@pytest.mark.parametrize("turn_class", ["parser_replay", "draft_self_test"])
async def test_actual_machine_read_scope_transfers_without_remint_and_revocation_stays_current(
    machine, fixture, runtime, turn_class,
):
    binding, authority, _, _ = await bind_machine(machine, fixture, runtime, turn_class)
    host, socket, chat = machine[0], binding.websocket, binding.chat_id
    try:
        with guidance.use_turn_guidance(binding, expected_orchestrator=host):
            lease = await module.capture_context_authority(orchestrator=host, websocket=socket, chat_id=chat)
            with module.use_context_authority(lease):
                async def child():
                    current = module.current_context_authority(orchestrator=host, websocket=socket, chat_id=chat)
                    return await current.verify(orchestrator=host, websocket=socket, chat_id=chat)
                proof = await asyncio.create_task(child())
                assert proof.owner_id == authority.user_id == fixture[1]
                assert proof.operation_records == () and len(machine[2]) == 1
                await asyncio.to_thread(host.offline_grants.revoke_for_user, fixture[1])
                with pytest.raises(AssignmentError):
                    await asyncio.create_task(child())
                assert len(machine[2]) == 1
    finally:
        binding.origin.close()
        Orchestrator._unbind_machine_turn(host, socket)


async def test_missing_and_forged_authority_never_authorize():
    for value in (None, "owner", SimpleNamespace(owner_id="owner")):
        with pytest.raises(AssignmentError):
            with module.use_context_authority(value):
                pytest.fail("forged authority entered")
        with pytest.raises(AssignmentError):
            module.ContextAuthorityLease(value, orchestrator=object())
    with pytest.raises(AssignmentError):
        module.current_context_authority(orchestrator=object(), websocket=None, chat_id="chat")
    with pytest.raises(AssignmentError):
        await module.capture_context_authority(orchestrator=object(), websocket=None, chat_id="chat")


@pytest.mark.parametrize("mismatch", ["host", "socket", "chat"])
async def test_child_scope_mismatch_refuses_without_adopting_new_turn(human, bound, fixture, mismatch):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        host, socket, chat, _ = turn
        values = dict(orchestrator=host, websocket=socket, chat_id=chat)
        values[{"host": "orchestrator", "socket": "websocket", "chat": "chat_id"}[mismatch]] = object()
        with module.use_context_authority(lease):
            with pytest.raises(AssignmentError):
                module.current_context_authority(**values)
            with pytest.raises(AssignmentError):
                await asyncio.create_task(lease.verify(**values))
            with pytest.raises(AssignmentError):
                lease.assert_current(**values)
            with pytest.raises(AssignmentError):
                await module.capture_context_authority(**values)
            assert (await verify(lease, turn)).owner_id == fixture[1]


async def test_capture_and_scope_installation_require_original_task(human, bound, fixture):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        async def child_capture():
            with pytest.raises(AssignmentError):
                await acquire(turn)
            with pytest.raises(AssignmentError):
                with module.use_context_authority(lease):
                    pytest.fail("child installed authority")
        await asyncio.create_task(child_capture())
        with module.use_context_authority(lease):
            assert (await verify(lease, turn)).owner_id == fixture[1]


@pytest.mark.parametrize("loss", ["origin", "turn", "boundary", "policy", "session", "replacement", "runtime"])
async def test_inherited_authority_refuses_original_authentication_loss(human, bound, fixture, runtime, monkeypatch, loss):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        with module.use_context_authority(lease):
            if loss == "origin":
                turn[3].origin.close()
            elif loss == "turn":
                turn[3].close()
            elif loss == "boundary":
                human[1].close()
            elif loss == "policy":
                monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "different-client")
            elif loss == "session":
                await asyncio.to_thread(fixture[0].delete, fixture[2])
            elif loss == "replacement":
                previous = await asyncio.to_thread(get_session_record, runtime, fixture[2])
                await asyncio.to_thread(replace_session_record, runtime, previous)
            else:
                monkeypatch.setattr(turn[0].runtime_composition.plane, "runtime", object())
            with pytest.raises(AssignmentError):
                await asyncio.create_task(verify(lease, turn))


@pytest.mark.parametrize("change", ["claims", "expiry", "error", "malformed"])
async def test_every_delivery_rechecks_original_production_iam(human, bound, fixture, monkeypatch, change):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        original = auth.verify_production_token
        async def changed(token):
            claims = await original(token)
            if change == "error":
                raise RuntimeError("private-provider-credential")
            if change == "malformed":
                return {**claims, "unserializable": object()}
            if change == "claims":
                return {**claims, "realm_access": {"roles": ["different-current-role"]}}
            return {**claims, "exp": claims["exp"] + 1}
        monkeypatch.setattr(auth, "verify_production_token", changed)
        with module.use_context_authority(lease):
            with pytest.raises(AssignmentError) as refused:
                await asyncio.create_task(verify(lease, turn))
            assert "private-provider-credential" not in str(refused.value)
            assert refused.value.__cause__ is None


async def test_session_replaced_during_actual_iam_await_is_refused(human, bound, fixture, runtime, monkeypatch):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        original = auth.verify_production_token
        async def changed(token):
            claims = await original(token)
            previous = await asyncio.to_thread(get_session_record, runtime, fixture[2])
            await asyncio.to_thread(replace_session_record, runtime, previous)
            return claims
        monkeypatch.setattr(auth, "verify_production_token", changed)
        with module.use_context_authority(lease):
            with pytest.raises(AssignmentError):
                await asyncio.create_task(verify(lease, turn))
        assert fixture[-1] == []


async def test_session_consent_is_rechecked_after_current_operation_checks(human, bound, fixture, runtime, monkeypatch):
    async with admitted_turn(human, bound, fixture, operation=True) as turn:
        lease = await acquire(turn)
        coordinator = turn[3].operations[0].coordinator
        original = coordinator.assert_current_execution_lease
        changed = []
        def retire(*args, **kwargs):
            value = original(*args, **kwargs)
            if not changed:
                changed.append(True)
                runtime.repositories.history.sessions.delete(kwargs["transaction"], owner_id=fixture[1],
                    session_id=fixture[2], expected_incarnation_id=turn[3].origin.credential.incarnation_id)
            return value
        monkeypatch.setattr(coordinator, "assert_current_execution_lease", retire)
        with module.use_context_authority(lease):
            with pytest.raises(AssignmentError):
                await verify(lease, turn)
        assert changed == [True]


@pytest.mark.parametrize("loss", ["cancelled", "completed", "reselected", "metadata", "generation", "token"])
async def test_actual_operation_fence_and_owner_metadata_are_current(human, bound, fixture, monkeypatch, loss):
    async with admitted_turn(human, bound, fixture, operation=True) as turn:
        lease = await acquire(turn)
        operation = turn[3].operations[0]
        with module.use_context_authority(lease):
            if loss in {"cancelled", "completed"}:
                await asyncio.to_thread(operation.coordinator.terminalize, operation.fence,
                    state=OperationState.CANCELLED if loss == "cancelled" else OperationState.COMPLETED,
                    terminal_code="cancelled" if loss == "cancelled" else None,
                    safe_summary=None, retry_after_ms=None)
            elif loss == "reselected":
                await asyncio.to_thread(operation.coordinator.reselect_execution, operation.fence)
            else:
                original = operation.coordinator.assert_current_execution_lease
                def wrong_owner(*args, **kwargs):
                    value = original(*args, **kwargs)
                    changes = {
                        "metadata": {"owner_user_id": "foreign-owner"},
                        "generation": {"execution_generation": value.execution_generation + 1},
                        "token": {"execution_lease_token": uuid4()},
                    }
                    return replace(value, **changes[loss])
                monkeypatch.setattr(operation.coordinator, "assert_current_execution_lease", wrong_owner)
            with pytest.raises(AssignmentError):
                await asyncio.create_task(verify(lease, turn))


async def test_proof_tracks_current_revision_without_clock_fields(human, bound, fixture):
    async with admitted_turn(human, bound, fixture, operation=True) as turn:
        lease = await acquire(turn)
        with module.use_context_authority(lease):
            before = await verify(lease, turn)
            operation = turn[3].operations[0]
            await asyncio.to_thread(operation.coordinator.update_phase, operation.fence, "model_call")
            after = await verify(lease, turn)
            assert before.stable_identity != after.stable_identity
            assert after.operation_records[0].state_revision > before.operation_records[0].state_revision
            assert after.operation_fences == before.operation_fences
            record = after.operation_records[0]
            clock_only = replace(after, operation_records=(replace(record,
                accepted_at=record.accepted_at + timedelta(days=1),
                updated_at=record.updated_at + timedelta(days=1)),))
            assert after.stable_identity == clock_only.stable_identity


async def test_parent_guidance_and_budget_currentness_survive_child_transfer(human, bound, fixture, monkeypatch):
    async with admitted_turn(human, bound, fixture, operation=True) as turn:
        host, socket, _, parent = turn
        parent_budget = ChainBudget("parent", chat_id=parent.chat_id)
        child_chat = str(uuid4())
        child_budget = ChainBudget("child", chat_id=child_chat, parent=parent_budget)
        host._chain_budgets = {parent.chat_id: parent_budget, child_chat: child_budget}
        binding = guidance.inherit_turn_guidance(parent, expected_orchestrator=host,
                                                 websocket=socket, chat_id=child_chat, budget=child_budget)
        with guidance.use_turn_guidance(binding, expected_orchestrator=host):
            child_turn = host, socket, child_chat, binding
            lease = await acquire(child_turn)
            with module.use_context_authority(lease):
                proof = await asyncio.create_task(verify(lease, child_turn))
                assert proof.operation_fences == (parent.operations[0].fence,)
                child_budget.spent_hops = child_budget.max_hops + 1
                with pytest.raises(AssignmentError):
                    await asyncio.create_task(verify(lease, child_turn))


async def test_original_task_cancellation_revokes_inherited_child_before_scope_exit(human, bound, fixture):
    ready, check = asyncio.Event(), asyncio.Event()
    results = []
    async def original_turn():
        async with admitted_turn(human, bound, fixture) as turn:
            lease = await acquire(turn)
            with module.use_context_authority(lease):
                async def child():
                    await check.wait()
                    with pytest.raises(AssignmentError):
                        await verify(lease, turn)
                    results.append("refused")
                callback = asyncio.create_task(child())
                ready.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    check.set()
                    await asyncio.shield(callback)
                    raise
    task = asyncio.create_task(original_turn())
    await asyncio.wait_for(ready.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert results == ["refused"]


async def test_inherited_context_is_revoked_when_original_scope_ends(human, bound, fixture):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        release = asyncio.Event()
        async def delayed():
            await release.wait()
            with pytest.raises(AssignmentError):
                module.current_context_authority(orchestrator=turn[0], websocket=turn[1], chat_id=turn[2])
        with module.use_context_authority(lease):
            callback = asyncio.create_task(delayed())
        release.set()
        await callback


async def test_verification_timeout_and_adapter_failure_fail_closed(human, bound, fixture, monkeypatch):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        original = turn[3].origin.binding.adapter.run_in_transaction
        async def failed(*_args, **_kwargs):
            raise TimeoutError("private-database-diagnostic")
        with module.use_context_authority(lease):
            monkeypatch.setattr(turn[3].origin.binding.adapter, "run_in_transaction", failed)
            with pytest.raises(AssignmentError) as refused:
                await verify(lease, turn)
            assert "private-database-diagnostic" not in str(refused.value)
            monkeypatch.setattr(turn[3].origin.binding.adapter, "run_in_transaction", original)
            monkeypatch.setattr(module, "_VERIFY_SECONDS", 0)
            with pytest.raises(AssignmentError):
                await verify(lease, turn)


async def test_factory_closes_provisional_lease_when_verification_is_cancelled(human, bound, fixture, monkeypatch):
    async with admitted_turn(human, bound, fixture) as turn:
        captured = []
        async def cancelled(self, **_kwargs):
            captured.append(self)
            raise asyncio.CancelledError
        monkeypatch.setattr(module.ContextAuthorityLease, "verify", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await acquire(turn)
        assert captured[0]._closed is True


async def test_expired_original_identity_never_adopts_current_session(human, bound, fixture):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        with module.use_context_authority(lease):
            object.__setattr__(turn[3].origin, "expires_at", turn[3].origin.expires_at - timedelta(days=1))
            with pytest.raises(AssignmentError):
                await asyncio.create_task(verify(lease, turn))


@pytest.mark.parametrize("loss", ["none", "registration", "owner", "generation", "closing", "pending"])
async def test_foreground_binding_stays_current_in_child_callbacks(human, bound, fixture, monkeypatch, loss):
    async with admitted_turn(human, bound, fixture) as turn:
        host, _, chat, parent = turn
        socket = object()
        registration = {"sub": fixture[1], "_raw_token": parent.origin.token}
        generation = str(uuid4())
        connection = SimpleNamespace(websocket=socket, registered=True, closing=False,
                                     work_registrations_pending=0, connection_generation=generation)
        host.ui_sessions = {socket: registration}
        host._connection_contexts = {id(socket): connection}
        foreground = guidance._ForegroundSocketBinding(socket, connection, registration,
                                                        dict(registration), generation)
        binding = replace(parent, websocket=socket, foreground=foreground)
        with guidance.use_turn_guidance(binding, expected_orchestrator=host):
            foreground_turn = host, socket, chat, binding
            lease = await acquire(foreground_turn)
            with module.use_context_authority(lease):
                if loss == "registration":
                    host.ui_sessions[socket] = dict(registration)
                elif loss == "owner":
                    registration["sub"] = "foreign-owner"
                elif loss == "generation":
                    connection.connection_generation = str(uuid4())
                elif loss == "closing":
                    connection.closing = True
                elif loss == "pending":
                    connection.work_registrations_pending = 1
                if loss == "none":
                    assert (await asyncio.create_task(verify(lease, foreground_turn))).owner_id == fixture[1]
                else:
                    with pytest.raises(AssignmentError):
                        await asyncio.create_task(verify(lease, foreground_turn))


async def test_foreign_event_loop_cannot_reuse_original_read_lease(human, bound, fixture):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        with module.use_context_authority(lease):
            def foreign_loop():
                with pytest.raises(AssignmentError):
                    asyncio.run(verify(lease, turn))
            await asyncio.to_thread(foreign_loop)


async def test_completed_read_worker_cannot_publish_after_verification_deadline(human, bound, fixture, monkeypatch):
    async with admitted_turn(human, bound, fixture) as turn:
        lease = await acquire(turn)
        adapter = turn[3].origin.binding.adapter
        original = adapter.run_in_transaction
        ticks = [module.time.monotonic()]
        async def overdue(*args, **kwargs):
            value = await original(*args, **kwargs)
            ticks[0] += module._VERIFY_SECONDS + 1
            return value
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
        monkeypatch.setattr(adapter, "run_in_transaction", overdue)
        with module.use_context_authority(lease):
            with pytest.raises(AssignmentError):
                await asyncio.create_task(verify(lease, turn))
