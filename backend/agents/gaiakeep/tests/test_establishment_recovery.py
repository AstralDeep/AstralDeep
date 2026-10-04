"""Exercises entry-only connection recovery against real transport contexts and the byte-pinned SDK retry methods without network access."""

import errno
import json
from contextlib import contextmanager
import ssl
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.sync import client as websocket_client

from agents.gaiakeep import remote_runtime as runtime, transport
from agents.gaiakeep.tests.test_sdk_transport import sdk as sdk

@pytest.fixture
def wire(monkeypatch, sdk):
    state = SimpleNamespace(now=100, dial_failures=[], upgrade_failures=[], dials=[], upgrades=[],
                            frames=[], raw_closed=[], ws_closed=[], send_failure=None, recv_failure=None,
                            raw_close_failure=None, ws_close_failure=None, expire=False)
    monkeypatch.setattr(transport.time, 'monotonic', lambda: state.now)
    config = transport.GatewayConfig('r:l:p', frozenset({'r:l:p', 'r:a:p', 'r:b:p'}),
                                      'gateway.test', '', 'public-pin', 'fixture-key', 28282)

    class Raw:
        def close(self):
            state.raw_closed.append(self)
            if state.raw_close_failure:
                raise state.raw_close_failure

    class Socket:
        def send(self, text):
            self.payload = json.loads(text)
            state.frames.append(self.payload)
            if state.send_failure:
                raise state.send_failure

        def recv(self, timeout):
            if state.recv_failure:
                raise state.recv_failure
            if 'ident_key' in self.payload:
                return '{"status_code":"10"}'
            payload = self.payload['message_payload']
            return json.dumps({'status': '10', 'audience': 'fixture-audience', 'peer_audience': 'fixture-peer',
                               'vid': 'verified', 'client_rpc_id': payload['client_rpc_id']})

        def close(self):
            state.ws_closed.append(self)
            if state.ws_close_failure:
                raise state.ws_close_failure

    def dial(address, timeout):
        state.dials.append((address, timeout))
        failure = state.dial_failures.pop(0) if state.dial_failures else None
        if failure:
            if state.expire:
                state.now = 221
            raise failure
        return Raw()

    def connect(url, **kwargs):
        state.upgrades.append((url, kwargs))
        failure = state.upgrade_failures.pop(0) if state.upgrade_failures else None
        if failure:
            if state.expire:
                state.now = 221
            raise failure
        return Socket()

    monkeypatch.setattr(transport.socket, 'create_connection', dial)
    monkeypatch.setattr(websocket_client, 'connect', connect)
    native = runtime._sdk_transport(config, ssl.create_default_context())
    state.native, state.sdk, state.config = native, sdk, config
    return state


def core_for(wire, audience='fixture-audience', peers=()):
    core = wire.sdk.Core()
    core.transport, core.addr = wire.native, 'r:l:p'
    core.signer = SimpleNamespace(audience=audience, sign=lambda action, params, **kwargs: dict(params))
    core.timeouts = SimpleNamespace(c0=20, c1=20, commit=20)
    core.read_peers, core.read_consistency = list(peers), 'linearizable'
    core._next_peer, core.commit_index = 0, 0
    core._lock = threading.Lock()
    core._peer_audiences = {}
    core.read_stats = {'follower': 0, 'leader_fallback': 0, 'skipped': {}}
    core.busy_backoff_max, core.redirect_wait, core.redirects = 0, 0, 2
    return core


@pytest.mark.parametrize('stage', ['dial', 'upgrade'])
@pytest.mark.parametrize('failure', [TimeoutError('private'), ConnectionResetError('private'),
                                     OSError(errno.ENETUNREACH, 'private'), OSError(errno.ETIMEDOUT, 'private'),
                                     ConnectionError('private')])
def test_open_failure_recovers_before_one_mutation_frame(wire, stage, failure):
    getattr(wire, stage + '_failures').append(failure)
    core = core_for(wire)
    signed = []
    core.signer.sign = lambda action, params, **kwargs: signed.append((action, dict(params))) or dict(params)
    assert core.call('core.put', {'upload_id': 'synthetic'})['status'] == '10'
    assert len(wire.dials) == 2 and len(wire.frames) == len(signed) == 1
    assert wire.frames[0]['message_payload']['action'] == 'core.put'
    assert len(wire.raw_closed) == (1 if stage == 'dial' else 2)
    assert len(wire.ws_closed) == 1
    assert wire.native.deadline == 220
    assert all(timeout == 10 for _, timeout in wire.dials)
    assert all(kwargs['open_timeout'] == 10 and kwargs['ssl'].check_hostname
               and kwargs['ssl'].verify_mode == ssl.CERT_REQUIRED and kwargs['proxy'] is None
               for _, kwargs in wire.upgrades)


def test_initial_unsigned_audience_opening_recovers(wire):
    wire.upgrade_failures = [TimeoutError('private')]
    core = core_for(wire, audience=None)
    assert core.call('core.branches', {'collection_id': 'scratch'})['status'] == '10'
    assert [frame['message_payload']['action'] for frame in wire.frames] == ['core.status', 'core.branches']
    assert len(wire.dials) == 3


