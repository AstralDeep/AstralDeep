"""Executes the reviewed Gaia adapter under the owner's pinned SSH account with bounded stdin and results.
The installed Gaia signing profile and key remain on the login node.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
from contextlib import contextmanager
from pathlib import Path

from orchestrator.remote_transport import HostKeyMismatch, ParamikoTransport

from agents.gaiakeep import catalog
from agents.gaiakeep.transport import FLOW_TIMEOUT, MAX_RPC, GatewayConfig, ProtocolError, validate_failure_detail

MAX_WIRE = 13 << 20
MAX_STDERR = 16384
BUNDLE_FILES = ('catalog.py', 'client.py', 'transport.py', 'remote_runtime.py',
                'capabilities.json', 'sdk-artifact.json')


def _bundle():
    root = Path(__file__).parent
    return {name: (root / name).read_text(encoding='utf-8') for name in BUNDLE_FILES}


def _command(bundle):
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    bootstrap = f'''import hashlib,json,os,signal,sys,tempfile
def expire(*args):
 raise TimeoutError("Gaia process deadline reached.")
signal.signal(signal.SIGALRM,expire);signal.alarm(110)
payload=json.loads(sys.stdin.buffer.read({MAX_WIRE + 1}))
bundle=payload.pop("bundle")
assert hashlib.sha256(json.dumps(bundle,sort_keys=True,separators=(",",":")).encode()).hexdigest()=={digest!r}
with tempfile.TemporaryDirectory(prefix="astral-gaia-") as root:
 os.chmod(root,0o700)
 directory=os.path.join(root,"agents","gaiakeep")
 os.makedirs(directory)
 for package in (os.path.join(root,"agents"),directory):
  open(os.path.join(package,"__init__.py"),"w").close()
 for name,source in bundle.items():
  with open(os.path.join(directory,name),"w",encoding="utf-8") as file: file.write(source)
 sys.path.insert(0,root)
 from agents.gaiakeep.remote_runtime import run
 result=run(payload)
 encoded=json.dumps(result,allow_nan=False,separators=(",",":")).encode()
 assert len(encoded)<={MAX_WIRE}
 sys.stdout.buffer.write(encoded)
 sys.stdout.buffer.flush()
'''
    return 'exec "$HOME/.gaiakeep/venv/bin/python" -I -c ' + shlex.quote(bootstrap)


def _trust():
    from agents.gaiakeep.client import AgentError

    names = ('GAIAKEEP_CORE_ADDRESS', 'GAIAKEEP_ALLOWED_PEERS', 'GAIAKEEP_GATEWAY_TLS_NAME',
             'GAIAKEEP_GATEWAY_CA_FILE', 'GAIAKEEP_CORE_PUBLIC_KEY')
    try:
        address, peers, name, ca, key = (os.environ[field].strip() for field in names)
        allowed = frozenset(p.strip() for p in peers.split(',') if p.strip())
        if (not all((address, allowed, name, ca, key)) or address not in allowed or len(allowed) > 16
                or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', name)):
            raise ValueError
        if any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}:[A-Za-z0-9_.-]{1,128}:[A-Za-z0-9_.-]{1,128}', p)
               for p in allowed):
            raise ValueError
        path = Path(ca)
        if path.stat().st_size > 65536:
            raise ValueError
        pem = path.read_text(encoding='ascii')
        port = int(os.getenv('GAIAKEEP_GATEWAY_PORT', '28282'))
        if not 1 <= port <= 65535:
            raise ValueError
        config = GatewayConfig(address, allowed, name, ca, key, '', port,
                               os.getenv('GAIAKEEP_ALLOW_LEGACY', '').lower() == 'true',
                               os.getenv('GAIAKEEP_RESTORE_ROOT', ''))
        config.tls_context()
        return config, {'core_address': address, 'allowed_peers': sorted(allowed), 'tls_name': name,
                        'ca_pem': pem, 'core_public_key': key, 'port': port,
                        'allow_legacy': config.allow_legacy, 'restore_root': config.restore_root}
    except (KeyError, ValueError, OSError, UnicodeError) as exc:
        raise AgentError('not_configured', 'Configure GaiaKeep gateway certificate, hostname and core identity.') from exc


def _exchange(target, bundle, request):
    if not target.host_key_fingerprint:
        raise HostKeyMismatch('A pinned SSH host key is required.')
    wire = json.dumps(dict(request, bundle=bundle), allow_nan=False, separators=(',', ':')).encode()
    if len(wire) > MAX_WIRE:
        raise ProtocolError('The Gaia request exceeds the SSH bound.')
    deadline = time.monotonic() + FLOW_TIMEOUT
    connection = channel = None
    try:
        connection, _ = ParamikoTransport()._connect(target, min(10, deadline - time.monotonic()))
        channel = connection.get_transport().open_session(timeout=min(10, deadline - time.monotonic()))
        channel.settimeout(min(10, deadline - time.monotonic()))
        channel.exec_command(_command(bundle))
        channel.settimeout(0)
        sent = stderr_size = 0
        output = bytearray()
        shutdown = False
        while time.monotonic() < deadline:
            if sent < len(wire) and channel.send_ready():
                try:
                    count = channel.send(wire[sent:sent + 65536])
                except TimeoutError:
                    count = None
                if count == 0:
                    raise EOFError('Gaia SSH input closed.')
                sent += count or 0
            if sent == len(wire) and not shutdown:
                channel.shutdown_write()
                shutdown = True
            if channel.recv_ready():
                output.extend(channel.recv(65536))
                if len(output) > MAX_WIRE:
                    raise ProtocolError('The Gaia response exceeds the SSH bound.')
            if channel.recv_stderr_ready():
                stderr_size += len(channel.recv_stderr(65536))
                if stderr_size > MAX_STDERR:
                    raise ProtocolError('The Gaia process exceeded its diagnostic bound.')
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                if not shutdown or channel.recv_exit_status() != 0:
                    raise ProtocolError('The Gaia process ended without a confirmed result.')
                return bytes(output)
            time.sleep(0.01)
        raise TimeoutError('The Gaia SSH operation exceeded its deadline.')
    finally:
        try:
            if channel is not None:
                channel.close()
        finally:
            if connection is not None:
                connection.close()


class RemoteCore:
    def __init__(self, target, trust):
        self.target, self.trust = target, trust

    def perform(self, action, arguments, request_id=None):
        from agents.gaiakeep.client import AgentError

        tool = ('gaiakeep_read_file' if action == 'read' else 'gaiakeep_upload_file' if action == 'upload'
                else 'gaiakeep_' + action.replace('.', '_'))
        public = dict(arguments, machine_id='ssh-owner')
        catalog.validate(tool, public)
        request = {'tool': tool, 'arguments': public, 'trust': self.trust}
        if request_id is not None:
            request['request_id'] = request_id
        result_wire = _exchange(self.target, _bundle(), request)
        if action != 'read' and len(result_wire) > MAX_RPC:
            raise ProtocolError('The Gaia result exceeds the RPC response bound.')
        try:
            reply = json.loads(result_wire)
            if not isinstance(reply, dict) or type(reply.get('ok')) is not bool:
                raise ValueError
            if not reply['ok']:
                verdict = reply.get('verdict')
                if verdict not in {'not_configured', 'auth_failed', 'integrity_error', 'upstream_denied',
                                   'invalid_argument', 'protocol_error', 'unsupported', 'unconfirmed', 'unavailable'}:
                    raise ValueError
                messages = {'not_configured': 'The DGX Gaia client or trust configuration needs attention.',
                            'auth_failed': 'GaiaKeep authentication failed.',
                            'integrity_error': 'GaiaKeep data or identity verification failed.',
                            'upstream_denied': 'GaiaKeep refused this operation.',
                            'invalid_argument': 'GaiaKeep rejected the supplied arguments.',
                            'protocol_error': 'GaiaKeep returned an invalid response.',
                            'unsupported': 'This Gaia operation is not enabled or supported.',
                            'unconfirmed': 'The operation may have taken effect. Check native state before retrying.',
                            'unavailable': 'GaiaKeep is currently unavailable.'}
                raise AgentError(verdict, messages[verdict], validate_failure_detail(reply.get('failure_detail')))
            result = reply['result']
            if not isinstance(result, dict):
                raise ValueError
            if action == 'read':
                import base64

                data = base64.b64decode(result['data_base64'], validate=True)
                if (len(data) > catalog.MAX_FILE or type(result.get('byte_count')) is not int
                        or len(data) != result['byte_count'] or hashlib.sha256(data).hexdigest() != result.get('sha256')
                        or result.get('vid') != arguments['vid'] or result.get('path') != arguments['path']):
                    raise AgentError('integrity_error', 'GaiaKeep file verification failed.')
            return result
        except (KeyError, ValueError, TypeError, RecursionError) as exc:
            raise ProtocolError('The Gaia SSH response is malformed.') from exc


@contextmanager
def open_remote_client(target):
    config, trust = _trust()
    if not target.host_key_fingerprint:
        raise HostKeyMismatch('A pinned SSH host key is required.')
    yield RemoteCore(target, trust), config
