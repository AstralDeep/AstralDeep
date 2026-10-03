"""Runs closed Gaia operations on the owner's login node with verified SDK bytes and operator-pinned gateway trust."""

from __future__ import annotations

import base64
import errno
import hashlib
import importlib
import importlib.abc
import importlib.metadata
import importlib.util
import json
import os
import re
import ssl
import stat
import sys
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agents.gaiakeep import catalog, client
from agents.gaiakeep.transport import FLOW_TIMEOUT, RPC_TIMEOUT, GatewayConfig

SDK_LOCK = json.loads(Path(__file__).with_name('sdk-artifact.json').read_text(encoding='utf-8'))


class VerifiedSource(importlib.abc.Loader):
    def __init__(self, source):
        self.source = source

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.source, module.__file__, 'exec'), module.__dict__)


class VerifiedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root, sources):
        self.root, self.sources = root, sources

    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'gaiakeep' and not fullname.startswith('gaiakeep.'):
            return None
        name = fullname.replace('.', '/')
        package = name + '/__init__.py'
        filename = package if package in self.sources else name + '.py'
        if filename not in self.sources:
            raise ImportError('An unpinned Gaia SDK module was requested.')
        return importlib.util.spec_from_file_location(
            fullname, str(self.root / filename), loader=VerifiedSource(self.sources[filename]),
            submodule_search_locations=[str(self.root / name)] if filename == package else None)


class SDKBinding:
    def __init__(self, snapshot, finder):
        self.snapshot, self.finder, self.values = snapshot, finder, ()

    def __iter__(self):
        return iter(self.values)

    def close(self):
        if self.finder in sys.meta_path:
            sys.meta_path.remove(self.finder)
        for name, module in list(sys.modules.items()):
            if ((name == 'gaiakeep' or name.startswith('gaiakeep.'))
                    and str(getattr(module, '__file__', '')).startswith(self.snapshot.name + os.sep)):
                sys.modules.pop(name, None)
        self.snapshot.cleanup()


def _validated_sdk():
    binding = None
    try:
        distribution = importlib.metadata.distribution(SDK_LOCK['distribution'])
        if distribution.version != SDK_LOCK['version']:
            raise ValueError
        root = Path(distribution.locate_file('')).resolve()
        package = root / 'gaiakeep'
        expected = SDK_LOCK['files']
        actual = {p.relative_to(root).as_posix() for p in package.rglob('*.py')}
        if actual != {name for name in expected if name.endswith('.py')}:
            raise ValueError
        sources, verified = {}, {}
        for name, digest in expected.items():
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(package.resolve()):
                raise ValueError
            data = _file_bytes(path, 2 << 20)
            if len(data) > 2 << 20 or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError
            verified[name] = data
            if name.endswith('.py'):
                sources[name] = data
        if any(name == 'gaiakeep' or name.startswith('gaiakeep.') for name in sys.modules):
            raise ValueError
        os.environ['GAIAKEEP_PURE_PYTHON'] = '1'
        snapshot = tempfile.TemporaryDirectory(prefix='astral-gaia-sdk-')
        snapshot_root = Path(snapshot.name)
        finder = VerifiedFinder(snapshot_root, sources)
        binding = SDKBinding(snapshot, finder)
        for name, data in verified.items():
            path = snapshot_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        sys.meta_path.insert(0, finder)
        try:
            sdk = importlib.import_module('gaiakeep.client')
            errors = importlib.import_module('gaiakeep.errors')
            profiles = importlib.import_module('gaiakeep.profile')
            keys = importlib.import_module('gaiakeep.keys')
        except Exception:
            binding.close()
            binding = None
            raise
        binding.values = sdk.CoreClient, sdk.Timeouts, errors.GaiaKeepError, errors.raise_for, profiles, keys
        return binding
    except (OSError, ImportError, ValueError, KeyError) as exc:
        if binding is not None:
            binding.close()
        raise client.AgentError('not_configured', 'Install the exact qualified GaiaKeep client in your DGX account.') from exc


def _file_bytes(path, limit, private=False):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > limit
                or (private and (info.st_mode & 0o077
                    or (hasattr(os, 'geteuid') and info.st_uid != os.geteuid())))):
            raise PermissionError
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError
        return data
    finally:
        os.close(descriptor)


def _private_bytes(path, limit):
    return _file_bytes(path, limit, private=True)


def _public_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,300}={0,2}', value):
        raise ValueError
    der = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    key = serialization.load_der_public_key(der)
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != 'secp384r1':
        raise ValueError
    return der


