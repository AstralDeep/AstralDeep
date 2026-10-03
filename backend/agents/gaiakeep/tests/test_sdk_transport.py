"""Exercises the qualified SDK's byte-pinned retry methods against the real remote RPC boundary and deterministic fake sockets. Public source fixtures remain inert outside these tests; signing, TLS and service behavior are not replaced in production."""

import ast
from contextlib import contextmanager
import errno
import hashlib
import json
from pathlib import Path
import ssl
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest
from agents.gaiakeep import client, remote_runtime, transport
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

FIXTURES = Path(__file__).parent / 'fixtures' / 'qualified-sdk'


@pytest.fixture
def sdk(monkeypatch):
    metadata = json.loads((FIXTURES / 'source.json').read_bytes())
    data = {}
    for name, info in metadata['files'].items():
        raw = (FIXTURES / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == info['sha256']
        if name.endswith('.py.txt'):
            assert info['sha256'] == remote_runtime.SDK_LOCK['files']['gaiakeep/' + name[:-4]]
            data[name] = raw
    errors = ModuleType('gaiakeep.errors')
    exec(compile(data['errors.py.txt'], str(FIXTURES / 'errors.py.txt'), 'exec'), errors.__dict__)
    monkeypatch.setitem(sys.modules, 'gaiakeep.errors', errors)
    methods = {'_raw', '_saw', '_skip', '_follower_read', 'call', '_peer_audience', '_learn_audience', 'status'}
    constants = {'ANY_PEER', 'NOT_LEADER', 'MAX_HORIZON_AHEAD_MS', 'BUSY', 'FOLLOWER_READS', 'LINEARIZABLE',
                 'SESSION', 'C1_VERBS', 'IDEMPOTENT_READS', '_PATH_PARAMS'}
    parsed = ast.parse(data['client.py.txt'])
    selected = []
    for node in parsed.body:
        if isinstance(node, ast.Assign) and any(isinstance(name, ast.Name) and name.id in constants for name in node.targets):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == '_horizon_of':
            selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == 'CoreClient':
            node.body = [method for method in node.body if isinstance(method, ast.FunctionDef) and method.name in methods]
            assert {method.name for method in node.body} == methods
            selected.append(node)
    namespace = {'time': time, 'TransportError': errors.TransportError, 'RpcTimeout': errors.RpcTimeout,
                 'OutcomeUnknown': errors.OutcomeUnknown, 'raise_for': errors.raise_for,
                 'strip_transport': lambda reply: reply, 'json_param': lambda reply, field: None,
                 'unsigned': lambda action: action == 'core.status',
                 'log': SimpleNamespace(info=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None,
                                        warn=lambda *args, **kwargs: None)}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(FIXTURES / 'client.py.txt'), 'exec'), namespace)
    ingest = next(node for node in ast.parse(data['ingest.py.txt']).body
                  if isinstance(node, ast.ClassDef) and node.name == 'Ingest')
    ingest.body = [method for method in ingest.body if isinstance(method, ast.FunctionDef) and method.name == '_commit']
    assert len(ingest.body) == 1
    exec(compile(ast.Module(body=[ingest], type_ignores=[]), str(FIXTURES / 'ingest.py.txt'), 'exec'), namespace)
    return SimpleNamespace(errors=errors, Core=namespace['CoreClient'], Ingest=namespace['Ingest'])


