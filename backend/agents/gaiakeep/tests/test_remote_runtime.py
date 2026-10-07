"""Verifies remote SDK integrity, private identity, pinned deployment trust and bounded operation results without a live service."""

import base64
import builtins
import hashlib
import importlib.metadata
import json
import os
import ssl
import stat
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from agents.gaiakeep import client, remote_runtime, transport
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def public_key(key):
    return base64.urlsafe_b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode().rstrip('=')


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    key = ec.generate_private_key(ec.SECP384R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    trust = {'core_address': 'r:a:p', 'allowed_peers': ['r:a:p', 'r:b:p'], 'tls_name': 'gateway.test',
             'ca_pem': 'fixture-ca', 'core_public_key': public_key(key), 'port': 28282}
    profile_data = {'core_addr': 'r:a:p', 'core_public_key': trust['core_public_key'], 'read_peers': ['r:a:p'],
                    'service_key': 'private-service-key', 'principal': 'owner', 'port': 28282,
                    'tenant': 'owner-tenant', 'default_collection': 'scratch', 'default_domain': 'private-domain',
                    'key_file': str(tmp_path / '.gaiakeep' / 'owner.key')}
    profile = SimpleNamespace(**profile_data)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(remote_runtime, '_private_bytes',
                        lambda path, limit: pem if str(path).endswith('owner.key') else json.dumps(profile_data).encode())
    records = {'constructed': [], 'calls': [], 'closed': [], 'transports': []}

    class Core:
        def __init__(self, transport, address, **kwargs):
            records['constructed'].append((address, kwargs))
            self.transport, self.address = transport, address
            self.profile = None
            self.signer = SimpleNamespace(principal='owner')

        def call(self, action, params, timeout=None):
            return self.transport.call(self.address, action, params, timeout or 20)

        def read(self, vid, path, max_bytes=None):
            records['calls'].append(('read', vid, path, max_bytes))
            return b'hello'

        def ingest(self, files, **kwargs):
            records['calls'].append(('ingest', files, kwargs, Path(next(iter(files.values()))).read_bytes()))
            return SimpleNamespace(vid='committed', state='published')

        def have_ingest(self, files, **kwargs):
            return {'vid': 'deduplicated'}, {'sent': 1}

        def close(self):
            records['closed'].append('core')

    class Loopback:
        def __init__(self, config, tls_context=None):
            records['transports'].append((config, tls_context))
            self.deadline = 0

        def remaining(self, timeout):
            return timeout

        def call(self, addr, action, params, timeout):
            records['calls'].append((action, params))
            return {'status': '10', 'text': 'private-service-key', 'job_id': 'native-job'}

        def close(self):
            records['closed'].append('transport')

    errors = ModuleType('gaiakeep.errors')
    error_path = Path(__file__).parent / 'fixtures' / 'qualified-sdk' / 'errors.py.txt'
    error_source = error_path.read_bytes()
    assert hashlib.sha256(error_source).hexdigest() == remote_runtime.SDK_LOCK['files']['gaiakeep/errors.py']
    exec(compile(error_source, str(error_path), 'exec'), errors.__dict__)
    monkeypatch.setitem(sys.modules, 'gaiakeep.errors', errors)
    monkeypatch.setattr(transport, 'VerifiedLoopbackTransport', Loopback, raising=False)
    monkeypatch.setattr(ssl, 'create_default_context', lambda **kwargs: SimpleNamespace(
        verify_mode=ssl.CERT_REQUIRED, check_hostname=True, cadata=kwargs.get('cadata')))
    profiles = SimpleNamespace(Profile=lambda *args: profile,
                               resolve_path=lambda: str(Path.home() / '.gaiakeep' / 'gaiakeep-profile.json'))
    monkeypatch.setattr(remote_runtime, '_validated_sdk', lambda: (
        Core, lambda **kwargs: kwargs, Exception, lambda action, reply: reply,
        profiles, SimpleNamespace(parse_private=lambda data: key)))
    return SimpleNamespace(trust=trust, records=records, Core=Core, profile=profile, profiles=profiles, key=key, pem=pem,
                           request=lambda **change: dict(tool='gaiakeep_core_whoami',
                               arguments={'machine_id': 'mine', 'params': {}}, trust=dict(trust), **change))


def test_owner_profile_and_key_stay_remote(runtime):
    out = remote_runtime.run(runtime.request())
    assert out == {'ok': True, 'result': {'status': '10', 'text': '[redacted]', 'job_id': 'native-job'}}
    address, options = runtime.records['constructed'][0]
    assert address == 'r:a:p'
    assert options['principal'] == 'owner' and options['key'] is runtime.key
    assert options['key_file'] is None and options['flows'] == 1
    assert options['timeouts']['flow_max'] == 120 and options['redirects'] == 2
    config, context = runtime.records['transports'][0]
    assert config.port == 28282 and config.service_key == 'private-service-key'
    assert context.cadata == 'fixture-ca' and context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert runtime.records['closed'] == ['core', 'transport']


def test_connection_summary_exposes_only_safe_discovery_fields(runtime):
    request = runtime.request()
    request['tool'] = 'gaiakeep_connection_info'
    out = remote_runtime.run(request)
    assert out == {'ok': True, 'result': {'principal': 'owner', 'tenant': 'owner-tenant',
        'default_collection': 'scratch', 'default_domain': 'private-domain', 'client_version': '0.2.0'}}
    assert not runtime.records['calls']
    assert 'key' not in str(out) and 'gaiakeep-profile.json' not in str(out)


def test_collection_discovery_uses_validated_identity(runtime, monkeypatch):
    calls = []
    def collections(core, params=None):
        calls.append(core.profile.principal)
        core.call('core.whoami', {})
        core.call('core.head', {'collection_id': core.profile.default_collection})
        return {'collections': [{'collection_id': core.profile.default_collection, 'head': {'vid': 'head'}}]}
    monkeypatch.setattr(remote_runtime, '_known_collections', collections)
    request = runtime.request()
    request['tool'] = 'gaiakeep_list_collections'
    out = remote_runtime.run(request)
    assert out['result']['collections'][0]['collection_id'] == 'scratch'
    assert calls == ['owner'] and [item[0] for item in runtime.records['calls']] == ['core.whoami', 'core.head']


def test_discovery_denial_remains_read_failure(runtime, monkeypatch):
    def refused(core, params=None):
        raise type('Forbidden', (Exception,), {})('private-service-key')
    monkeypatch.setattr(remote_runtime, '_known_collections', refused)
    request = runtime.request()
    request['tool'] = 'gaiakeep_list_collections'
    out = remote_runtime.run(request)
    assert out['verdict'] == 'upstream_denied' and 'private-service-key' not in str(out)


def test_paged_collection_discovery_uses_profile_tenant_and_native_bounds(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(client, 'execute', lambda core, action, params: calls.append((action, params)) or {'next': 'next'})
    core = SimpleNamespace(profile=runtime.profile)
    assert remote_runtime._known_collections(core, {'after': 'previous', 'limit': 17}) == {'next': 'next'}
    assert calls == [('core.collections', {'after': 'previous', 'limit': 17, 'tenant_id': 'owner-tenant'})]
    with pytest.raises(client.AgentError):
        remote_runtime._known_collections(SimpleNamespace(profile=SimpleNamespace(tenant=None)), {'after': 'previous'})


@pytest.mark.parametrize('params', [{'after': 'previous'}, {'after': False}, {'after': []},
                                   {'after': 'x' * 4097}, {'limit': True}, {'limit': 0}, {'limit': 1001},
                                   {'principal': 'forged'}, {'tenant_id': 'forged'}, [], 0])
def test_invalid_collection_discovery_refuses_before_optional_sdk_import(monkeypatch, params):
    imported = []
    original = builtins.__import__
    def without_sdk(name, *args, **kwargs):
        if name == 'gaiakeep' or name.startswith('gaiakeep.'):
            imported.append(name)
            raise ModuleNotFoundError('The optional SDK is deliberately unavailable.')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_sdk)
    with pytest.raises(client.AgentError) as error:
        remote_runtime._known_collections(SimpleNamespace(profile=SimpleNamespace(tenant=None)), params)
    assert error.value.verdict == 'invalid_argument' and not imported


def test_dataset_manifest_paths_remain_typed_strings_through_remote_sanitizer():
    from agents.gaiakeep.dataset_workspace import _manifest

    manifest = _manifest([{'path': '["metrics"]', 'size': 1, 'sha256': 'a' * 64}])
    assert remote_runtime._result(manifest, 'inspect_dataset', ()) == manifest
    assert client.clean_dataset_result(dict(manifest, secret='private-value'), ('private-value',)) == manifest
    with pytest.raises(client.AgentError):
        client.clean_dataset_result(manifest, ('metrics',))


@pytest.mark.parametrize('payload', [None, {}, {'tool': [], 'arguments': {}, 'trust': {}},
                                     {'tool': 'unknown', 'arguments': {}, 'trust': {}},
                                     {'tool': 'gaiakeep_core_whoami', 'arguments': {'machine_id': 'm', 'params': {},
                                      'principal': 'other'}, 'trust': {}}, {'credentials': {'key': 'forged'}}])
def test_closed_requests_fail_before_remote_identity(runtime, payload):
    assert remote_runtime.run(payload)['verdict'] == 'invalid_argument'
    assert not runtime.records['constructed']


@pytest.mark.parametrize('change', [
    {'port': True}, {'port': 0}, {'port': 65536}, {'allowed_peers': 'r:a:p'}, {'allowed_peers': []},
    {'allowed_peers': ['r:b:p']}, {'allowed_peers': ['r:a:p', 'invalid']},
    {'allowed_peers': ['r:a:p'] * 17}, {'tls_name': '../other'}, {'ca_pem': ''},
    {'ca_pem': 'x' * 65537}, {'core_public_key': 'invalid-key'}, {'allow_legacy': 'true'},
    {'restore_root': []}, {'host': 'internal-target'},
])
def test_operator_trust_is_closed_and_bounded(runtime, change):
    request = runtime.request()
    request['trust'].update(change)
    assert remote_runtime.run(request)['verdict'] == 'invalid_argument'
    assert not runtime.records['calls']


@pytest.mark.parametrize('field,value', [('core_addr', 'r:b:p'), ('read_peers', ['r:c:p']),
                                      ('port', 22), ('service_key', 'key\r\ninjected'),
                                      ('service_key', ''), ('principal', '\x00owner')])
def test_profile_cannot_expand_trust(runtime, field, value):
    setattr(runtime.profile, field, value)
    assert remote_runtime.run(runtime.request())['verdict'] == 'not_configured'
    assert not runtime.records['transports']


def test_profile_wrong_public_key(runtime):
    runtime.profile.core_public_key = public_key(ec.generate_private_key(ec.SECP384R1()))
    assert remote_runtime.run(runtime.request())['verdict'] == 'not_configured'
    assert not runtime.records['calls']


def test_profile_replacement_is_never_reread(runtime, monkeypatch, tmp_path):
    original_init, original_read = runtime.Core.__init__, remote_runtime._private_bytes
    reads = []
    def bounded_read(path, limit):
        reads.append(str(path))
        return original_read(path, limit)
    def replaced_profile(self, *args, **kwargs):
        path = tmp_path / '.gaiakeep' / 'gaiakeep-profile.json'
        path.parent.mkdir()
        path.write_bytes(b'x' * 65537)
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(runtime.Core, '__init__', replaced_profile)
    monkeypatch.setattr(runtime.Core, 'from_profile', lambda *a, **kw: pytest.fail('unbounded profile reread'), raising=False)
    monkeypatch.setattr(remote_runtime, '_private_bytes', bounded_read)
    assert remote_runtime.run(runtime.request())['ok']
    assert len(reads) == 2 and reads[0].endswith('gaiakeep-profile.json') and reads[1].endswith('owner.key')
    assert runtime.records['constructed'][0][0] == 'r:a:p'
    assert runtime.records['closed'] == ['core', 'transport']


def test_selected_account_profile_retains_private_bounded_reads(runtime, monkeypatch, tmp_path):
    selected = tmp_path / 'project profile' / 'gaiakeep-profile.json'
    runtime.profiles.resolve_path = lambda: str(selected)
    original, reads = remote_runtime._private_bytes, []
    def recorded(path, limit):
        reads.append((str(path), limit))
        return original(path, limit)
    monkeypatch.setattr(remote_runtime, '_private_bytes', recorded)
    assert remote_runtime.run(runtime.request())['ok']
    assert reads == [(str(selected), 65536), (runtime.profile.key_file, 16384)]


@pytest.mark.parametrize('exc,verdict', [(FileNotFoundError(), 'not_configured'),
                                      (PermissionError(), 'auth_failed'), (ValueError(), 'invalid_argument')])
def test_private_profile_errors_are_sanitized(runtime, monkeypatch, exc, verdict):
    def failed(*args):
        raise exc
    monkeypatch.setattr(remote_runtime, '_private_bytes', failed)
    out = remote_runtime.run(runtime.request())
    assert out['verdict'] == verdict and 'private-service-key' not in str(out)
    assert not runtime.records['calls']


def test_wrong_private_curve_refused_before_network(runtime, monkeypatch):
    sdk = remote_runtime._validated_sdk()
    monkeypatch.setattr(remote_runtime, '_validated_sdk', lambda: (
        *sdk[:-1], SimpleNamespace(parse_private=lambda data: ec.generate_private_key(ec.SECP256R1()))))
    assert remote_runtime.run(runtime.request())['verdict'] == 'auth_failed'
    assert not runtime.records['transports']


@pytest.mark.parametrize('tool,arguments', [
    ('gaiakeep_fetch', {'machine_id': 'm', 'params': {}}),
    ('gaiakeep_listnodes', {'machine_id': 'm', 'params': {}}),
])
def test_unqualified_legacy_never_loads_identity(runtime, monkeypatch, tool, arguments):
    monkeypatch.setattr(remote_runtime, '_validated_sdk', lambda: pytest.fail('identity was loaded'))
    out = remote_runtime.run({'tool': tool, 'arguments': arguments, 'trust': runtime.trust})
    assert out['verdict'] == 'unsupported'


def test_verified_file_bytes_stay_outside_redaction(runtime):
    request = runtime.request()
    request.update(tool='gaiakeep_read_file', arguments={'machine_id': 'm', 'vid': 'v', 'path': 'folder/file'})
    out = remote_runtime.run(request)
    assert base64.b64decode(out['result']['data_base64']) == b'hello'
    assert out['result']['sha256'] == hashlib.sha256(b'hello').hexdigest()
    assert runtime.records['calls'][0] == ('read', 'v', 'folder/file', 8 << 20)


def test_upload_publishes_only_owned_temporary_bytes(runtime):
    request = runtime.request()
    request.update(tool='gaiakeep_upload_file', request_id='a' * 32,
                   arguments={'machine_id': 'm', 'collection_id': 'c', 'path': 'file',
                              'data_base64': base64.b64encode(b'hello').decode(), 'expected_head': 'head'})
    out = remote_runtime.run(request)
    assert out['result']['vid'] == 'committed'
    _, files, options, data = runtime.records['calls'][0]
    assert data == b'hello' and options['request_id'] == 'a' * 32 and options['expected_head'] == 'head'
    assert not Path(next(iter(files.values()))).exists()


@pytest.mark.parametrize('request_id', [None, 'short', 'x' * 65, 4, ';shell-injection'])
def test_upload_requires_bounded_retained_id(runtime, request_id):
    request = runtime.request()
    request.update(tool='gaiakeep_upload_file', request_id=request_id,
                   arguments={'machine_id': 'm', 'collection_id': 'c', 'path': 'file', 'data_base64': ''})
    assert remote_runtime.run(request)['verdict'] == 'invalid_argument'
    assert not runtime.records['constructed']


@pytest.mark.parametrize('exception,verdict', [(TimeoutError('secret'), 'unconfirmed'),
                                             (client.ProtocolError('secret'), 'unconfirmed'),
                                             (client.AgentError('protocol_error', 'secret'), 'unconfirmed'),
                                             (client.AgentError('integrity_error', 'secret'), 'unconfirmed')])
def test_mutation_outcomes_are_never_replayed(runtime, monkeypatch, exception, verdict):
    calls = []
    def failed(*args, **kwargs):
        calls.append(1)
        raise exception
    monkeypatch.setattr(client, 'execute', failed)
    request = runtime.request()
    request.update(tool='gaiakeep_core_repair')
    out = remote_runtime.run(request)
    assert out['verdict'] == verdict and calls == [1] and 'secret' not in out['message']
    assert runtime.records['closed'] == ['core', 'transport']


def test_cleanup_failure_after_mutation_is_uncertain(runtime, monkeypatch):
    def failed(*args):
        raise OSError('private-service-key')
    monkeypatch.setattr(runtime.Core, 'close', failed)
    request = runtime.request()
    request['tool'] = 'gaiakeep_core_repair'
    out = remote_runtime.run(request)
    assert out['verdict'] == 'unconfirmed' and 'private-service-key' not in str(out)


@pytest.mark.parametrize('value,verdict', [
    ([], 'protocol_error'), ({'large': 'x' * ((1 << 20) + 1)}, 'protocol_error'),
    ({'data_base64': '!', 'byte_count': 1, 'sha256': 'bad'}, 'invalid_argument'),
    ({'data_base64': '', 'byte_count': 1, 'sha256': 'bad'}, 'integrity_error'),
    ({'data_base64': 1}, 'protocol_error'),
])
def test_result_contract_failures(value, verdict):
    action = 'read' if isinstance(value, dict) and 'data_base64' in value else 'core.status'
    with pytest.raises((client.AgentError, ValueError)) as error:
        remote_runtime._result(value, action, [])
    assert client.failure(error.value, False)[0] == verdict


def test_operator_can_choose_another_loopback_port(runtime):
    request = runtime.request()
    request['trust']['port'] = runtime.profile.port = 41282
    assert remote_runtime.run(request)['ok'] is True
    assert runtime.records['transports'][0][0].port == 41282


def test_public_key_must_be_p384():
    with pytest.raises(ValueError):
        remote_runtime._public_key(public_key(ec.generate_private_key(ec.SECP256R1())))


@pytest.fixture
def sdk_tree(tmp_path, monkeypatch):
    monkeypatch.setenv('GAIAKEEP_PURE_PYTHON', os.environ.get('GAIAKEEP_PURE_PYTHON', ''))
    data = {'gaiakeep/__init__.py': b'', 'gaiakeep/client.py': b'CoreClient = object\nTimeouts = dict\n',
            'gaiakeep/errors.py': b'GaiaKeepError = Exception\nraise_for = str\n',
            'gaiakeep/profile.py': b'Profile = dict\n', 'gaiakeep/keys.py': b'parse_private = bytes\n',
            'gaiakeep/ops.py': b'def known_collections(core, with_heads=True):\n return {"with_heads":with_heads}\n',
            'gaiakeep/core_actions.json': b'{}'}
    for name, value in data.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(value)
    lock = {'distribution': 'gaiakeep', 'version': '0.2.0',
            'files': {name: hashlib.sha256(value).hexdigest() for name, value in data.items()}}
    monkeypatch.setattr(remote_runtime, 'SDK_LOCK', lock)
    monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: SimpleNamespace(
        version='0.2.0', locate_file=lambda path: tmp_path / path))
    for name in list(sys.modules):
        if name == 'gaiakeep' or name.startswith('gaiakeep.'):
            monkeypatch.delitem(sys.modules, name)
    finders = list(sys.meta_path)
    yield SimpleNamespace(root=tmp_path, data=data, lock=lock)
    sys.meta_path[:] = finders
    for name in list(sys.modules):
        if name == 'gaiakeep' or name.startswith('gaiakeep.'):
            sys.modules.pop(name)