def _trust(value):
    fields = {'core_address', 'allowed_peers', 'tls_name', 'ca_pem', 'core_public_key',
              'port', 'allow_legacy', 'restore_root'}
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError
    peers = value.get('allowed_peers')
    if (not isinstance(peers, list) or not 1 <= len(peers) <= 16
            or value.get('core_address') not in peers or type(value.get('port')) is not int
            or not 1 <= value['port'] <= 65535 or type(value.get('allow_legacy', False)) is not bool
            or not isinstance(value.get('restore_root', ''), str)):
        raise ValueError
    for peer in peers:
        if not isinstance(peer, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}:[A-Za-z0-9_.-]{1,128}:[A-Za-z0-9_.-]{1,128}', peer):
            raise ValueError
    name, pem = value.get('tls_name'), value.get('ca_pem')
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', name)
            or not isinstance(pem, str) or not 1 <= len(pem) <= 65536):
        raise ValueError
    _public_key(value.get('core_public_key'))
    return value


def _profile_trust(profile, trust):
    if (profile.core_addr != trust['core_address']
            or _public_key(profile.core_public_key) != _public_key(trust['core_public_key'])
            or not set(profile.read_peers or []).issubset(trust['allowed_peers'])
            or profile.port != trust['port'] or not isinstance(profile.service_key, str)
            or not 1 <= len(profile.service_key) <= 4096 or re.search(r'[\r\n]', profile.service_key)
            or not isinstance(profile.principal, str) or not 1 <= len(profile.principal) <= 256
            or re.search(r'[\x00-\x1f\x7f]', profile.principal)):
        raise client.AgentError('not_configured', 'Your Gaia profile does not match the qualified deployment trust.')