@pytest.fixture
def rpc(monkeypatch, sdk):
    records = {'calls': [], 'closed': [], 'failures': {}, 'statuses': {}, 'payloads': [], 'replies': {}}
    config = transport.GatewayConfig('r:l:p', frozenset({'r:l:p', 'r:a:p', 'r:b:p'}),
                                      'gateway.test', '', 'fixture-public-key', 'fixture-service-key')

    class Socket:
        def send(self, value):
            self.envelope = json.loads(value)
            records['payloads'].append(self.envelope['message_payload'])

        def recv(self, timeout):
            info, payload = self.envelope['message_info'], self.envelope['message_payload']
            peer = ':'.join(info[field] for field in ('dst_region', 'dst_agent', 'dst_plugin'))
            action = payload['action']
            records['calls'].append((peer, action))
            failure = records['failures'].get((peer, action))
            if failure is not None:
                if records.get('expire_on_failure'):
                    compatible.deadline = 0
                raise failure
            reply = records['replies'].get((peer, action),
                       {'status': records['statuses'].get((peer, action), '10'), 'audience': 'fixture-audience',
                        'peer_audience': 'fixture-peer-audience', 'vid': 'verified-head'})
            return json.dumps(dict(reply, client_rpc_id=payload['client_rpc_id']))

        def close(self):
            records['closed'].append(True)

    @contextmanager
    def opened(self, path):
        assert path == '/api/apisocket'
        socket = Socket()
        try:
            yield socket
        finally:
            socket.close()

    monkeypatch.setattr(transport.VerifiedLoopbackTransport, '_socket', opened)
    compatible = remote_runtime._sdk_transport(config, ssl.create_default_context())
    unadapted = transport.VerifiedLoopbackTransport(config, ssl.create_default_context())

    def core(native, read_peers=('r:a:p', 'r:b:p')):
        instance = sdk.Core()
        instance.transport, instance.addr = native, 'r:l:p'
        instance.signer = SimpleNamespace(audience=None, sign=lambda action, params, **kwargs: dict(params))
        instance.timeouts = SimpleNamespace(c0=20, c1=20, commit=20)
        instance.read_peers, instance.read_consistency = list(read_peers), 'linearizable'
        instance._next_peer, instance.commit_index = 0, 0
        instance._lock = threading.Lock()
        instance._peer_audiences = {}
        instance.read_stats = {'follower': 0, 'leader_fallback': 0, 'skipped': {}}
        instance.busy_backoff_max, instance.redirect_wait, instance.redirects = 0, 0, 2
        return instance

    return SimpleNamespace(records=records, compatible=compatible, unadapted=unadapted, core=core, sdk=sdk)


def head_calls(rpc):
    return [peer for peer, action in rpc.records['calls'] if action == 'core.head']


def test_existing_raw_timeout_aborts_before_healthy_follower(rpc):
    failure = TimeoutError('private socket failure')
    rpc.records['failures'][('r:a:p', 'core.head')] = failure
    with pytest.raises(TimeoutError) as raised:
        rpc.core(rpc.unadapted).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert raised.value is failure and head_calls(rpc) == ['r:a:p']


@pytest.mark.parametrize('failure', [TimeoutError('private'), ConnectionResetError('private'),
                                     OSError(errno.ENETUNREACH, 'private'), ConnectionClosedError(None, None)])
def test_transient_failed_follower_reaches_next_admitted_peer(rpc, failure):
    rpc.records['failures'][('r:a:p', 'core.head')] = failure
    core = rpc.core(rpc.compatible)
    assert core.call('core.head', {'collection_id': 'scratch', 'branch': 'main'})['vid'] == 'verified-head'
    assert head_calls(rpc) == ['r:a:p', 'r:b:p']
    assert core.read_stats['follower'] == 1 and core.read_stats['leader_fallback'] == 0
    assert len(rpc.records['closed']) == len(rpc.records['calls'])


@pytest.mark.parametrize('failure,count,error', [(TimeoutError('private'), 3, 'RpcTimeout'),
                                                (ConnectionResetError('private'), 4, 'TransportError')])
def test_all_peers_fail_only_within_existing_sdk_read_attempts(rpc, failure, count, error):
    for peer in ('r:a:p', 'r:b:p', 'r:l:p'):
        rpc.records['failures'][(peer, 'core.head')] = failure
    with pytest.raises(getattr(rpc.sdk.errors, error)) as raised:
        rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert len(head_calls(rpc)) == count and set(head_calls(rpc)) == {'r:a:p', 'r:b:p', 'r:l:p'}
    assert 'private' not in str(raised.value)
    assert len(rpc.records['closed']) == len(rpc.records['calls'])


@pytest.mark.parametrize('failure', [TimeoutError('private'), OSError(errno.ETIMEDOUT, 'private'),
                                     ConnectionAbortedError('private'), ConnectionRefusedError('private'),
                                     ConnectionResetError('private'), BrokenPipeError('private'),
                                     OSError(errno.ECONNABORTED, 'private'), OSError(errno.ECONNREFUSED, 'private'),
                                     OSError(errno.ECONNRESET, 'private'), OSError(errno.EPIPE, 'private'),
                                     OSError(errno.ENETRESET, 'private'), OSError(errno.ENETDOWN, 'private'),
                                     OSError(errno.ENETUNREACH, 'private'), OSError(errno.EHOSTUNREACH, 'private'),
                                     ConnectionClosedError(None, None),
                                     *[ConnectionClosedError(Close(code, ''), None) for code in (1001, 1006, 1011, 1012, 1013, 1014)]])