def test_exact_sdk_source_import_ignores_cached_bytecode(sdk_tree):
    out = remote_runtime._validated_sdk()
    assert tuple(out)[:4] == (object, dict, Exception, str)
    assert os.environ['GAIAKEEP_PURE_PYTHON'] == '1'
    assert remote_runtime._known_collections(SimpleNamespace(profile=SimpleNamespace(tenant=None))) == {'with_heads': False}
    finder = sys.meta_path[0]
    assert finder.find_spec('other') is None
    with pytest.raises(ImportError):
        finder.find_spec('gaiakeep.unpinned')
    snapshot = Path(out.snapshot.name)
    (sdk_tree.root / 'gaiakeep/core_actions.json').write_text('{"changed": true}')
    assert (snapshot / 'gaiakeep/core_actions.json').read_bytes() == b'{}'
    assert sys.modules['gaiakeep'].__file__.startswith(str(snapshot))
    out.close()
    assert not snapshot.exists() and not any(name == 'gaiakeep' for name in sys.modules)


def test_upload_top_level_id_cannot_drift_from_approved_id(runtime):
    request = runtime.request()
    request.update(tool='gaiakeep_upload_file', request_id='a' * 32,
                   arguments={'machine_id': 'm', 'collection_id': 'c', 'path': 'file',
                              'data_base64': '', 'request_id': 'b' * 32})
    assert remote_runtime.run(request)['verdict'] == 'invalid_argument'
    assert not runtime.records['calls']


