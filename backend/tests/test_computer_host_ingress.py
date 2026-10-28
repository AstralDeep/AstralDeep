"""Exercises authenticated computer-host frames through the real WebSocket ingress loop.
Current owner registries and session managers decide whether control frames can change state.
"""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import orchestrator as module
from agents.computer_use import mcp_tools
from agents.computer_use.mcp_server import MCPServer
from orchestrator.computer_hosts import ComputerHostRegistry
from orchestrator.computer_sessions import ComputerSessionManager
from shared.feature_flags import flags
from shared.protocol import MCPRequest
from tests.test_hitl_public_research import _dispatch
from tests.test_hitl_task_continuation import task_runtime  # noqa: F401
from tests.test_hitl_public_research import runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401
from tests.perf.test_runtime_reliability_060 import _MessageProbe, _orchestrator, _socket


_NATIVE = json.loads((Path(__file__).parent / 'fixtures/computer_host_control_wire.json').read_text())


def _native_frame(kind, *, session_id=None, request_id=None, legacy=False):
    field = 'old_text' if legacy else 'wire_text'
    wire = next(item[field] for item in _NATIVE['messages'] if item['kind'] == kind)
    if session_id is not None:
        wire = wire.replace(_NATIVE['session_id'], session_id)
    if request_id is not None:
        request = json.loads(wire)['payload']['request_id']
        wire = wire.replace(request, request_id)
    return wire


@asynccontextmanager
async def _ingress(state_runtime, monkeypatch, *, enabled=True):
    orch, _previous, effects, turns, proposals, outcome = state_runtime
    baseline = _orchestrator(module, _MessageProbe())
    for key, value in vars(baseline).items():
        if key not in vars(orch) and key != 'handle_ui_message':
            setattr(orch, key, value)
    websocket = _socket(module)
    websocket.user = 'owner-a'
    orch.ui_clients = [websocket]
    orch.ui_sessions = {websocket: {'sub': 'owner-a'}}
    orch._ws_active_chat = {id(websocket): 'chat-a'}
    orch._connection_contexts = {}
    orch._registered_events = {}
    orch.computer_hosts = ComputerHostRegistry(orch)
    orch.computer_sessions = ComputerSessionManager(orch, orch.computer_hosts)
    orch._tunnel_ingress_over_cap = lambda _owner: False
    monkeypatch.setitem(flags._flags, 'computer_use', enabled)
    generation = _NATIVE['connection_generation']
    registered, completed = asyncio.Event(), asyncio.Event()
    barriers, sent = [], []
    on_send = [None]

    async def send(socket, raw):
        socket.sent.append(raw)
        value = json.loads(raw)
        sent.append(value)
        if value.get('type') == 'pong' and barriers:
            barriers.pop(0).set()
        if on_send[0] is not None:
            await on_send[0](value)
        return True

    async def handle(socket, raw):
        value = json.loads(raw)
        if value.get('type') == 'register_ui':
            orch._registered_events[id(socket)].set()
            registered.set()
            return
        await module.Orchestrator.handle_ui_message(orch, socket, raw)
        if value.get('action') == 'authorize_action':
            completed.set()

    async def feed(raw=None):
        barrier = asyncio.Event()
        barriers.append(barrier)
        if raw is not None:
            websocket.feed(raw)
        websocket.feed(json.dumps({'type': 'ping'}))
        await asyncio.wait_for(barrier.wait(), 5)

    orch._safe_send = send
    orch.handle_ui_message = handle
    serving = asyncio.create_task(orch._serve_ui_frames(websocket, websocket.receive_text))
    try:
        websocket.feed(_NATIVE['registration_text'])
        await asyncio.wait_for(registered.wait(), 5)
        await feed()
        yield SimpleNamespace(orch=orch, websocket=websocket, generation=generation, feed=feed,
                              sent=sent, effects=effects, turns=turns, proposals=proposals, outcome=outcome,
                              completed=completed, on_send=on_send)
    finally:
        for session in orch.computer_sessions.live_for_owner('owner-a'):
            await orch.computer_sessions.end(session, 'user_stop')
        websocket.disconnect()
        try:
            await serving
        except module.WebSocketDisconnect:
            pass


async def test_native_announce_registers_only_the_authenticated_current_host(task_runtime, monkeypatch):  # noqa: F811
    async with _ingress(task_runtime, monkeypatch) as state:
        await state.feed(_native_frame('announce'))
        host = state.orch.computer_hosts.get('owner-a', _NATIVE['host_id'])
        assert host is not None
        assert host.websocket is state.websocket and host.owner_sub == 'owner-a'
        assert host.platform == 'macos' and host.name == 'Synthetic Mac'
        assert state.orch.computer_hosts.get('owner-b', _NATIVE['host_id']) is None
        assert not state.effects
        assert not any('invalid_input' in json.dumps(frame) for frame in state.sent)