def _result(value, action, secrets):
    encoded = None
    if action == 'read' and isinstance(value, dict):
        value = dict(value)
        encoded = value.pop('data_base64', None)
        if not isinstance(encoded, str) or len(encoded) > 4 * ((catalog.MAX_FILE + 2) // 3):
            raise client.AgentError('protocol_error', 'Invalid bounded Gaia file result.')
        data = base64.b64decode(encoded, validate=True)
        if (len(data) > catalog.MAX_FILE or type(value.get('byte_count')) is not int or value['byte_count'] != len(data)
                or value.get('sha256') != hashlib.sha256(data).hexdigest()):
            raise client.AgentError('integrity_error', 'Gaia file result failed its byte integrity check.')
    out = client.clean_result(value, secrets)
    if not isinstance(out, dict) or len(json.dumps(out, allow_nan=False).encode()) > catalog.MAX_RPC:
        raise client.AgentError('protocol_error', 'The Gaia result exceeds its bounded object contract.')
    if action == 'read':
        out['data_base64'] = encoded
    return out


def _known_collections(core):
    from gaiakeep.ops import known_collections

    return known_collections(core, with_heads=True)


def _sdk_transport(config, context):
    from gaiakeep.errors import RpcTimeout, TransportError, from_reply
    from websockets.exceptions import ConnectionClosed

    from agents.gaiakeep.transport import ConnectionOpenTimeout, VerifiedLoopbackTransport

    class SDKTransport(VerifiedLoopbackTransport):
        def call(self, addr, action, params, timeout):
            try:
                reply = super().call(addr, action, params, timeout)
            except (ssl.SSLError, PermissionError):
                raise
            except ConnectionClosed as exc:
                codes = [close.code for close in (exc.rcvd, exc.sent) if close is not None]
                if (time.monotonic() >= self.deadline
                        or any(code not in {1001, 1006, 1011, 1012, 1013, 1014} for code in codes)):
                    raise
                raise TransportError('Gaia RPC connection closed.', action=action) from exc
            except OSError as exc:
                if time.monotonic() >= self.deadline:
                    raise
                if isinstance(exc, ConnectionOpenTimeout) and catalog.ACTIONS.get(action, {}).get('read') is True:
                    raise TransportError('Gaia RPC connection could not be opened.', action=action) from exc
                if isinstance(exc, TimeoutError) or exc.errno == errno.ETIMEDOUT:
                    if catalog.ACTIONS.get(action, {}).get('read') is True:
                        raise RpcTimeout('Gaia RPC timed out.', action=action) from exc
                    raise TransportError('Gaia RPC timed out.', action=action) from exc
                if (isinstance(exc, (ConnectionAbortedError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError))
                        or exc.errno in {errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET, errno.EPIPE,
                                         errno.ENETRESET, errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH}):
                    raise TransportError('Gaia RPC connection failed.', action=action) from exc
                raise
            if catalog.ACTIONS.get(action, {}).get('read') is not True:
                error = from_reply(action, reply)
                if isinstance(error, RpcTimeout):
                    raise TransportError('Gaia RPC outcome is uncertain.', action=action) from error
            return reply

    return SDKTransport(config, tls_context=context)


def run(request):
    started = time.monotonic()
    mutation = dispatched = False
    core = transport = binding = None
    try:
        if not isinstance(request, dict) or set(request) - {'tool', 'arguments', 'trust', 'request_id'}:
            raise ValueError
        name, arguments = request.get('tool'), request.get('arguments')
        catalog.validate(name, arguments)
        action = catalog.TOOLS[name]['action']
        mutation = catalog.is_mutation(name)
        trust = _trust(request.get('trust'))
        if action == 'fetch' or (catalog.ACTIONS.get(action, {}).get('legacy') and not trust.get('allow_legacy', False)):
            raise client.AgentError('unsupported', 'This Gaia operation is not enabled for the qualified deployment.')
        if action == 'core.legalorder' and arguments['params'].get('action', 'erase') != 'erase':
            raise client.AgentError('unsupported', 'GaiaKeep cannot currently sign legal-order shortening.')
        request_id = request.get('request_id')
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', request_id)):
            raise ValueError
        if request_id is not None and (action != 'upload' or arguments.get('strategy', 'ingest') == 'have'):
            raise ValueError
        if arguments.get('request_id') is not None and arguments['request_id'] != request_id:
            raise ValueError
        if action == 'upload' and arguments.get('strategy', 'ingest') == 'ingest' and request_id is None:
            raise ValueError
        binding = _validated_sdk()
        CoreClient, Timeouts, GaiaKeepError, raise_for, profiles, keys = binding
        profile_path = Path.home() / '.gaiakeep' / 'gaiakeep-profile.json'
        profile = profiles.Profile(json.loads(_private_bytes(profile_path, 65536)), str(profile_path))
        _profile_trust(profile, trust)
        private = _private_bytes(profile.key_file, 16384)
        key = keys.parse_private(private)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != 'secp384r1':
            raise client.AgentError('auth_failed', 'Your Gaia account requires its existing P-384 private key.')
        context = ssl.create_default_context(cadata=trust['ca_pem'])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        config = GatewayConfig(trust['core_address'], frozenset(trust['allowed_peers']), trust['tls_name'], '',
                               trust['core_public_key'], profile.service_key, trust['port'],
                               trust.get('allow_legacy', False), trust.get('restore_root', ''))
        transport = _sdk_transport(config, context)
        transport.deadline = started + FLOW_TIMEOUT
        core = CoreClient(
            transport, trust['core_address'], principal=profile.principal, key=key, key_file=None,
            core_public_key=trust['core_public_key'], flows=1, chunk=1 << 20, window_bytes=4 << 20,
            redirects=2, redirect_wait=0, read_peers=profile.read_peers, busy_backoff_max=2,
            timeouts=Timeouts(c0=RPC_TIMEOUT, c1=RPC_TIMEOUT, open=RPC_TIMEOUT, commit=RPC_TIMEOUT,
                              commit_wait=FLOW_TIMEOUT, flow_stall=20, flow_max=FLOW_TIMEOUT,
                              legacy_min=RPC_TIMEOUT, legacy_max=RPC_TIMEOUT, sync_verb=RPC_TIMEOUT))
        core.profile = profile
        transport.remaining(RPC_TIMEOUT)
        dispatched = True
        if action == 'connection_info':
            result = {'principal': profile.principal, 'tenant': profile.tenant,
                      'default_collection': profile.default_collection, 'default_domain': profile.default_domain,
                      'client_version': SDK_LOCK['version']}
        elif action == 'list_collections':
            result = _known_collections(core)
        elif action == 'read':
            result = client.read(core, arguments['vid'], arguments['path'])
        elif action == 'upload':
            result = client.upload(core, arguments['collection_id'], arguments['path'], arguments['data_base64'],
                                   arguments.get('branch', 'main'), arguments.get('strategy', 'ingest'),
                                   arguments.get('base_vid'), arguments.get('expected_head'), request_id)
        else:
            result = client.execute(core, action, arguments['params'], config, {},
                                    sdk_loader=lambda: (CoreClient, Timeouts, GaiaKeepError, raise_for))
        secrets = [profile.service_key, private.decode('ascii', 'ignore'), base64.b64encode(private).decode(), str(profile_path), profile.key_file]
        result = _result(result, action, secrets)
        core.close()
        core = None
        transport.close()
        transport = None
        if hasattr(binding, 'close'):
            binding.close()
        binding = None
        return {'ok': True, 'result': result}
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, PermissionError) and not dispatched:
            exc = client.AgentError('auth_failed', 'Your Gaia profile and key must be private files owned by your DGX account.')
        elif isinstance(exc, FileNotFoundError) and not dispatched:
            exc = client.AgentError('not_configured', 'Run the Gaia client setup in your DGX account first.')
        elif isinstance(exc, (ValueError, TypeError, KeyError)) and not dispatched:
            exc = client.AgentError('invalid_argument', 'The Gaia request or account configuration is invalid.')
        verdict, message = client.failure(exc, mutation and dispatched)
        if mutation and dispatched and verdict in {'protocol_error', 'integrity_error'}:
            verdict, message = 'unconfirmed', 'The operation may have taken effect. Check native state before retrying.'
        return {'ok': False, 'verdict': verdict, 'message': message}
    finally:
        for item in (core, transport, binding):
            if item is not None:
                try:
                    if hasattr(item, 'close'):
                        item.close()
                except Exception:  # noqa: BLE001
                    pass