def test_transient_mutation_failure_is_uncertain_and_not_resent(rpc, failure):
    rpc.records['failures'][('r:l:p', 'core.put')] = failure
    with pytest.raises(rpc.sdk.errors.TransportError) as raised:
        rpc.core(rpc.compatible).call('core.put', {'upload_id': 'synthetic'})
    assert type(raised.value) is rpc.sdk.errors.TransportError
    assert [call for call in rpc.records['calls'] if call[1] == 'core.put'] == [('r:l:p', 'core.put')]
    assert client.failure(raised.value, True)[0] == 'unconfirmed'


@pytest.mark.parametrize('failure', [TimeoutError('private'), OSError(errno.ETIMEDOUT, 'private')])
def test_real_ingest_commit_timeout_is_uncertain_without_resend(rpc, failure):
    rpc.records['failures'][('r:l:p', 'core.commit')] = failure
    core = rpc.core(rpc.compatible)
    core.transfers = SimpleNamespace(proof_params=lambda upload: {'receipt': 'synthetic-proof'})
    ingest = rpc.sdk.Ingest()
    ingest.client, ingest.request_id = core, 'synthetic-request'
    with pytest.raises(rpc.sdk.errors.TransportError) as raised:
        ingest._commit('synthetic-upload')
    assert type(raised.value) is rpc.sdk.errors.TransportError
    assert [call for call in rpc.records['calls'] if call[1] == 'core.commit'] == [('r:l:p', 'core.commit')]
    committed = [payload for payload in rpc.records['payloads'] if payload['action'] == 'core.commit']
    assert len(committed) == 1
    assert committed[0]['request_id'] == 'synthetic-request' and committed[0]['upload_id'] == 'synthetic-upload'
    assert committed[0]['receipt'] == 'synthetic-proof'
    assert client.failure(raised.value, True)[0] == 'unconfirmed'
    assert len(rpc.records['closed']) == len(rpc.records['calls'])


def test_real_ingest_commit_success_preserves_the_native_result(rpc):
    core = rpc.core(rpc.compatible)
    core.transfers = SimpleNamespace(proof_params=lambda upload: {'receipt': 'synthetic-proof'})
    ingest = rpc.sdk.Ingest()
    ingest.client, ingest.request_id = core, 'synthetic-request'
    reply = ingest._commit('synthetic-upload')
    assert reply['status'] == '10' and reply['vid'] == 'verified-head'
    assert [call for call in rpc.records['calls'] if call[1] == 'core.commit'] == [('r:l:p', 'core.commit')]
    assert len(rpc.records['closed']) == len(rpc.records['calls'])


@pytest.mark.parametrize('reply', [{'status': '500'}, {'status': '504'}, {'error': 'private'}, {}])
def test_real_ingest_native_uncertain_commit_reply_is_not_resent(rpc, reply):
    rpc.records['replies'][('r:l:p', 'core.commit')] = reply
    core = rpc.core(rpc.compatible)
    core.transfers = SimpleNamespace(proof_params=lambda upload: {})
    ingest = rpc.sdk.Ingest()
    ingest.client, ingest.request_id = core, 'synthetic-request'
    with pytest.raises(rpc.sdk.errors.TransportError) as raised:
        ingest._commit('synthetic-upload')
    assert type(raised.value) is rpc.sdk.errors.TransportError
    assert isinstance(raised.value.__cause__, rpc.sdk.errors.RpcTimeout)
    assert 'private' not in str(raised.value) and client.failure(raised.value, True)[0] == 'unconfirmed'
    assert [call for call in rpc.records['calls'] if call[1] == 'core.commit'] == [('r:l:p', 'core.commit')]
    assert len(rpc.records['closed']) == len(rpc.records['calls'])


@pytest.mark.parametrize('action', ['core.put', 'unknown.action'])
@pytest.mark.parametrize('reply', [{'status': '500'}, {'status': '504'}, {}])
def test_uncertain_native_mutation_and_unknown_replies_have_no_retry_type(rpc, action, reply):
    rpc.records['replies'][('r:l:p', action)] = reply
    with pytest.raises(rpc.sdk.errors.TransportError) as raised:
        rpc.compatible.call('r:l:p', action, {}, 20)
    assert type(raised.value) is rpc.sdk.errors.TransportError
    assert rpc.records['calls'] == [('r:l:p', action)] and rpc.records['closed'] == [True]


