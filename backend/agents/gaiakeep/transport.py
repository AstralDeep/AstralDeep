"""Provides bounded, correlated native RPC and verified TLS data streams through caller-owned SSH tunnels or the operator-configured loopback gateway."""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import re
import socket
import ssl
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

MAX_RPC = 1 << 20
MAX_EXPANDED = 4 << 20
MAX_FRAME = (1 << 20) + 65536
CONNECT_TIMEOUT = 10
RPC_TIMEOUT = 20
FLOW_TIMEOUT = 120


def __getattr__(name):
    if name == 'ParamikoTransport':
        from orchestrator.remote_transport import ParamikoTransport

        return ParamikoTransport
    if name == 'validate_egress_url':
        from shared.external_http import validate_egress_url

        return validate_egress_url
    raise AttributeError(name)


class ProtocolError(Exception):
    pass


class ConnectionOpenTimeout(TimeoutError):
    pass


def _json(value, limit=MAX_RPC):
    if not isinstance(value, str) or len(value.encode()) > limit:
        raise ProtocolError('Invalid or oversized native frame.')
    try:
        out = json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError('Invalid native JSON.') from exc
    if not isinstance(out, dict):
        raise ProtocolError('Native reply must be an object.')
    return out


def decode_compressed(value):
    try:
        if not isinstance(value, str) or len(value) > MAX_RPC:
            raise ValueError
        raw = base64.b64decode(value, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as reader:
            data = reader.read(MAX_EXPANDED + 1)
        if len(data) > MAX_EXPANDED:
            raise ValueError
        return json.loads(data)
    except (ValueError, OSError, EOFError, RecursionError) as exc:
        raise ProtocolError('Invalid or oversized compressed reply.') from exc


def _compressed(value):
    if not isinstance(value, str):
        return False
    try:
        raw = base64.b64decode(value)
    except ValueError:
        raw = b''
    return value.startswith('H4sI') or raw.startswith(b'\x1f\x8b')


@dataclass(frozen=True)
class GatewayConfig:
    core_address: str
    allowed_peers: frozenset[str]
    tls_name: str
    ca_file: str
    core_public_key: str
    service_key: str = field(repr=False)
    port: int = 8282
    allow_legacy: bool = False
    restore_root: str = ''

    @classmethod
    def from_environment(cls):
        names = ['GAIAKEEP_CORE_ADDRESS', 'GAIAKEEP_ALLOWED_PEERS', 'GAIAKEEP_GATEWAY_TLS_NAME',
                 'GAIAKEEP_GATEWAY_CA_FILE', 'GAIAKEEP_CORE_PUBLIC_KEY', 'CRESCO_SERVICE_KEY']
        values = [os.getenv(name, '').strip() for name in names]
        if not all(values):
            raise ValueError('Gateway trust configuration is incomplete.')
        address, peers, name, ca, key, service = values
        allowed = frozenset(p.strip() for p in peers.split(',') if p.strip())
        if address not in allowed or len(allowed) > 16:
            raise ValueError('The configured core must be an admitted peer.')
        for peer in allowed:
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}:[A-Za-z0-9_.-]{1,128}:[A-Za-z0-9_.-]{1,128}', peer):
                raise ValueError('Invalid core peer address.')
        port = int(os.getenv('GAIAKEEP_GATEWAY_PORT', '8282'))
        if not 1 <= port <= 65535 or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', name):
            raise ValueError('Invalid gateway configuration.')
        return cls(address, allowed, name, ca, key, service, port,
                   os.getenv('GAIAKEEP_ALLOW_LEGACY', '').lower() == 'true',
                   os.getenv('GAIAKEEP_RESTORE_ROOT', ''))

    def tls_context(self):
        context = ssl.create_default_context(cafile=self.ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context


class SocketBridge:
    def __init__(self, channel, deadline=None):
        self.socket, self.peer = socket.socketpair()
        self.channel = channel
        self.deadline = deadline or time.monotonic() + FLOW_TIMEOUT
        self.stopped = threading.Event()
        self.peer.settimeout(CONNECT_TIMEOUT)
        self.workers = [threading.Thread(target=self._pump, args=(self.peer, channel), daemon=True),
                        threading.Thread(target=self._pump, args=(channel, self.peer), daemon=True)]
        for worker in self.workers:
            worker.start()

    def _pump(self, source, destination):
        try:
            while not self.stopped.is_set() and time.monotonic() < self.deadline:
                try:
                    chunk = source.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                destination.sendall(chunk)
        except (OSError, EOFError):
            pass
        finally:
            self.close(join=False)

    def close(self, *, join=True):
        self.stopped.set()
        for connection in (self.socket, self.peer, self.channel):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if join:
            for worker in self.workers:
                if worker is not threading.current_thread():
                    worker.join(timeout=1)


class NativeTransport:
    max_rpc_seconds = RPC_TIMEOUT

    def __init__(self, target, config, tls_context=None):
        self.target, self.config = target, config
        self.context = tls_context or config.tls_context()
        if not self.context.check_hostname or self.context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError('Verified TLS is mandatory.')
        self.streams = []
        self.deadline = time.monotonic() + FLOW_TIMEOUT

    def remaining(self, cap):
        value = min(cap, self.deadline - time.monotonic())
        if value <= 0:
            raise TimeoutError('GaiaKeep operation deadline reached.')
        return value

    @contextmanager
    def _rpc_socket(self):
        with ExitStack() as stack:
            try:
                ws = stack.enter_context(self._socket('/api/apisocket'))
            except TimeoutError as exc:
                raise ConnectionOpenTimeout('The Gaia RPC connection did not open.') from exc
            yield ws

    @contextmanager
    def _socket(self, path):
        from orchestrator.remote_transport import ParamikoTransport
        from shared.external_http import validate_egress_url
        from websockets.sync.client import connect

        host = self.target.address
        authority = f'[{host}]' if ':' in host else host
        gate = globals().get('validate_egress_url', validate_egress_url)
        gate(f'https://{authority}:{self.config.port}')
        with ParamikoTransport().open_tunnel(self.target, self.config.port, timeout=CONNECT_TIMEOUT) as channel:
            bridge = SocketBridge(channel, self.deadline)
            ws = None
            try:
                ws = connect(f'wss://{self.config.tls_name}:{self.config.port}{path}',
                             sock=bridge.socket, ssl=self.context,
                             server_hostname=self.config.tls_name, proxy=None,
                             additional_headers={'cresco_service_key': self.config.service_key},
                             compression=None, max_size=MAX_FRAME, max_queue=8,
                             open_timeout=self.remaining(CONNECT_TIMEOUT), close_timeout=1)
                yield ws
            finally:
                try:
                    if ws is not None:
                        ws.close()
                finally:
                    bridge.close()

    def call(self, addr, action, params, timeout):
        if addr not in self.config.allowed_peers:
            raise ProtocolError('Core redirect is outside the admitted peer set.')
        region, agent, plugin = addr.split(':')
        seconds = self.remaining(min(timeout, RPC_TIMEOUT))
        rpc_id = uuid.uuid4().hex
        payload = dict(params, action=action, client_rpc_id=rpc_id)
        envelope = {'message_info': {'message_type': 'global_plugin_msgevent', 'message_event_type': 'EXEC',
                     'dst_region': region, 'dst_agent': agent, 'dst_plugin': plugin, 'is_rpc': 'true',
                     'rpc_timeout_ms': str(max(1000, int(seconds * 1000) - 1000))},
                    'message_payload': payload}
        wire = json.dumps(envelope, allow_nan=False)
        if len(wire.encode()) > MAX_RPC:
            raise ProtocolError('Native request exceeds the RPC bound.')
        with self._rpc_socket() as ws:
            ws.send(wire)
            reply = _json(ws.recv(timeout=self.remaining(seconds)))
        if reply.get('client_rpc_id') != rpc_id:
            raise ProtocolError('Native reply correlation failed.')
        reply.pop('client_rpc_id')
        expanded = 0
        for value in reply.values():
            if _compressed(value):
                try:
                    expanded += len(json.dumps(decode_compressed(value), allow_nan=False).encode())
                except (ValueError, RecursionError) as exc:
                    raise ProtocolError('Invalid compressed reply content.') from exc
                if expanded > MAX_EXPANDED:
                    raise ProtocolError('Compressed reply exceeds the aggregate expansion bound.')
        return reply

    def open_stream(self, name, on_frame):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,256}', name):
            raise ProtocolError('Invalid native stream identifier.')
        stream = NativeStream(self, name, on_frame)
        self.streams.append(stream)
        return stream

    def close(self):
        error = None
        for stream in self.streams:
            try:
                stream.close()
            except Exception as exc:  # noqa: BLE001
                error = error or exc
        self.streams.clear()
        if error is not None:
            raise error