def test_have_upload_rejects_unusable_id(runtime):
    request = runtime.request()
    request.update(tool='gaiakeep_upload_file', request_id='a' * 32,
                   arguments={'machine_id': 'm', 'collection_id': 'c', 'path': 'file',
                              'data_base64': '', 'strategy': 'have'})
    assert remote_runtime.run(request)['verdict'] == 'invalid_argument'
    request.pop('request_id')
    assert remote_runtime.run(request)['result']['publication']['vid'] == 'deduplicated'


def test_legal_shortening_never_loads_profile(runtime, monkeypatch):
    monkeypatch.setattr(remote_runtime, '_validated_sdk', lambda: pytest.fail('identity was loaded'))
    request = runtime.request()
    request.update(tool='gaiakeep_core_legalorder', arguments={'machine_id': 'm', 'params': {
        'tenant_id': 't', 'order_ref': 'o', 'authority': 'a', 'action': 'shorten'}})
    assert remote_runtime.run(request)['verdict'] == 'unsupported'


def test_sdk_snapshot_binding_closes_after_success(runtime, monkeypatch):
    sdk = remote_runtime._validated_sdk()
    closed = []
    class Binding:
        def __iter__(self):
            return iter(sdk)
        def close(self):
            closed.append(True)
    monkeypatch.setattr(remote_runtime, '_validated_sdk', Binding)
    assert remote_runtime.run(runtime.request())['ok']
    assert closed == [True]


