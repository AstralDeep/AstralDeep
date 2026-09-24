"""Tests for machine and scheduled guidance in orchestrator/turn_guidance_authority.py:
derived grants are read without reminting, changed or expired sources refuse
guidance, and expiry is checked against the database clock, not a lagging host.
"""

import asyncio
from datetime import timedelta
import time
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

from orchestrator import offline_grant, turn_guidance_authority as module
from orchestrator.async_tasks import VirtualWebSocket
from orchestrator.chain_authority import MachineAuthority, MachineTurnAuthority
from orchestrator.orchestrator import Orchestrator
from orchestrator.work_admission import StaleExecutionFenceError, WorkAdmissionCoordinator
from persistent_agents.models import AssignmentError
from scheduler.store import ScheduledJobStore
from tests.helpers.session_consent_088 import consent_from_store
from tests.test_human_request_authority_088 import selected
from tests.test_user_skill_facade_088 import (
    bound as bound, catalog as catalog, command, fixture as fixture, human as human,
    runtime as runtime, service as service, signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def machine(catalog, human, bound, fixture, runtime, monkeypatch):
    orch = human[2]
    orch.ui_sessions = {}
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('OFFLINE_GRANT_ENC_KEY', key)
    monkeypatch.setattr(offline_grant, 'OFFLINE_GRANT_ENC_KEY', key)
    orch.offline_grants = offline_grant.OfflineGrantStore(plane_runtime=runtime)
    grant = await asyncio.to_thread(orch.offline_grants.capture, fixture[1],
        consent_from_store(fixture[0], fixture[1], fixture[2]))
    minted = []
    async def mint(grant_id, *, user_id):
        assert grant_id == grant and user_id == fixture[1]
        minted.append(grant_id)
        return fixture[3]()
    monkeypatch.setattr(orch.offline_grants, 'mint_access_token', mint)
    caller = await selected(human, bound, fixture)
    await catalog.save(caller=caller, **command())
    orch.work_admission = await asyncio.to_thread(WorkAdmissionCoordinator.from_plane,
        plane_runtime=runtime, slot_lease=timedelta(seconds=60))
    return orch, grant, minted


async def bind(machine, fixture, runtime, turn_class):
    orch, grant, minted = machine
    authority = await MachineTurnAuthority(orch, orch.offline_grants).derive(
        user_id=fixture[1], agent_id=None, consented_scopes=[] if turn_class == 'scheduled_job' else None,
        grant_id=grant, turn_class=turn_class)
    assert type(authority) is MachineAuthority and len(minted) == 1
    chat = str(uuid4())
    store = attempt = None
    if turn_class == 'scheduled_job':
        store = ScheduledJobStore(plane_runtime=runtime, coordinator=orch.work_admission)
        job = store.create_job(fixture[1], name='Guidance timer', instruction='Read current owner guidance',
            schedule_kind='interval', schedule_expr='1h', timezone='UTC', consented_scopes=[],
            agent_id=None, target_chat_id=None, next_run_at=int(time.time() * 1000) - 1000,
            offline_grant_id=grant)
        claim = store.materialize_and_claim_due('guidance-test', limit=1, lease_seconds=60)[0]
        attempt = store.allocate_attempt(claim)
        if attempt.execution_fence is None:
            attempt = store.claim_attempt_execution(attempt)
        attempt = store.start_attempt(attempt)
        assert attempt.job['id'] == job['id']
        chat = job['id']
    socket = VirtualWebSocket(SimpleNamespace(_operation=None, outputs=[]))
    Orchestrator._bind_machine_turn(orch, socket, authority)
    binding = await module.bind_machine_guidance(expected_orchestrator=orch, websocket=socket,
        authority=authority, chat_id=chat, scheduled_attempt=attempt, scheduled_store=store)
    return binding, authority, store, attempt


@pytest.mark.parametrize('turn_class', ['parser_replay', 'draft_self_test', 'scheduled_job'])
async def test_actual_machine_derivation_reads_original_grant_without_remint(
    machine, catalog, fixture, runtime, turn_class,
):
    binding, authority, _, _ = await bind(machine, fixture, runtime, turn_class)
    orch = machine[0]
    try:
        with module.use_turn_guidance(binding, expected_orchestrator=orch):
            reader = await module.acquire_turn_guidance_reader(expected_orchestrator=orch,
                websocket=binding.websocket, chat_id=binding.chat_id)
            try:
                values = await catalog.list(caller=reader)
                assert len(values) == 1 and values[0].instructions == 'Use the exact weekly outline.'
                assert authority.access_token not in repr(binding) and authority.access_token not in repr(reader)
                with pytest.raises(AssignmentError):
                    await catalog.save(caller=reader, **command())
            finally:
                reader.close()
        assert len(machine[2]) == 1
    finally:
        binding.origin.close()
        Orchestrator._unbind_machine_turn(orch, binding.websocket)


@pytest.mark.parametrize('loss', ['grant', 'grant_expired', 'registration', 'authority', 'store', 'token', 'socket', 'boundary'])
async def test_machine_guidance_refuses_changed_original_source(machine, catalog, fixture, runtime, loss):
    binding, _, _, _ = await bind(machine, fixture, runtime, 'parser_replay')
    orch = machine[0]
    try:
        with module.use_turn_guidance(binding, expected_orchestrator=orch):
            if loss == 'grant':
                orch.offline_grants.revoke_for_user(fixture[1])
            elif loss == 'grant_expired':
                with runtime.transaction() as tx:
                    tx.execute('UPDATE user_offline_grant SET expires_at=0 WHERE id=%s', (machine[1],))
            elif loss == 'registration':
                orch.ui_sessions[binding.websocket] = dict(orch.ui_sessions[binding.websocket])
            elif loss == 'authority':
                binding.websocket._guidance_machine_authority = None
            elif loss == 'store':
                orch.offline_grants = offline_grant.OfflineGrantStore(plane_runtime=runtime)
            elif loss == 'token':
                orch.ui_sessions[binding.websocket]['_raw_token'] = fixture[3](sub='different-owner')
            elif loss == 'boundary':
                orch.human_request_boundary = None
            else:
                await binding.websocket.close()
            with pytest.raises(AssignmentError):
                await module.acquire_turn_guidance_reader(expected_orchestrator=orch,
                    websocket=binding.websocket, chat_id=binding.chat_id)
        assert len(machine[2]) == 1
    finally:
        binding.origin.close()
        Orchestrator._unbind_machine_turn(orch, binding.websocket)


@pytest.mark.parametrize('loss', ['lease', 'generation', 'operation', 'job'])
async def test_scheduled_guidance_keeps_exact_running_attempt(machine, fixture, runtime, loss):
    binding, _, _, attempt = await bind(machine, fixture, runtime, 'scheduled_job')
    orch = machine[0]
    try:
        with module.use_turn_guidance(binding, expected_orchestrator=orch):
            with runtime.transaction() as tx:
                if loss == 'lease':
                    tx.execute("UPDATE scheduled_occurrence SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE occurrence_id=%s", (str(attempt.claim.occurrence_id),))
                elif loss == 'generation':
                    tx.execute('UPDATE scheduled_occurrence SET claim_generation=claim_generation+1 WHERE occurrence_id=%s', (str(attempt.claim.occurrence_id),))
                elif loss == 'operation':
                    tx.execute("UPDATE operation_record SET execution_generation=execution_generation+1 WHERE operation_id=%s", (str(attempt.operation_id),))
                else:
                    attempt.job['offline_grant_id'] = str(uuid4())
            with pytest.raises(AssignmentError):
                await module.acquire_turn_guidance_reader(expected_orchestrator=orch,
                    websocket=binding.websocket, chat_id=binding.chat_id)
    finally:
        binding.origin.close()
        Orchestrator._unbind_machine_turn(orch, binding.websocket)


async def test_expired_operation_slots_cannot_authorize_guidance(machine, fixture, runtime):
    binding, _, _, attempt = await bind(machine, fixture, runtime, 'scheduled_job')
    orch = machine[0]
    try:
        with module.use_turn_guidance(binding, expected_orchestrator=orch):
            with runtime.transaction() as tx:
                tx.execute("UPDATE operation_admission_slot SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=%s", (str(attempt.operation_id),))
            with pytest.raises(AssignmentError):
                await module.acquire_turn_guidance_reader(expected_orchestrator=orch,
                    websocket=binding.websocket,chat_id=binding.chat_id)
    finally:
        binding.origin.close()
        Orchestrator._unbind_machine_turn(orch,binding.websocket)


async def test_durable_read_refuses_missing_outer_transaction(machine,fixture,runtime):
    binding,_,_,attempt=await bind(machine,fixture,runtime,'scheduled_job')
    orch=machine[0]
    try:
        with pytest.raises(StaleExecutionFenceError):
            orch.work_admission.assert_current_execution_lease(attempt.execution_fence,transaction=None)
        with module.use_turn_guidance(binding,expected_orchestrator=orch):
            reader=await module.acquire_turn_guidance_reader(expected_orchestrator=orch,
                websocket=binding.websocket,chat_id=binding.chat_id)
            reader.close()
    finally:
        binding.origin.close()
        Orchestrator._unbind_machine_turn(orch,binding.websocket)


async def test_machine_capture_refuses_missing_original_host_boundary(machine,fixture,runtime):
    binding,authority,_,_=await bind(machine,fixture,runtime,'parser_replay')
    orch=machine[0]
    try:
        orch.human_request_boundary=None
        with pytest.raises(AssignmentError):
            await module.bind_machine_guidance(expected_orchestrator=orch,
                websocket=binding.websocket,authority=authority,chat_id=binding.chat_id)
        assert len(machine[2])==1
    finally:
        binding.origin.close()
        Orchestrator._unbind_machine_turn(orch,binding.websocket)


async def test_grant_expiry_uses_database_clock_despite_lagging_host(machine, fixture, runtime, monkeypatch):
    from datetime import datetime, timezone
    with runtime.transaction() as tx:
        tx.execute('UPDATE user_offline_grant SET expires_at=%s WHERE id=%s', (int(time.time()*1000)+1200,machine[1]))
    binding, _, _, _ = await bind(machine, fixture, runtime, 'parser_replay')
    orch = machine[0]
    class LaggingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz or timezone.utc)-timedelta(days=1)
    monkeypatch.setattr(module,'datetime',LaggingClock)
    try:
        with module.use_turn_guidance(binding,expected_orchestrator=orch):
            await asyncio.sleep(1.3)
            with pytest.raises(AssignmentError):
                await module.acquire_turn_guidance_reader(expected_orchestrator=orch,
                    websocket=binding.websocket,chat_id=binding.chat_id)
    finally:
        binding.origin.close()
        Orchestrator._unbind_machine_turn(orch,binding.websocket)