def test_peer_audience_opening_recovers_without_repeating_status_frame(wire):
    wire.upgrade_failures = [TimeoutError('private')]
    core = core_for(wire, peers=('r:a:p',))
    assert core.call('core.head', {'collection_id': 'scratch', 'branch': 'main'})['status'] == '10'
    assert [frame['message_payload']['action'] for frame in wire.frames] == ['core.status', 'core.head']
    assert len(wire.dials) == 3 and core.read_stats['follower'] == 1


@pytest.mark.parametrize('action', ['core.status', 'core.branches', 'core.put', 'core.commit', 'unknown.action'])
def test_exhausted_opening_never_adds_sdk_attempt(wire, action):
    wire.upgrade_failures = [TimeoutError('private'), ConnectionResetError('private')]
    error = wire.sdk.errors.RpcTimeout if runtime.catalog.ACTIONS.get(action, {}).get('read') is True else wire.sdk.errors.TransportError
    with pytest.raises(error) as raised:
        core_for(wire).call(action, {'collection_id': 'scratch'})
    assert type(raised.value) is error
    assert isinstance(raised.value.__cause__, transport.ConnectionOpenError)
    assert len(wire.dials) == len(wire.raw_closed) == 2 and wire.frames == []


def test_real_ingest_commit_establishes_twice_but_signs_and_sends_once(wire):
    wire.upgrade_failures = [TimeoutError('private')]
    core = core_for(wire)
    core.transfers = SimpleNamespace(proof_params=lambda upload: {'receipt': 'synthetic'})
    signed = []
    core.signer.sign = lambda action, params, **kwargs: signed.append((action, dict(params))) or dict(params)
    ingest = wire.sdk.Ingest()
    ingest.client, ingest.request_id = core, 'synthetic-request'
    assert ingest._commit('synthetic-upload')['vid'] == 'verified'
    assert len(wire.dials) == 2 and len(wire.frames) == len(signed) == 1
    assert signed[0][0] == 'core.commit' and signed[0][1]['request_id'] == 'synthetic-request'


def test_real_ingest_exhaustion_never_reenters_commit(wire):
    wire.upgrade_failures = [TimeoutError('private'), TimeoutError('private')]
    core = core_for(wire)
    core.transfers = SimpleNamespace(proof_params=lambda upload: {})
    signed = []
    core.signer.sign = lambda action, params, **kwargs: signed.append(action) or dict(params)
    ingest = wire.sdk.Ingest()
    ingest.client, ingest.request_id = core, 'synthetic-request'
    with pytest.raises(wire.sdk.errors.TransportError) as raised:
        ingest._commit('synthetic-upload')
    assert type(raised.value) is wire.sdk.errors.TransportError
    assert signed == ['core.commit'] and len(wire.dials) == 2 and not wire.frames


@pytest.mark.parametrize('failure', [ssl.SSLCertVerificationError('private'), PermissionError('private'),
                                     transport.ProtocolError('private'), InvalidStatus(SimpleNamespace(status_code=401)),
                                     OSError('private'), OSError(errno.EIO, 'private'),
                                     ConnectionClosedError(None, None),
                                     *[ConnectionClosedError(Close(code, ''), None) for code in (1002, 1003, 1007, 1008, 1009, 1010)],
                                     ConnectionClosedOK(Close(1000, ''), None)])
@pytest.mark.parametrize('prior', [False, True])
def test_denials_and_unknown_failures_are_not_retried_or_poisoned(wire, failure, prior):
    wire.upgrade_failures = ([TimeoutError('private')] if prior else []) + [failure]
    with pytest.raises(type(failure)) as raised:
        core_for(wire).call('core.branches', {'collection_id': 'scratch'})
    assert raised.value is failure and len(wire.dials) == (2 if prior else 1) and not wire.frames
    assert failure.__context__ is None
    detail = transport.failure_detail(failure)
    expected = 'tls' if isinstance(failure, ssl.SSLError) else 'authorization' if isinstance(failure, PermissionError) else 'protocol' if isinstance(failure, transport.ProtocolError) else 'connection' if isinstance(failure, (ConnectionClosedError, ConnectionClosedOK)) else 'other'
    assert detail == {'native_phase': 'websocket_open', 'failure_kind': expected, 'native_status': None}


def test_first_timeout_followed_by_cleanup_failure_does_not_retry(wire):
    wire.upgrade_failures = [TimeoutError('private opening')]
    failure = TimeoutError('private cleanup')
    wire.raw_close_failure = failure
    with pytest.raises(wire.sdk.errors.RpcTimeout) as raised:
        core_for(wire).call('core.branches', {'collection_id': 'scratch'})
    assert raised.value.__cause__ is failure and len(wire.dials) == 1 and not wire.frames
    assert transport.failure_detail(failure)['native_phase'] == 'cleanup'