async def test_native_announce_payload_cannot_select_another_owner(task_runtime, monkeypatch):  # noqa: F811
    async with _ingress(task_runtime, monkeypatch) as state:
        frame = json.loads(_native_frame('announce'))
        frame['payload']['owner_sub'] = 'owner-b'
        await state.feed(json.dumps(frame))
        assert state.orch.computer_hosts.get('owner-a', _NATIVE['host_id']) is not None
        assert state.orch.computer_hosts.get('owner-b', _NATIVE['host_id']) is None
        assert not state.effects


@pytest.mark.parametrize('legacy', [False, True])
async def test_flag_off_native_announce_is_inert_without_invalid_input(task_runtime, monkeypatch, legacy):  # noqa: F811
    async with _ingress(task_runtime, monkeypatch, enabled=False) as state:
        await state.feed(_native_frame('announce', legacy=legacy))
        assert state.orch.computer_hosts.host_for_socket(state.websocket) is None
        assert not state.effects
        assert not any('invalid_input' in json.dumps(frame) for frame in state.sent)


async def test_unenveloped_native_announce_cannot_register(task_runtime, monkeypatch):  # noqa: F811
    async with _ingress(task_runtime, monkeypatch) as state:
        await state.feed(_native_frame('announce', legacy=True))
        assert state.orch.computer_hosts.host_for_socket(state.websocket) is None
        assert not state.effects


@pytest.mark.parametrize('change', ['missing_submission', 'missing_request', 'invalid_submission', 'invalid_request',
    'invalid_connection', 'stale_connection', 'conflicting_submission', 'malformed_payload', 'unknown_event',
    'malformed_descriptor', 'no_owner'])
async def test_invalid_native_announce_metadata_has_no_registry_or_action_effect(task_runtime, monkeypatch, change):  # noqa: F811
    async with _ingress(task_runtime, monkeypatch) as state:
        frame = json.loads(_native_frame('announce'))
        _damage(frame, change)
        if change == 'no_owner':
            state.orch.ui_sessions[state.websocket] = {}
        await state.feed(json.dumps(frame))
        assert state.orch.computer_hosts.host_for_socket(state.websocket) is None
        assert not state.effects
        assert not any(item.get('type') == 'computer_request' for item in state.sent)


def _damage(frame, change):
    if change.startswith('missing_'):
        frame.pop('submission_id' if change == 'missing_submission' else 'request_generation')
    elif change.startswith('invalid_'):
        field = {'invalid_submission': 'submission_id', 'invalid_request': 'request_generation',
                 'invalid_connection': 'connection_generation'}[change]
        frame[field] = 'not-a-uuid'
    elif change == 'stale_connection':
        frame['connection_generation'] = str(uuid4())
    elif change == 'conflicting_submission':
        frame['payload']['submission_id'] = str(uuid4())
    elif change == 'malformed_payload':
        frame['payload'] = []
    elif change == 'unknown_event':
        frame['payload']['event'] = 'unknown'
    elif change == 'malformed_descriptor':
        frame['payload']['host']['verbs'] = ['run_shell']
    elif change == 'wrong_request':
        frame['payload']['request_id'] = 'creq_cccccccccccccccccccccccccccccccc'


async def test_native_host_events_ack_pause_resume_stop_and_withdraw_exact_session(task_runtime, monkeypatch):  # noqa: F811
    async with _ingress(task_runtime, monkeypatch) as state:
        await state.feed(_native_frame('announce'))
        host = state.orch.computer_hosts.host_for_socket(state.websocket)
        session = await state.orch.computer_sessions.start('owner-a', host, state.websocket, 'chat-a', wait_for_ack=False)
        assert not session.acked.is_set()
        await state.feed(_native_frame('heartbeat', session_id=session.session_id))
        assert session.acked.is_set()
        session.grant_terminal(180)
        await state.feed(_native_frame('paused', session_id=session.session_id))
        assert session.state == 'paused' and session.pause_reason == 'local_input' and not session.terminal_ok
        await state.feed(_native_frame('resumed', session_id=session.session_id))
        assert session.state == 'active' and not session.terminal_ok
        await state.feed(_native_frame('stopped', session_id=session.session_id))
        assert session.state == 'ended'
        await state.feed(_native_frame('withdraw'))
        assert state.orch.computer_hosts.host_for_socket(state.websocket) is None
        assert not state.effects
        assert not any(item.get('type') == 'computer_request' for item in state.sent)