@pytest.mark.parametrize('reply', [{'status': '500'}, {'status': '504'}, {'error': 'private'}, {}])
def test_native_read_timeout_preserves_the_existing_sdk_refusal(rpc, reply):
    rpc.records['replies'][('r:a:p', 'core.head')] = reply
    assert rpc.compatible.call('r:a:p', 'core.head', {}, 20) == reply
    rpc.records['calls'].clear()
    with pytest.raises(rpc.sdk.errors.RpcTimeout):
        rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert head_calls(rpc) == ['r:a:p']


@pytest.mark.parametrize('status,name', [('3', 'Unauthenticated'), ('4', 'Forbidden'), ('7', 'PolicyRefused'),
                                       ('8', 'BadRequest'), ('9', 'NotFound'), ('6', 'Failed'),
                                       ('13', 'Deferred'), ('15', 'StageRequired')])
def test_real_ingest_native_refusals_preserve_the_exact_sdk_type(rpc, status, name):
    reply = {'status': status}
    rpc.records['replies'][('r:l:p', 'core.commit')] = reply
    assert rpc.compatible.call('r:l:p', 'core.commit', {}, 20) == reply
    rpc.records['calls'].clear()
    core = rpc.core(rpc.compatible)
    core.transfers = SimpleNamespace(proof_params=lambda upload: {})
    ingest = rpc.sdk.Ingest()
    ingest.client, ingest.request_id = core, 'synthetic-request'
    with pytest.raises(getattr(rpc.sdk.errors, name)) as raised:
        ingest._commit('synthetic-upload')
    assert type(raised.value) is getattr(rpc.sdk.errors, name)
    assert [call for call in rpc.records['calls'] if call[1] == 'core.commit'] == [('r:l:p', 'core.commit')]


@pytest.mark.parametrize('action,read', [('core.head', True), ('core.get', True), ('core.put', False),
                                       ('core.commit', False), ('unknown.action', False)])
@pytest.mark.parametrize('failure', [TimeoutError('private'), OSError(errno.ETIMEDOUT, 'private')])
def test_only_pinned_read_rpc_timeouts_have_the_sdk_timeout_type(rpc, action, read, failure):
    rpc.records['failures'][('r:l:p', action)] = failure
    with pytest.raises(rpc.sdk.errors.TransportError) as raised:
        rpc.compatible.call('r:l:p', action, {}, 20)
    expected = rpc.sdk.errors.RpcTimeout if read else rpc.sdk.errors.TransportError
    assert type(raised.value) is expected and raised.value.action == action
    assert raised.value.__cause__ is failure and 'private' not in str(raised.value)
    assert rpc.records['calls'] == [('r:l:p', action)] and rpc.records['closed'] == [True]


@pytest.mark.parametrize('failure', [ssl.SSLCertVerificationError('private'), PermissionError('private'),
                                     transport.ProtocolError('private'),
                                     ConnectionClosedError(Close(1008, 'private'), None)])
def test_real_ingest_commit_denials_are_never_repeated_or_normalized(rpc, failure):
    rpc.records['failures'][('r:l:p', 'core.commit')] = failure
    core = rpc.core(rpc.compatible)
    core.transfers = SimpleNamespace(proof_params=lambda upload: {})
    ingest = rpc.sdk.Ingest()
    ingest.client, ingest.request_id = core, 'synthetic-request'
    with pytest.raises(type(failure)) as raised:
        ingest._commit('synthetic-upload')
    assert raised.value is failure
    assert [call for call in rpc.records['calls'] if call[1] == 'core.commit'] == [('r:l:p', 'core.commit')]
    assert len(rpc.records['closed']) == len(rpc.records['calls'])


def test_real_ingest_commit_deadline_expiry_never_repeats(rpc):
    failure = TimeoutError('private')
    rpc.records['failures'][('r:l:p', 'core.commit')] = failure
    rpc.records['expire_on_failure'] = True
    core = rpc.core(rpc.compatible)
    core.transfers = SimpleNamespace(proof_params=lambda upload: {})
    ingest = rpc.sdk.Ingest()
    ingest.client, ingest.request_id = core, 'synthetic-request'
    with pytest.raises(TimeoutError) as raised:
        ingest._commit('synthetic-upload')
    assert raised.value is failure
    assert [call for call in rpc.records['calls'] if call[1] == 'core.commit'] == [('r:l:p', 'core.commit')]
    assert len(rpc.records['closed']) == len(rpc.records['calls'])


