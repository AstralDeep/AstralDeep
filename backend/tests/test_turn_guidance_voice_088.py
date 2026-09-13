"""Proof-admitted local/remote voice guidance over actual current Plane rows.

Only the guidance boundary is exercised: real recognition/proof or local
registry, operation admission and message acceptance precede a catalog read.
No microphone, external issuer, model or speech worker is contacted.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from orchestrator import human_request_authority as humans, turn_guidance_authority as module
from orchestrator.history import ConversationCommitRepository
from orchestrator.orchestrator import _VoiceDispatchContext
from orchestrator.voice_control_binding import ClientLocalBindingRegistry
from orchestrator.voice_sessions import (
    CreateSession, LocalTranscriptSubmission, RecognitionBinding, VoiceSessionRepository,
)
from orchestrator.work_admission import AdmissionClass, OperationOwner, OperationRequest, OwnerScope
from persistent_agents.models import AssignmentError
from shared.protocol import VoiceLocalFinal, VoiceLocalRecognitionStarted
from tests import test_conversation_publication_voice_065 as remote
from tests.test_turn_guidance_ingress_088 import (
    guidance_turn as guidance_turn, metadata as metadata, ingress as ingress,
    surface as surface, command as command, context as context, fixture as fixture,
    runtime as runtime, service as service, signing_key as signing_key, registered,
)

pytestmark = pytest.mark.asyncio


async def accepted_voice(guidance_turn, runtime, fixture, backend, monkeypatch):
    state, chat, _ = guidance_turn
    orch, owner = state.orch, fixture[1]
    await registered(state)
    now = datetime.now(timezone.utc)
    voice = VoiceSessionRepository(plane_runtime=runtime, plane_repositories=runtime.repositories)
    with runtime.transaction() as tx:
        tx.execute("INSERT INTO chats (id,user_id,title,created_at,updated_at) VALUES (%s,%s, 'Guidance voice',1,1)", (chat,owner))
    local = backend == 'client_local'
    request = CreateSession(user_id=owner, activation_id=str(uuid4()), device_id=str(uuid4()),
        device_kind='web', transport='client_local' if local else 'livekit', speech_backend=backend,
        room_name=None if local else 'test-room', participant_identity=None if local else 'test-client',
        visible_chat_id=chat, owner_connection_generation=state.connection_generation,
        control_binding_id=str(uuid4()), control_binding_expires_at=now+timedelta(minutes=5),
        lease_expires_at=now+timedelta(minutes=1), media_grant_nonce_hash=None if local else os.urandom(32),
        media_grant_issued_at=None if local else now,
        media_grant_expires_at=None if local else now+timedelta(minutes=5))
    session = voice.create_session(request, now=now).session
    await voice.claim_control_lease(user_id=owner, session_id=session.session_id,
        generation=1, owner_id='publication-test', now=now)
    if not local:
        voice.assign_worker(user_id=owner, session_id=session.session_id, expected_generation=1,
            assignment_id=str(uuid4()), worker_identity='test-worker', issued_at=now,
            expires_at=now+timedelta(minutes=5), now=now)
    voice.apply_chat_context(user_id=owner, session_id=session.session_id, expected_generation=1,
        expected_media_grant_revision=1, control_owner_id='publication-test', visible_chat_id=chat,
        chat_context_revision=1, now=now)
    session = voice.mark_session_active(user_id=owner, session_id=session.session_id,
        expected_generation=1, expected_media_grant_revision=1, now=now)
    if local:
        start = VoiceLocalRecognitionStarted(device_id=request.device_id,
            connection_generation=state.connection_generation, session_id=session.session_id,
            generation=1, speech_revision=1, client_turn_id=str(uuid4()), chat_id=chat,
            chat_context_revision=1, recognition_sequence=1)
        registry = ClientLocalBindingRegistry()
        reservation = registry.reserve_turn(socket_id=id(state.socket), current_socket_id=id(state.socket),
            user_id=owner, claims=SimpleNamespace(subject=owner, device_id=request.device_id,
                connection_generation=state.connection_generation, binding_id=request.control_binding_id,
                expires_at=request.control_binding_expires_at), session=session, frame=start, now=now)
        turn = voice.bind_recognition_turn(RecognitionBinding(owner,session.session_id,1,1,
            start.client_turn_id,chat,1,0,'publication-test'), now=now).turn
        authority = registry.finalize_turn(reservation=reservation, turn=turn, now=now)
        text = '/weekly original voice'
        frame = VoiceLocalFinal(device_id=request.device_id, connection_generation=state.connection_generation,
            session_id=session.session_id,generation=1,speech_revision=1,client_turn_id=turn.client_turn_id,
            turn_id=turn.turn_id,submission_id=turn.submission_id,request_generation=turn.request_generation,
            chat_id=chat,chat_context_revision=1,recognition_sequence=1,recognized_locale='en-US',
            text=text,text_digest_sha256=hashlib.sha256(text.encode()).hexdigest())
        canonical, replayed = registry.verify_final(socket_id=id(state.socket),current_socket_id=id(state.socket),
            user_id=owner,frame=frame,now=now)
        assert not replayed
        admission = voice.admit_local_transcript(LocalTranscriptSubmission.from_authority(user_id=owner,
            authority=authority,expected_control_owner_id='publication-test',detected_language='en',
            canonical_text=canonical),now=now)
        message = json.loads(frame.to_json())
    else:
        admission = await asyncio.to_thread(remote._admit_turn, voice, session,
            text='/weekly original voice',now=now)
        message = {'type':'ui_event','action':'chat_message','submission_id':admission.turn.submission_id,
            'request_generation':admission.turn.request_generation,'connection_generation':state.connection_generation,
            'payload':{'voice_origin':{'turn_id':admission.turn.turn_id}}}
    pending = humans.capture_human_socket_request(orch.human_request_boundary, websocket=state.socket,
        context=orch._connection_contexts[id(state.socket)],message=message,purpose='voice_guidance')
    await pending.capture_session()
    origin = await module.capture_turn_guidance_from_human(await pending.authenticate(),expected_orchestrator=orch)
    pending.close()
    turn = admission.turn
    owner_scope = OperationOwner(OwnerScope.USER,owner,None)
    submitted = orch.work_admission.submit(OperationRequest(operation_kind='voice_chat_message',
        admission_class=AdmissionClass.VOICE_INTERACTIVE,owner=owner_scope,
        submission_id=UUID(turn.submission_id),idempotency_namespace='voice_chat_message',
        idempotency_key=turn.submission_id,normalized_input_digest='ab'*32,chat_id=chat,parent_operation_id=None,
        connection_generation=UUID(state.connection_generation),request_generation=UUID(turn.request_generation)))
    claim = orch.work_admission.claim_operation(AdmissionClass.VOICE_INTERACTIVE,submitted.operation_id)
    assert claim is not None
    commits = ConversationCommitRepository(plane_runtime=runtime,plane_repositories=runtime.repositories,
        operation_coordinator=orch.work_admission)
    def accept(**values):
        return voice.accept_transcript(user_id=owner,turn_id=turn.turn_id,message_id=values['message_id'],
            accepted_connection_generation=state.connection_generation,acceptance_commit_id=values['acceptance_commit_id'],
            operation_id=str(claim.operation.operation_id),now=now,result_commit_id=values['result_commit_id'],
            transaction=values['transaction'])
    await asyncio.to_thread(commits.accept_voice_turn,chat_id=chat,owner_user_id=owner,
        request_generation=turn.request_generation,result_request_generation=turn.result_request_generation,
        connection_generation=UUID(state.connection_generation),user_content=admission.canonical_text,
        operation_fence=claim.fence,operation_owner=owner_scope,accept_turn=accept)
    orch.voice_services = SimpleNamespace(repository=voice)
    dispatch = _VoiceDispatchContext(admission,state.connection_generation,object())
    context = {'operation_kind':'voice_chat_message','operation':claim.operation,
        'execution_fence':claim.fence,'guidance_origin':origin}
    binding = module.bind_voice_guidance(origin,expected_orchestrator=orch,websocket=state.socket,
        voice_dispatch=dispatch,operation_context=context,chat_id=chat)
    state.guidance_voice_dispatch = dispatch
    return state,binding,voice


@pytest.mark.parametrize('backend', ['client_local','llm_factory'])
@pytest.mark.parametrize('loss', ['none','voice_session','turn','credential','connection'])
async def test_accepted_voice_reads_only_current_original_authority(
    guidance_turn,runtime,fixture,monkeypatch,backend,loss,
):
    state,binding,voice = await accepted_voice(guidance_turn,runtime,fixture,backend,monkeypatch)
    try:
        with module.use_turn_guidance(binding,expected_orchestrator=state.orch):
            if loss != 'none':
                if loss == 'credential':
                    fixture[0].delete(fixture[2])
                elif loss == 'turn':
                    voice.terminalize_turn(user_id=fixture[1],turn_id=binding.voice.turn.turn_id,
                        terminal_kind='failed',result_commit_id=None,recap_source='none',
                        sensitivity='unknown',now=datetime.now(timezone.utc))
                with runtime.transaction() as tx:
                    if loss == 'voice_session':
                        tx.execute('UPDATE voice_session SET generation=generation+1 WHERE session_id=%s', (binding.voice.turn.session_id,))
                    elif loss == 'connection':
                        tx.execute('UPDATE voice_session SET owner_connection_generation=%s WHERE session_id=%s', (str(uuid4()),binding.voice.turn.session_id))
                with pytest.raises(AssignmentError):
                    await module.acquire_turn_guidance_reader(expected_orchestrator=state.orch,
                        websocket=state.socket,chat_id=binding.chat_id)
            else:
                reader = await module.acquire_turn_guidance_reader(expected_orchestrator=state.orch,
                    websocket=state.socket,chat_id=binding.chat_id)
                try:
                    values = await state.orch._user_skill_store.list(caller=reader)
                    assert len(values)==1 and values[0].instructions=='Use the exact original weekly outline.'
                finally:
                    reader.close()
    finally:
        binding.origin.close()


async def test_voice_clock_is_checked_in_database_after_lagging_host_observation(
    guidance_turn,runtime,fixture,monkeypatch,
):
    state,binding,_ = await accepted_voice(guidance_turn,runtime,fixture,'client_local',monkeypatch)
    class LaggingClock(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime.now(tz or timezone.utc)-timedelta(seconds=1)
    try:
        with module.use_turn_guidance(binding,expected_orchestrator=state.orch):
            with runtime.transaction() as tx:
                tx.execute("UPDATE voice_session SET lease_expires_at=clock_timestamp()+interval '300 milliseconds' WHERE session_id=%s", (binding.voice.turn.session_id,))
            monkeypatch.setattr(module,'datetime',LaggingClock)
            await asyncio.sleep(.4)
            with pytest.raises(AssignmentError):
                await module.acquire_turn_guidance_reader(expected_orchestrator=state.orch,
                    websocket=state.socket,chat_id=binding.chat_id)
    finally:
        binding.origin.close()


async def test_voice_reverse_order_writer_refuses_promptly_then_recovers(
    guidance_turn,runtime,fixture,monkeypatch,
):
    state,binding,_ = await accepted_voice(guidance_turn,runtime,fixture,'client_local',monkeypatch)
    try:
        with module.use_turn_guidance(binding,expected_orchestrator=state.orch):
            with runtime.transaction() as writer:
                writer.fetch_one('SELECT turn_id FROM voice_turn WHERE turn_id=%s FOR UPDATE', (binding.voice.turn.turn_id,))
                async with asyncio.timeout(2):
                    with pytest.raises(AssignmentError):
                        await module.acquire_turn_guidance_reader(expected_orchestrator=state.orch,
                            websocket=state.socket,chat_id=binding.chat_id)
                # The reader must release its session lock after refusal. A
                # normal turn→session writer can finish without a deadlock.
                runtime.repositories.history.sessions.bound_request_execution_waits(writer)
                writer.fetch_one('SELECT session_id FROM voice_session WHERE session_id=%s FOR UPDATE', (binding.voice.turn.session_id,))
            reader = await module.acquire_turn_guidance_reader(expected_orchestrator=state.orch,
                websocket=state.socket,chat_id=binding.chat_id)
            reader.close()
    finally:
        binding.origin.close()