@pytest.mark.parametrize('change', ['version', 'source', 'extra', 'missing', 'preloaded', 'import'])
def test_sdk_drift_is_refused(sdk_tree, monkeypatch, change):
    if change == 'version':
        monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: SimpleNamespace(version='0.1.0'))
    elif change == 'source':
        (sdk_tree.root / 'gaiakeep/client.py').write_text('changed = True')
    elif change == 'extra':
        (sdk_tree.root / 'gaiakeep/extra.py').write_text('unapproved = True')
    elif change == 'missing':
        (sdk_tree.root / 'gaiakeep/core_actions.json').unlink()
    elif change == 'preloaded':
        monkeypatch.setitem(sys.modules, 'gaiakeep', SimpleNamespace())
    else:
        monkeypatch.setattr(importlib, 'import_module', lambda name: (_ for _ in ()).throw(ImportError()))
    with pytest.raises(client.AgentError) as error:
        remote_runtime._validated_sdk()
    assert error.value.verdict == 'not_configured'


def test_private_file_requires_owner_only_permissions(tmp_path, monkeypatch):
    path = tmp_path / 'private'
    path.write_bytes(b'key')
    real = os.fstat
    monkeypatch.setattr(os, 'fstat', lambda descriptor: SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600, st_uid=1, st_size=3))
    monkeypatch.setattr(os, 'geteuid', lambda: 1, raising=False)
    assert remote_runtime._private_bytes(path, 16) == b'key'
    for mode, owner, size in [(stat.S_IFREG | 0o644, 1, 3), (stat.S_IFDIR | 0o700, 1, 3),
                               (stat.S_IFREG | 0o600, 2, 3), (stat.S_IFREG | 0o600, 1, 17)]:
        monkeypatch.setattr(os, 'fstat', lambda descriptor: SimpleNamespace(st_mode=mode, st_uid=owner, st_size=size))
        with pytest.raises(PermissionError):
            remote_runtime._private_bytes(path, 16)
    monkeypatch.setattr(os, 'fstat', real)


def test_private_file_growth_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'private'
    path.write_bytes(b'x' * 17)
    monkeypatch.setattr(os, 'fstat', lambda descriptor: SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600, st_uid=1, st_size=1))
    monkeypatch.setattr(os, 'geteuid', lambda: 1, raising=False)
    with pytest.raises(ValueError):
        remote_runtime._private_bytes(path, 16)