@pytest.mark.parametrize('failure', [ssl.SSLCertVerificationError('private'), ssl.SSLError('private'),
                                     PermissionError('private'), OSError('private'), OSError(errno.EINVAL, 'private'),
                                     transport.ProtocolError('private'), ValueError('private')])
def test_security_protocol_and_unknown_os_failures_are_not_normalized(rpc, failure):
    rpc.records['failures'][('r:a:p', 'core.head')] = failure
    with pytest.raises(type(failure)) as raised:
        rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert raised.value is failure and head_calls(rpc) == ['r:a:p']


@pytest.mark.parametrize('code', [1000, 1002, 1003, 1007, 1008, 1009, 1010, 4003])
def test_nontransient_close_codes_never_trigger_follower_retry(rpc, code):
    failure = ConnectionClosedOK(Close(code, 'private'), None) if code == 1000 else ConnectionClosedError(Close(code, 'private'), None)
    rpc.records['failures'][('r:a:p', 'core.head')] = failure
    with pytest.raises(type(failure)) as raised:
        rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert raised.value is failure and head_calls(rpc) == ['r:a:p']


@pytest.mark.parametrize('code', [1001, 1006, 1011, 1012, 1013, 1014])
def test_only_transient_close_codes_use_sdk_read_fallback(rpc, code):
    rpc.records['failures'][('r:a:p', 'core.head')] = ConnectionClosedError(Close(code, ''), None)
    assert rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})['vid'] == 'verified-head'
    assert head_calls(rpc) == ['r:a:p', 'r:b:p']


@pytest.mark.parametrize('name', ['Forbidden', 'Unauthenticated', 'PolicyRefused', 'IntegrityError'])
def test_sdk_authorization_policy_and_integrity_denials_are_authoritative(rpc, name):
    failure = getattr(rpc.sdk.errors, name)('private')
    rpc.records['failures'][('r:a:p', 'core.head')] = failure
    with pytest.raises(type(failure)) as raised:
        rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert raised.value is failure and head_calls(rpc) == ['r:a:p']


def test_deadline_expiry_stops_before_any_rpc(rpc):
    rpc.compatible.deadline = 0
    with pytest.raises(TimeoutError):
        rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert rpc.records['calls'] == []


@pytest.mark.parametrize('failure', [TimeoutError('private'), ConnectionResetError('private'), ConnectionClosedError(None, None)])
def test_deadline_expiring_during_rpc_never_retries(rpc, failure):
    rpc.records['failures'][('r:a:p', 'core.head')] = failure
    rpc.records['expire_on_failure'] = True
    with pytest.raises(type(failure)) as raised:
        rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert raised.value is failure and head_calls(rpc) == ['r:a:p']
    assert len(rpc.records['closed']) == len(rpc.records['calls'])


@pytest.mark.parametrize('status,name', [('3', 'Unauthenticated'), ('4', 'Forbidden'), ('7', 'PolicyRefused')])
def test_returned_native_denial_never_reaches_another_peer(rpc, status, name):
    rpc.records['statuses'][('r:a:p', 'core.head')] = status
    with pytest.raises(getattr(rpc.sdk.errors, name)):
        rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert head_calls(rpc) == ['r:a:p']


def test_mixed_policy_close_is_not_a_transient_connection(rpc):
    failure = ConnectionClosedError(Close(1013, ''), Close(1008, 'private'), True)
    rpc.records['failures'][('r:a:p', 'core.head')] = failure
    with pytest.raises(ConnectionClosedError) as raised:
        rpc.core(rpc.compatible).call('core.head', {'collection_id': 'scratch', 'branch': 'main'})
    assert raised.value is failure and head_calls(rpc) == ['r:a:p']


def test_stream_errors_remain_outside_rpc_normalization(rpc, monkeypatch):
    failure = TimeoutError('private')
    def failed(*args):
        raise failure
    monkeypatch.setattr(transport.VerifiedLoopbackTransport, 'open_stream', failed)
    with pytest.raises(TimeoutError) as raised:
        rpc.compatible.open_stream('stream', None)
    assert raised.value is failure and rpc.records['calls'] == []
