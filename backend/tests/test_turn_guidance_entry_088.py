"""Entry compatibility before model execution, with real admission and guidance.

The ordinary slash expander is the stopping boundary. Its explicit failure is
not evidence of a successful model, speech, or completed conversation result.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

from orchestrator import orchestrator as hub
from orchestrator.user_skill_catalog import SkillCatalogError
from orchestrator.slash_commands import expand_message as original_expand_message
from orchestrator.work_admission import OperationOwner, OwnerScope
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_turn_guidance_ingress_088 import (
    guidance_turn as guidance_turn, metadata as metadata, ingress as ingress,
    surface as surface, command as command, context as context, fixture as fixture,
    runtime as runtime, service as service, signing_key as signing_key,
    registered, send, terminal,
)
from tests.test_turn_guidance_voice_088 import accepted_voice

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('loss', ['none', 'credential', 'registration'])
async def test_first_send_binds_new_chat_before_guidance_without_adopting_identity(
    guidance_turn, runtime, fixture, monkeypatch, loss,
):
    state, _, observed = guidance_turn
    await registered(state)
    created = str(uuid4())
    entered, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    def create(*, user_id):
        assert user_id == fixture[1]
        loop.call_soon_threadsafe(entered.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
        with runtime.transaction() as tx:
            tx.execute('INSERT INTO chats (id,user_id,title,created_at,updated_at) VALUES (%s,%s,%s,1,1)',
                       (created,user_id,'First send'))
        return created

    monkeypatch.setattr(state.orch.history,'create_chat',create)
    generation = send(state,'chat_message',message='/weekly first send')
    try:
        await asyncio.wait_for(entered.wait(),5)
        if loss == 'credential':
            await asyncio.to_thread(replace_session_record,runtime,get_session_record(runtime,fixture[2]))
        elif loss == 'registration':
            state.register()
            await asyncio.wait_for(state.registrations.get(),5)
        release.set()
        result = await terminal(state)
        assert result['state']=='failed'
        frame = next(row for row in state.frames if str(row.request_generation)==generation)
        assert frame.chat_id is None
        assert any(row.get('type')=='chat_created' and row['payload']['chat_id']==created
                   for row in state.socket.payloads())
        assert len(observed)==(1 if loss=='none' else 0)
        if observed:
            assert 'exact original weekly outline' in observed[0]
    finally:
        release.set()


@pytest.mark.parametrize('backend',['client_local','llm_factory'])
@pytest.mark.parametrize('loss',['none','credential'])
async def test_proof_admitted_voice_enters_real_chat_wrapper_and_current_catalog(
    guidance_turn,runtime,fixture,monkeypatch,backend,loss,
):
    state,binding,voice = await accepted_voice(guidance_turn,runtime,fixture,backend,monkeypatch)
    orch = state.orch
    operation = binding.operations[0]
    turn = binding.voice.turn
    dispatch = state.guidance_voice_dispatch
    operation_context={'operation_kind':'voice_chat_message','operation':operation.record,
        'owner':OperationOwner(OwnerScope.USER,fixture[1],None),
        'execution_fence':operation.fence,'guidance_origin':binding.origin}
    token=hub._CONNECTION_OPERATION_CONTEXT.set(operation_context)
    async def available(*_a,**_k):
        return None
    # Recognition/proof, operation claim and acceptance are actual above. This
    # entry probe does not run a provider or re-publish the already accepted UI.
    monkeypatch.setattr(orch,'_resolve_llm_client_for',available)
    monkeypatch.setattr(orch,'_deliver_committed_conversation_snapshot',available)
    monkeypatch.setattr(orch,'_broadcast_voice_ack',available)
    monkeypatch.setattr(orch,'_broadcast_voice_turn_state',available)
    orch.voice_services.coordinator=type('NoMedia',(),{'emit_transcript_accepted':staticmethod(available)})()
    orch.voice_services.start_turn_announcements=available
    orch._voice_ack_tasks=set()
    from orchestrator.history import ConversationCommitRepository
    orch.conversation_commits=ConversationCommitRepository(plane_runtime=runtime,
        plane_repositories=runtime.repositories,operation_coordinator=orch.work_admission)
    try:
        if loss=='credential':
            await asyncio.to_thread(replace_session_record,runtime,get_session_record(runtime,fixture[2]))
        with pytest.raises(SkillCatalogError):
            await orch._serialized_chat(state.socket,'/weekly original voice',binding.chat_id,None,
                user_id=fixture[1],operation_context=operation_context,voice_dispatch=dispatch)
        assert len(guidance_turn[2])==(1 if loss=='none' else 0)
        assert voice.get_turn(user_id=fixture[1],turn_id=turn.turn_id).accepted_at is not None
        assert not any(row.get('type')=='ui_render' and 'original weekly outline' in str(row)
                       for row in state.socket.payloads())
    finally:
        hub._CONNECTION_OPERATION_CONTEXT.reset(token)
        binding.origin.close()
        if orch._voice_ack_tasks:
            await asyncio.gather(*orch._voice_ack_tasks)


async def test_foreground_registration_replacement_after_binding_refuses_guidance(
    guidance_turn,monkeypatch,
):
    state,chat,observed=guidance_turn
    await registered(state)
    entered,release=asyncio.Event(),asyncio.Event()
    actual=state.orch._begin_conversation_publication
    async def hold(*args,**kwargs):
        entered.set()
        await release.wait()
        return await actual(*args,**kwargs)
    monkeypatch.setattr(state.orch,'_begin_conversation_publication',hold)
    send(state,'chat_message',message='/weekly replacement must refuse',chat_id=chat)
    try:
        await asyncio.wait_for(entered.wait(),5)
        await registered(state)
        release.set()
        assert (await terminal(state))['state']=='failed'
        assert observed==[]
    finally:
        release.set()


@pytest.mark.parametrize('turn_class',['parser_replay','draft_self_test','scheduled_job'])
@pytest.mark.parametrize('loss',['none','grant'])
async def test_actual_machine_chat_wrapper_uses_original_derived_guidance(
    guidance_turn,runtime,fixture,monkeypatch,turn_class,loss,
):
    from orchestrator import offline_grant, turn_guidance_authority as guidance
    from orchestrator.async_tasks import VirtualWebSocket
    from orchestrator.chain_authority import MachineAuthority, MachineTurnAuthority
    from scheduler.store import ScheduledJobStore
    from tests.helpers.session_consent_088 import consent_from_store
    state,chat,observed=guidance_turn
    orch=state.orch
    key=Fernet.generate_key().decode()
    monkeypatch.setenv('OFFLINE_GRANT_ENC_KEY',key)
    monkeypatch.setattr(offline_grant,'OFFLINE_GRANT_ENC_KEY',key)
    orch.offline_grants=offline_grant.OfflineGrantStore(plane_runtime=runtime)
    grant=await asyncio.to_thread(orch.offline_grants.capture,fixture[1],
        consent_from_store(fixture[0],fixture[1],fixture[2]))
    minted=[]
    async def mint(grant_id,*,user_id):
        assert grant_id==grant and user_id==fixture[1]
        minted.append(grant_id)
        return fixture[3]()
    monkeypatch.setattr(orch.offline_grants,'mint_access_token',mint)
    authority=await MachineTurnAuthority(orch,orch.offline_grants).derive(user_id=fixture[1],
        agent_id=None,consented_scopes=[] if turn_class=='scheduled_job' else None,
        grant_id=grant,turn_class=turn_class)
    assert type(authority) is MachineAuthority
    socket=VirtualWebSocket(SimpleNamespace(_operation=None,outputs=[]))
    orch._bind_machine_turn(socket,authority)
    store=attempt=None
    if turn_class=='scheduled_job':
        store=ScheduledJobStore(plane_runtime=runtime,coordinator=orch.work_admission)
        job=store.create_job(fixture[1],name='Actual guidance entry',instruction='Read current guidance',
            schedule_kind='interval',schedule_expr='1h',timezone='UTC',consented_scopes=[],
            agent_id=None,target_chat_id=None,next_run_at=int(time.time()*1000)-1000,offline_grant_id=grant)
        claim=store.materialize_and_claim_due('entry-guidance',limit=1,lease_seconds=60)[0]
        attempt=store.allocate_attempt(claim)
        if attempt.execution_fence is None:
            attempt=store.claim_attempt_execution(attempt)
        attempt=store.start_attempt(attempt)
        assert attempt.job['id']==job['id']
        socket._guidance_scheduled_attempt=(attempt,store)
    captured=[]
    actual=guidance.bind_machine_guidance
    async def capture(**kwargs):
        binding=await actual(**kwargs)
        captured.append(binding)
        return binding
    monkeypatch.setattr(guidance,'bind_machine_guidance',capture)
    try:
        if loss=='grant':
            await asyncio.to_thread(orch.offline_grants.revoke_for_user,fixture[1])
        with pytest.raises(SkillCatalogError):
            await orch.handle_chat_message(socket,'/weekly original machine',chat,user_id=fixture[1])
        assert len(observed)==(1 if loss=='none' else 0)
        assert len(minted)==1
        assert all(value.closed and value.origin.closed for value in captured)
    finally:
        orch._unbind_machine_turn(socket)
        await socket.close()


async def test_remote_voice_ingress_captures_original_human_before_admission_queue(
    guidance_turn,runtime,fixture,monkeypatch,
):
    state,binding,_=await accepted_voice(guidance_turn,runtime,fixture,'llm_factory',monkeypatch)
    turn=binding.voice.turn
    entered,release=asyncio.Event(),asyncio.Event()
    actual=state.orch._call_work_admission
    async def hold(callback,*args,**kwargs):
        if getattr(callback,'__name__','')=='_submit_connection_batch':
            entered.set()
            await release.wait()
        return await actual(callback,*args,**kwargs)
    monkeypatch.setattr(state.orch,'_call_work_admission',hold)
    # This verifies human capture, not voice admission. The existing recognized
    # turn above is real; its UI payload shape must select the original human
    # before any operation queue wait or later proof-validation dispatch.
    generation=send(state,'chat_message',message='/weekly queued voice',chat_id=binding.chat_id,
        voice_origin={'session_id':turn.session_id,'generation':turn.session_generation,
            'media_grant_revision':turn.media_grant_revision,'turn_id':turn.turn_id,
            'client_turn_id':turn.client_turn_id})
    try:
        await asyncio.wait_for(entered.wait(),5)
        frame=next(row for row in state.frames if str(row.request_generation)==generation)
        assert frame.operation_kind=='voice_chat_message'
        assert frame.guidance_origin is not None
        assert frame.guidance_origin.owner_id==fixture[1]
        assert frame.guidance_origin.credential.session_id==fixture[2]
        assert not frame.guidance_origin.closed
    finally:
        # Discard this deliberately held capture; no second voice dispatch or
        # accepted transcript is authorized by this ingress-only assertion.
        state.socket.closed=True
        state.socket.disconnect()
        release.set()
        binding.origin.close()


@pytest.mark.parametrize('display',[None,'Original display'])
async def test_expanded_guidance_enters_normal_message_before_provider_gate(
    guidance_turn,monkeypatch,display,
):
    from orchestrator import onboarding_submit, slash_commands
    from llm_config.types import LLMUnavailable
    state,chat,_=guidance_turn
    await registered(state)
    inspected=[]
    original=onboarding_submit.is_onboarding_submit
    def inspect(message):
        inspected.append(message)
        return original(message)
    async def stop(*_a,**_k):
        raise SkillCatalogError('qualification_stop_before_provider',503)
    monkeypatch.setattr(slash_commands,'expand_message',original_expand_message)
    monkeypatch.setattr(onboarding_submit,'is_onboarding_submit',inspect)
    monkeypatch.setattr(state.orch,'_resolve_llm_client_for',stop)
    state.orch._LLMUnavailable=LLMUnavailable
    send(state,'chat_message',message='/weekly ordinary expansion',chat_id=chat,display_message=display)
    assert (await terminal(state))['state']=='failed'
    assert len(inspected)==1 and 'exact original weekly outline' in inspected[0]
    assert 'ordinary expansion' in inspected[0]


@pytest.mark.parametrize('failure',['credential','history','submit','already_terminal','ack','feature_off'])
async def test_actual_async_handoff_failure_preserves_origin_custody(
    guidance_turn,runtime,fixture,monkeypatch,failure,
):
    from orchestrator import turn_guidance_authority as guidance
    from shared.feature_flags import flags
    state,chat,observed=guidance_turn
    await registered(state)
    origins=[]
    capture=guidance.capture_turn_guidance_from_human
    async def captured(*args,**kwargs):
        value=await capture(*args,**kwargs)
        origins.append(value)
        return value
    monkeypatch.setattr(guidance,'capture_turn_guidance_from_human',captured)
    manager=state.orch.async_task_manager
    actual_submit=manager.submit
    if failure=='credential':
        actual_dispatch=state.orch._dispatch_async_chat
        async def invalid(*args,**kwargs):
            await asyncio.to_thread(replace_session_record,runtime,get_session_record(runtime,fixture[2]))
            return await actual_dispatch(*args,**kwargs)
        monkeypatch.setattr(state.orch,'_dispatch_async_chat',invalid)
    elif failure=='history':
        monkeypatch.setattr(state.orch.history,'get_conversation_record',lambda *_a,**_k:None)
    elif failure in {'submit','already_terminal'}:
        async def submit(*args,**kwargs):
            if failure=='submit':
                raise RuntimeError('controlled admission failure')
            task=await actual_submit(*args,**kwargs)
            await asyncio.wait_for(asyncio.shield(task.asyncio_task),5)
            return task
        monkeypatch.setattr(manager,'submit',submit)
    elif failure=='feature_off':
        monkeypatch.setitem(flags._flags,'user_skills',False)
    actual_send=state.orch._safe_send
    entered,release=asyncio.Event(),asyncio.Event()
    if failure=='ack':
        handle=state.orch.handle_chat_message
        async def hold(*args,**kwargs):
            entered.set()
            await release.wait()
            return await handle(*args,**kwargs)
        monkeypatch.setattr(state.orch,'handle_chat_message',hold)
        async def send_failed(socket,data):
            if json.loads(data).get('type')=='task_started':
                assert origins and not origins[0].closed
                raise RuntimeError('controlled acknowledgement failure')
            return await actual_send(socket,data)
        monkeypatch.setattr(state.orch,'_safe_send',send_failed)
    send(state,'chat_message',message='/weekly async custody',chat_id=chat,async_mode=True)
    await terminal(state)
    if failure=='ack':
        await asyncio.wait_for(entered.wait(),5)
        assert not origins[0].closed
        release.set()
    for task in manager._tasks.values():
        async with asyncio.timeout(5):
            while task.asyncio_task is None:
                await asyncio.sleep(.01)
            await asyncio.shield(task.asyncio_task)
    await manager.drain(timeout_seconds=5)
    assert all(origin.closed for origin in origins)
    if failure in {'credential','history','submit'}:
        assert not manager._tasks and not observed
    else:
        assert len(manager._tasks)==1
        task=next(iter(manager._tasks.values()))
        assert task._canonical_status().value=='failed'
        assert getattr(task,'_guidance_origin',None) is None
        assert len(observed)==1
    assert len(origins)==(0 if failure in {'credential','feature_off'} else 1)