@pytest.mark.parametrize('stage', ['send', 'recv', 'ws_close', 'raw_close'])
def test_post_entry_failure_never_reopens(wire, stage):
    setattr(wire, stage + '_failure', TimeoutError('private'))
    with pytest.raises(wire.sdk.errors.TransportError):
        core_for(wire).call('core.commit', {'upload_id': 'synthetic'})
    assert len(wire.dials) == 1 and len(wire.frames) == 1
    assert len(wire.raw_closed) == len(wire.ws_closed) == 1


def test_hard_deadline_stops_second_attempt(wire):
    wire.upgrade_failures = [TimeoutError('private')]
    wire.expire = True
    with pytest.raises(transport.ConnectionOpenError) as raised:
        core_for(wire).call('core.put', {'upload_id': 'synthetic'})
    assert transport.failure_detail(raised.value)['native_phase'] == 'websocket_open'
    assert len(wire.dials) == len(wire.raw_closed) == 1 and not wire.frames
    assert wire.native.deadline == 220


@pytest.fixture
def quiet_worker(monkeypatch):
    worker = SimpleNamespace(start=lambda: None, join=lambda timeout: None)
    monkeypatch.setattr(transport.threading, 'Thread', lambda **kwargs: worker)
    return worker


def test_dataplane_recovery_sends_registration_once(wire, quiet_worker):
    wire.upgrade_failures = [ConnectionResetError('private')]
    stream = wire.native.open_stream('synthetic', None)
    assert len(wire.dials) == 2 and len(wire.frames) == 1
    assert wire.frames[0]['ident_id'] == 'synthetic'
    assert all(url.endswith('/api/dataplane') for url, _ in wire.upgrades)
    stream.close()
    assert len(wire.raw_closed) == 2 and len(wire.ws_closed) == 2


@pytest.mark.parametrize('stage', ['send', 'recv'])
def test_dataplane_activation_never_reopens(wire, quiet_worker, stage):
    failure = TimeoutError('private')
    setattr(wire, stage + '_failure', failure)
    with pytest.raises(TimeoutError) as raised:
        wire.native.open_stream('synthetic', None)
    assert raised.value is failure
    assert len(wire.dials) == len(wire.frames) == 1 and wire.native.streams == []
    assert len(wire.raw_closed) == 1


@pytest.mark.parametrize('cleanup', ['bridge', 'tunnel', None])
def test_ssh_context_cleanup_controls_open_retry(wire, monkeypatch, cleanup):
    counts = {'tunnels': 0, 'bridges': 0, 'closed': 0}
    failure = TimeoutError('private cleanup')

    class Fabric:
        @contextmanager
        def open_tunnel(self, target, port, timeout):
            counts['tunnels'] += 1
            try:
                yield SimpleNamespace()
            finally:
                counts['closed'] += 1
                if cleanup == 'tunnel':
                    raise failure

    class Bridge:
        def __init__(self, channel, deadline):
            self.socket = SimpleNamespace()
        def close(self):
            counts['bridges'] += 1
            if cleanup == 'bridge':
                raise failure

    remote = ModuleType('orchestrator.remote_transport')
    remote.ParamikoTransport = Fabric
    external = ModuleType('shared.external_http')
    external.validate_egress_url = lambda url: None
    monkeypatch.setitem(sys.modules, remote.__name__, remote)
    monkeypatch.setitem(sys.modules, external.__name__, external)
    monkeypatch.setattr(transport, 'SocketBridge', Bridge)
    wire.upgrade_failures = [TimeoutError('private opening')]
    native = transport.NativeTransport(SimpleNamespace(address='gateway.test'), wire.config, ssl.create_default_context())
    if cleanup is None:
        assert native.call('r:l:p', 'core.commit', {}, 20)['status'] == '10'
        assert counts == {'tunnels': 2, 'bridges': 2, 'closed': 2}
        assert len(wire.frames) == 1
    else:
        with pytest.raises(TimeoutError) as raised:
            native.call('r:l:p', 'core.commit', {}, 20)
        assert raised.value is failure
        assert counts == {'tunnels': 1, 'bridges': 1, 'closed': 1} and not wire.frames


@pytest.mark.parametrize('code', [1001, 1006, 1011, 1012, 1013, 1014])
def test_transient_closed_opening_recovers_once(wire, code):
    wire.upgrade_failures = [ConnectionClosedError(Close(code, ''), None)]
    assert core_for(wire).call('core.put', {'upload_id': 'synthetic'})['status'] == '10'
    assert len(wire.dials) == 2 and len(wire.frames) == 1


def test_mixed_closed_opening_denial_is_not_retried(wire):
    failure = ConnectionClosedError(Close(1011, ''), Close(1008, ''), True)
    wire.upgrade_failures = [failure]
    with pytest.raises(ConnectionClosedError) as raised:
        core_for(wire).call('core.put', {'upload_id': 'synthetic'})
    assert raised.value is failure and len(wire.dials) == 1 and not wire.frames