class VerifiedLoopbackTransport(NativeTransport):
    def __init__(self, config, tls_context=None):
        if isinstance(config.port, bool) or not isinstance(config.port, int) or not 1 <= config.port <= 65535:
            raise ValueError('The verified loopback gateway requires a valid operator port.')
        super().__init__(None, config, tls_context)
        self.port = config.port

    @contextmanager
    def _socket(self, path):
        from websockets.sync.client import connect

        if isinstance(self.config.port, bool) or self.config.port != self.port:
            raise ValueError('The verified loopback gateway port changed during the operation.')
        if not self.context.check_hostname or self.context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError('Verified TLS is mandatory.')
        connection = socket.create_connection(('127.0.0.1', self.port),
                                              timeout=self.remaining(CONNECT_TIMEOUT))
        ws = None
        try:
            ws = connect(f'wss://{self.config.tls_name}:{self.port}{path}',
                         sock=connection, ssl=self.context,
                         server_hostname=self.config.tls_name, proxy=None,
                         additional_headers={'cresco_service_key': self.config.service_key},
                         compression=None, max_size=MAX_FRAME, max_queue=8,
                         open_timeout=self.remaining(CONNECT_TIMEOUT), close_timeout=1)
            yield ws
        finally:
            try:
                if ws is not None:
                    ws.close()
            finally:
                connection.close()


