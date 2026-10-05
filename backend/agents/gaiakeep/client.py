"""Composes the pinned SDK with native trust and bounds, preserving owner credentials and truthful operation outcomes."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import importlib.metadata
import json
import math
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agents.gaiakeep.catalog import (
    ACTIONS,
    FILE_ACTIONS,
    MAX_FILE,
    MODERN_PROTOTYPE,
    REVISION,
    relative_path,
)
from agents.gaiakeep.transport import (
    FLOW_TIMEOUT,
    MAX_EXPANDED,
    MAX_RPC,
    RPC_TIMEOUT,
    GatewayConfig,
    NativeTransport,
    ProtocolError,
    decode_compressed,
    validate_failure_detail,
)

__all__ = [
    'MAX_FILE',
    'MAX_RPC',
    'AgentError',
    'clean_result',
    'credential_identity',
    'execute',
    'failure',
    'load_sdk',
    'open_client',
    'prepare_params',
    'read',
    'upload',
]


class AgentError(Exception):
    def __init__(self, verdict, message, detail=None, reconciliation=None):
        super().__init__(message)
        self.verdict = verdict
        self._gaiakeep_failure_detail = validate_failure_detail(detail)
        self.reconciliation = native_reconciliation(reconciliation)


def native_reconciliation(value):
    if not isinstance(value, dict):
        value = getattr(value, 'reconciliation', None) or getattr(value, 'detail', None)
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key in {'commit_job', 'job_id', 'vid'}
            and isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', item)}


def publication_result(core, value):
    result = dataclasses.asdict(value) if dataclasses.is_dataclass(value) else dict(vars(value))
    publications = getattr(getattr(core, 'transport', None), 'publication_metadata', {})
    for field in ('request_id', 'upload_id', 'vid'):
        metadata = publications.get(result.get(field), {})
        if metadata and ('vid' not in metadata or metadata['vid'] == result.get('vid')):
            result.update(metadata)
    return result


def observe_native_reply(transport, action, params, reply):
    status = reply.get('status')
    metadata = native_reconciliation(reply)
    if status in {16, '16'}:
        raise AgentError('pending', 'This version is awaiting verified durable copies. Check its commit job before reading; do not republish it.',
                         {'native_phase': 'rpc_response', 'failure_kind': 'native', 'native_status': 16}, metadata)
    if status in {10, '10'} and action in {'core.commit', 'core.publish'}:
        if 'commit_job' in reply and 'commit_job' not in metadata:
            raise AgentError('unconfirmed', 'The publication job could not be validated. Reconcile the retained request before any further publication.',
                             {'native_phase': 'rpc_response', 'failure_kind': 'protocol', 'native_status': 10}, metadata)
        for identifier in (params.get('request_id'), params.get('upload_id'), metadata.get('vid')):
            if isinstance(identifier, str):
                transport.publication_metadata[identifier] = metadata


def load_sdk():
    try:
        distribution = importlib.metadata.distribution('gaiakeep')
        origin = json.loads(distribution.read_text('direct_url.json') or '{}')
        if origin.get('vcs_info', {}).get('commit_id') != REVISION:
            raise ValueError
        from gaiakeep.client import CoreClient, Timeouts
        from gaiakeep.errors import GaiaKeepError, raise_for
        return CoreClient, Timeouts, GaiaKeepError, raise_for
    except (ImportError, ValueError) as exc:
        raise AgentError('not_configured', 'Install the exact pinned GaiaKeep optional dependency.') from exc


def credential_identity(credentials):
    principal = credentials.get('GAIAKEEP_PRINCIPAL')
    pem = credentials.get('GAIAKEEP_PRIVATE_KEY')
    if not isinstance(principal, str) or not 1 <= len(principal) <= 256:
        raise AgentError('not_configured', 'Add your GaiaKeep principal and private key in agent credentials.')
    if not isinstance(pem, str) or len(pem) > 16384:
        raise AgentError('not_configured', 'Add your GaiaKeep principal and private key in agent credentials.')
    try:
        encoded = pem.encode() if pem.lstrip().startswith('-----BEGIN') else base64.b64decode(pem, validate=True)
        key = serialization.load_pem_private_key(encoded, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != 'secp384r1':
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise AgentError('auth_failed', 'Use an unencrypted P-384 PEM key or its single-line base64 encoding.') from exc
    return principal, key


@contextmanager
def open_client(target, credentials, config=None):
    mode = os.getenv('GAIAKEEP_CONNECTION_MODE', 'ssh')
    if config is None and mode == 'ssh':
        from agents.gaiakeep.remote_client import open_remote_client

        with open_remote_client(target) as opened:
            yield opened
        return
    if mode not in {'ssh', 'native'}:
        raise AgentError('not_configured', 'Select a supported GaiaKeep connection mode.')
    principal, key = credential_identity(credentials)
    CoreClient, Timeouts, _, _ = load_sdk()
    try:
        config = config or GatewayConfig.from_environment()
        transport = NativeTransport(target, config)
    except (ValueError, OSError) as exc:
        raise AgentError('not_configured', 'Configure the existing Gaia gateway, trusted CA and core identity.') from exc
    core = None
    try:
        core = CoreClient(transport, config.core_address, principal=principal, key=key,
                          core_public_key=config.core_public_key, flows=2, chunk=1 << 20,
                          window_bytes=4 << 20, redirects=2, redirect_wait=0,
                          timeouts=Timeouts(c0=RPC_TIMEOUT, c1=RPC_TIMEOUT, open=RPC_TIMEOUT,
                                            commit=RPC_TIMEOUT, commit_wait=FLOW_TIMEOUT,
                                            flow_stall=20, flow_max=FLOW_TIMEOUT,
                                            legacy_min=RPC_TIMEOUT, legacy_max=RPC_TIMEOUT,
                                            sync_verb=RPC_TIMEOUT))
        yield core, config
    finally:
        try:
            if core is not None:
                core.close()
        finally:
            transport.close()


def clean_result(value, secrets=(), depth=0, decode_strings=True):
    if depth > 16:
        raise AgentError('protocol_error', 'The Gaia response exceeds the nesting bound.')
    if isinstance(value, dict):
        if len(value) > 10000:
            raise AgentError('protocol_error', 'The Gaia response exceeds the entry bound.')
        out = {clean_result(str(k), secrets, depth + 1, decode_strings): clean_result(v, secrets, depth + 1, decode_strings) for k, v in value.items()
               if not re.search(r'(^sig($|_)|token|secret|private|service_key|(^|_)grant$)', str(k), re.IGNORECASE)}
        if value.get('certificate_verified') is True and 'certificate_signature' in value and out != value:
            return {'certificate_verified': False, 'leaf_inclusion_verified': False, 'artifact_omitted': True}
        return out
    if isinstance(value, list):
        if len(value) > 10000:
            raise AgentError('protocol_error', 'The Gaia response exceeds the entry bound.')
        return [clean_result(v, secrets, depth + 1, decode_strings) for v in value]
    if isinstance(value, str):
        if len(value.encode()) > MAX_EXPANDED:
            raise AgentError('protocol_error', 'The Gaia response exceeds the text bound.')
        if decode_strings and value.startswith('H4sI'):
            return clean_result(decode_compressed(value), secrets, depth + 1)
        if decode_strings and value.lstrip().startswith(('{', '[')):
            try:
                return clean_result(json.loads(value), secrets, depth + 1)
            except (ValueError, RecursionError):
                pass
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[redacted]')
        value = re.sub(r'-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----',
                       '[redacted]', value, flags=re.DOTALL)
        return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise AgentError('protocol_error', 'Non-finite Gaia response value.')
        return value
    raise AgentError('protocol_error', 'Unsupported Gaia response value.')


def clean_dataset_result(value, secrets=()):
    result = clean_result(value, secrets, decode_strings=False)
    if 'files' in value and value['files'] != result.get('files'):
        raise AgentError('protocol_error', 'The dataset manifest could not be safely represented without changing its fingerprint.')
    return result


def prepare_params(action, params):
    out = dict(params)
    fields = {p['name'] for p in ACTIONS[action]['parameters']}
    if 'request_id' in fields and 'request_id' not in out:
        out['request_id'] = uuid.uuid4().hex
    out.pop('events', None)
    return out


def _info(*parts):
    encoded = [part.encode() for part in parts]
    return b''.join(len(part).to_bytes(4, 'big') + part for part in encoded)


def _certificate(core, reply, extract_id):
    try:
        cert, signature = reply['cert'], reply['sig']
        signer = reply.get('core_public_key', reply.get('signer_pub'))
        if 'core_public_key' in reply and 'signer_pub' in reply and reply['core_public_key'] != reply['signer_pub']:
            raise ValueError
        if not isinstance(cert, str) or len(cert.encode()) > 65536 or len(signature) > 300 or len(signer) > 300:
            raise ValueError
        signer_bytes = base64.urlsafe_b64decode(signer + '=' * (-len(signer) % 4))
        pinned = core.core_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if signer_bytes != pinned:
            raise ValueError
        signature_bytes = base64.urlsafe_b64decode(signature + '=' * (-len(signature) % 4))
        core.core_key.verify(signature_bytes, _info('GFS-XCERT-1', cert), ec.ECDSA(hashes.SHA384()))
        if extract_id != hashlib.sha384(_info('gfs/extract/v2', cert)).hexdigest()[:48]:
            raise ValueError
        return {'certificate_verified': True, 'leaf_inclusion_verified': False,
                'certificate_utf8_base64': base64.b64encode(cert.encode()).decode(),
                'certificate_signature': signature, 'certificate_public_key': signer}
    except (KeyError, ValueError, TypeError, InvalidSignature) as exc:
        raise AgentError('integrity_error', 'GaiaKeep extraction certificate failed its pinned identity check.') from exc


def execute(core, action, params, config=None, credentials=None, sdk_loader=None):
    if action == 'fetch':
        raise AgentError('unsupported', 'Legacy fetch requires a separately qualified bounded receiver. Use the versioned file read tool.')
    if action in FILE_ACTIONS:
        raise AgentError('invalid_argument', 'Use the bounded GaiaKeep file tools for encrypted transfer primitives.')
    params = prepare_params(action, params)
    if action == 'core.legalorder':
        if params.get('action', 'erase') != 'erase':
            raise AgentError('unsupported', 'GaiaKeep cannot currently sign legal-order shortening.')
        params.pop('action', None)
    if ACTIONS[action]['legacy']:
        if config is None or not config.allow_legacy:
            raise AgentError('unsupported', 'The operator has not enabled GaiaKeep legacy actions.')
        principal = core.signer.principal
        if action in MODERN_PROTOTYPE:
            identity_field = 'approver' if action == 'approvereconstruct' else 'user'
            params[identity_field] = principal
            status = core.transport.call(config.core_address, 'core.status', {}, RPC_TIMEOUT)
            audience = status.get('peer_audience')
            if not audience:
                raise AgentError('protocol_error', 'GaiaKeep did not provide the required peer audience.')
            signed = core.signer.sign(action, params, audience=audience)
            reply = core.transport.call(config.core_address, action, signed, RPC_TIMEOUT)
        else:
            if action == 'restore':
                root = PurePosixPath(config.restore_root)
                if not root.is_absolute() or '..' in root.parts or str(root) == '/':
                    raise AgentError('not_configured', 'An administrator-controlled restore root is required.')
                params['dest_path'] = str(root / relative_path(params['dest_path']))
                params['user'] = principal
                token = (credentials or {}).get('GAIAKEEP_RECONSTRUCTION_TOKEN')
                if token:
                    params['reconstruct_token'] = token
            reply = core.transport.call(config.core_address, action, params, RPC_TIMEOUT)
        _, _, _, raise_for = (sdk_loader or load_sdk)()
        reply = raise_for(action, reply)
    else:
        reply = core.call(action, params, timeout=RPC_TIMEOUT)
    if not isinstance(reply, dict):
        raise ProtocolError('Native reply must be an object.')
    if action == 'core.extractproof':
        reply['verified_certificate'] = _certificate(core, reply, params['extract_id'])
    for field in ACTIONS[action]['compressed_returns']:
        if reply.get(field):
            value = reply[field]
            try:
                reply[field] = json.loads(value) if isinstance(value, str) and value.lstrip().startswith(('{', '[')) else decode_compressed(value)
            except (ValueError, RecursionError) as exc:
                raise ProtocolError('Invalid native result content.') from exc
    return reply


def upload(core, collection_id, path, data_base64, branch, strategy='ingest', base_vid=None, expected_head=None,
           request_id=None, note=None):
    try:
        path = relative_path(path)
        data = base64.b64decode(data_base64, validate=True)
        if len(data) > MAX_FILE:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise AgentError('invalid_argument', 'Upload requires valid base64 data up to 8 MiB and a relative path.') from exc
    with tempfile.TemporaryDirectory(prefix='astral-gaia-') as directory:
        source = Path(directory) / 'payload'
        source.write_bytes(data)
        if strategy == 'have':
            reply, stats = core.have_ingest({path: str(source)}, collection_id=collection_id,
                                           branch=branch, expected_head=expected_head, note=note)
            return {'publication': reply, 'deduplication': stats}
        result = core.ingest({path: str(source)}, collection_id=collection_id, branch=branch,
                             request_id=request_id or uuid.uuid4().hex, mode='1a', legacy_publish='staged',
                             base_vid=base_vid, expected_head=expected_head, note=note)
        return publication_result(core, result)


def read(core, vid, path):
    data = core.read(vid, relative_path(path), max_bytes=MAX_FILE)
    if not isinstance(data, bytes) or len(data) > MAX_FILE:
        raise AgentError('protocol_error', 'GaiaKeep returned invalid or oversized file data.')
    return {'vid': vid, 'path': path, 'byte_count': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'data_base64': base64.b64encode(data).decode()}


def failure(exc, mutation):
    if isinstance(exc, AgentError):
        return exc.verdict, str(exc)
    name = type(exc).__name__
    if name in {'Unauthenticated', 'AuthenticationException'}:
        return 'auth_failed', 'GaiaKeep authentication failed.'
    if name in {'IntegrityError', 'InvalidSignature'}:
        return 'integrity_error', 'GaiaKeep data or identity verification failed.'
    if name == 'CommitPending':
        return 'pending', 'This version is awaiting verified durable copies. Check its commit job before reading; do not republish it.'
    if name in {'Forbidden', 'PolicyRefused', 'NotFound', 'BadRequest', 'Failed', 'StaleHead', 'RetryAfter'}:
        return 'upstream_denied', 'GaiaKeep refused the operation; inspect the native state before retrying.'
    if isinstance(exc, ValueError) and not mutation:
        return 'invalid_argument', 'Invalid GaiaKeep arguments or credentials.'
    if isinstance(exc, ProtocolError) and not mutation:
        return 'protocol_error', 'GaiaKeep returned an invalid native response.'
    if mutation:
        return 'unconfirmed', 'The operation may have taken effect. Check its native job or storage state before retrying.'
    return 'unavailable', 'The existing GaiaKeep interface could not be reached or did not answer.'