@pytest.mark.parametrize('change', ['missing_submission', 'missing_request', 'invalid_submission', 'invalid_request',
    'invalid_connection', 'stale_connection', 'conflicting_submission', 'malformed_payload',
    'wrong_request', 'legacy', 'no_owner', 'owner'])
async def test_invalid_native_response_does_not_resolve_current_request_or_dispatch(task_runtime, monkeypatch, change):  # noqa: F811
    async with _ingress(task_runtime, monkeypatch) as state:
        await state.feed(_native_frame('announce'))
        host = state.orch.computer_hosts.host_for_socket(state.websocket)
        frame = json.loads(_native_frame('response_ok', legacy=change == 'legacy'))
        request = frame['payload']['request_id']
        pending = asyncio.get_running_loop().create_future()
        state.orch.computer_hosts._pending[request] = (pending, 'owner-a', host.host_id, False)
        _damage(frame, change)
        if change in {'no_owner', 'owner'}:
            state.orch.ui_sessions[state.websocket] = {} if change == 'no_owner' else {'sub': 'owner-b'}
        await state.feed(json.dumps(frame))
        assert not pending.done() and not state.effects
        assert not any(item.get('type') == 'computer_request' for item in state.sent)
        state.orch.computer_hosts._pending.pop(request)
        pending.cancel()


@pytest.mark.parametrize('response_kind', ['response_ok', 'response_error'])
async def test_native_closed_reply_completes_approved_operation_on_same_socket(task_runtime, monkeypatch, response_kind):  # noqa: F811
    async with _ingress(task_runtime, monkeypatch) as state:
        orch, websocket = state.orch, state.websocket
        await state.feed(_native_frame('announce'))
        host = orch.computer_hosts.host_for_socket(websocket)
        session = await orch.computer_sessions.start('owner-a', host, websocket, 'chat-a', wait_for_ack=False)
        await state.feed(_native_frame('heartbeat', session_id=session.session_id))
        orch.agents['computer-use-1'] = object()
        orch.tool_permissions.get_tool_scope_map.return_value = {'type_text': 'tools:write'}
        loop = asyncio.get_running_loop()
        request_ready = asyncio.Event()
        physical = []
        monkeypatch.setattr(mcp_tools, '_ORCH', orch)

        async def observed(value):
            if value.get('type') == 'computer_request':
                physical.append(value)
                request_ready.set()

        async def execute(socket, agent, tool, args, **kwargs):
            async def effect(_capabilities):
                return await asyncio.to_thread(MCPServer().process_request, MCPRequest(method='tools/call', params={
                    'name': tool, 'arguments': {**args, 'user_id': 'owner-a', 'session_id': 'chat-a',
                                              '_runtime': SimpleNamespace(loop=loop)}}))

            return await orch._execute_governed_attempt(socket, agent, tool, args,
                user_id='owner-a', channel='web', audit_correlation_id='test', actor_user_id='owner-a',
                auth_principal='owner-a', conversation_id='chat-a', invoke=effect)

        state.on_send[0] = observed
        orch._execute_with_retry = execute
        proposal = await _dispatch((orch, websocket), tool='type_text',
            args={'text': 'reviewed synthetic text', 'hwnd': 42}, agent='computer-use-1')
        decision = next(item['payload'] for item in proposal.ui_components[0]['content']
                        if item.get('payload', {}).get('decision') == 'approve_and_resume')
        await state.feed(_native_frame('paused', session_id=session.session_id))
        websocket.feed(json.dumps({'type': 'ui_event', 'action': 'authorize_action', 'payload': decision,
            'submission_id': str(uuid4()), 'request_generation': str(uuid4()), 'connection_generation': state.generation}))
        await asyncio.wait_for(request_ready.wait(), 5)
        assert not state.completed.is_set() and len(orch.computer_hosts._pending) == 1
        request, = physical
        assert request['verb'] == 'type_text'
        assert request['args'] == {'text': 'reviewed synthetic text', 'hwnd': 42, 'terminal_ok': False}
        response = _native_frame(response_kind, request_id=request['request_id'])
        await state.feed(response)
        await asyncio.wait_for(state.completed.wait(), 5)
        await state.feed(response)
        assert len(physical) == 1 and not orch.computer_hosts._pending and not orch._hitl_pending_calls
        assert session.state == ('active' if response_kind == 'response_ok' else 'paused')
        assert any(work.frame.action == 'authorize_action'
                   for work in orch._connection_contexts[id(websocket)].operations.values())