class NativeStream:
    def __init__(self, transport, name, handler):
        self.name, self.transport, self.handler = name, transport, handler or (lambda frame: None)
        self.manager = transport._socket('/api/dataplane')
        self.ws = self.manager.__enter__()
        self.error = None
        self.closed = False
        self.worker = None
        try:
            self.ws.send(json.dumps({'ident_key': 'stream_name', 'ident_id': name, 'io_type_key': 'type',
                                    'output_id': 'output', 'input_id': 'input'}))
            activation = _json(self.ws.recv(timeout=transport.remaining(CONNECT_TIMEOUT)))
            if activation.get('status_code') != '10':
                raise ProtocolError('Native stream activation was refused.')
            self.worker = threading.Thread(target=self._receive, daemon=True)
            self.worker.start()
        except Exception:
            self.close()
            raise

    def _receive(self):
        try:
            while not self.closed:
                frame = self.ws.recv(timeout=self.transport.remaining(FLOW_TIMEOUT))
                if not isinstance(frame, (str, bytes)) or len(frame) > MAX_FRAME:
                    raise ProtocolError('Native stream frame exceeds its bound.')
                self.handler(frame.encode() if isinstance(frame, str) else frame)
        except Exception as exc:  # noqa: BLE001
            self.error = exc
            self.ws.close()

    def send(self, frame):
        self.transport.remaining(FLOW_TIMEOUT)
        if self.closed or self.error is not None:
            raise ProtocolError('Native stream failed.')
        if not isinstance(frame, bytes) or len(frame) > MAX_FRAME:
            raise ProtocolError('Invalid native data frame.')
        self.ws.send(frame)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.ws.close()
        finally:
            try:
                if self.worker is not None and self.worker is not threading.current_thread():
                    self.worker.join(timeout=1)
            finally:
                self.manager.__exit__(None, None, None)
