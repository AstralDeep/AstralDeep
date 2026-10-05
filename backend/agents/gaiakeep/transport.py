"""Provides bounded, correlated native RPC and verified TLS data streams through caller-owned SSH tunnels or the operator-configured loopback gateway."""

from __future__ import annotations

import base64
import errno
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


class ConnectionOpenError(OSError):
    pass


FAILURE_PHASES = frozenset({'rpc_open', 'tcp_connect', 'websocket_open', 'rpc_send', 'rpc_receive',
                          'rpc_decode', 'rpc_response', 'stream_open', 'stream_activation', 'stream_send',
                          'stream_receive', 'cleanup', 'sdk_validation', 'account_validation',
                          'sdk_read', 'sdk_upload', 'sdk_control', 'result_validation'})
FAILURE_KINDS = frozenset({'timeout', 'connection', 'tls', 'authentication', 'authorization',
                         'integrity', 'protocol', 'invalid_argument', 'native', 'other'})


def validate_failure_detail(value):
    try:
        if (type(value) is not dict or len(value) != 3
                or set(value) != {'native_phase', 'failure_kind', 'native_status'}
                or type(value['native_phase']) is not str or value['native_phase'] not in FAILURE_PHASES
                or type(value['failure_kind']) is not str or value['failure_kind'] not in FAILURE_KINDS
                or (value['native_status'] is not None
                    and (type(value['native_status']) is not int or not 0 <= value['native_status'] <= 999))):
            return None
        return dict(value)
    except Exception:
        return None


def _native_status(value):
    if type(value) is int:
        return value if 0 <= value <= 999 else None
    if type(value) is str and re.fullmatch(r'0|[1-9][0-9]{0,2}', value, flags=re.ASCII):
        return int(value)
    return None


def _failure_kind(exc, status):
    name = type(exc).__name__
    if isinstance(exc, ssl.SSLError):
        return 'tls'
    if isinstance(exc, TimeoutError) or name in {'RpcTimeout', 'OutcomeUnknown', 'TransferTimeout', 'JobTimeout'}:
        return 'timeout'
    if name in {'Unauthenticated', 'AuthenticationException'}:
        return 'authentication'
    if isinstance(exc, PermissionError) or name in {'Forbidden', 'PolicyRefused'}:
        return 'authorization'
    if name in {'IntegrityError', 'InvalidSignature'}:
        return 'integrity'
    if isinstance(exc, ProtocolError) or name == 'BadRequest':
        return 'protocol'
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return 'invalid_argument'
    if (name in {'TransportError', 'ConnectionClosed', 'ConnectionClosedError', 'ConnectionClosedOK'}
            or isinstance(exc, (ConnectionError, BrokenPipeError))
            or isinstance(exc, OSError) and exc.errno in {errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET,
                                                       errno.EPIPE, errno.ENETRESET, errno.ENETDOWN,
                                                       errno.ENETUNREACH, errno.EHOSTUNREACH}):
        return 'connection'
    return 'native' if status is not None else 'other'


def failure_detail(exc, fallback_phase=None):
    try:
        current, seen = exc, set()
        for _ in range(8):
            if not isinstance(current, BaseException) or id(current) in seen:
                break
            seen.add(id(current))
            detail = validate_failure_detail(getattr(current, '_gaiakeep_failure_detail', None))
            if detail is not None:
                return detail
            current = current.__cause__ or current.__context__
        if type(fallback_phase) is not str or fallback_phase not in FAILURE_PHASES:
            return None
        status = _native_status(getattr(exc, 'status', None))
        return {'native_phase': fallback_phase, 'failure_kind': _failure_kind(exc, status), 'native_status': status}
    except Exception:
        return None


def mark_failure(exc, phase, *, native_status=None):
    try:
        detail = failure_detail(exc, phase)
        if detail is not None:
            if native_status is not None and failure_detail(exc) is None:
                detail['native_status'] = _native_status(native_status)
            exc._gaiakeep_failure_detail = detail
    except Exception:
        pass
    return exc


def _raise_opening_error(exc):
    from websockets.exceptions import ConnectionClosed

    codes = [close.code for close in (exc.rcvd, exc.sent) if close is not None] if isinstance(exc, ConnectionClosed) else []
    transient_close = bool(codes) and all(code in {1001, 1006, 1011, 1012, 1013, 1014} for code in codes)
    if (not isinstance(exc, (ssl.SSLError, PermissionError))
            and (isinstance(exc, (TimeoutError, ConnectionError)) or transient_close
                 or isinstance(exc, OSError) and exc.errno in {
                     errno.ETIMEDOUT, errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET,
                     errno.EPIPE, errno.ENETRESET, errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH})):
        error = ConnectionOpenError('The Gaia connection could not be opened.')
        detail = failure_detail(exc)
        if detail is not None:
            error._gaiakeep_failure_detail = detail
        raise error from exc
    raise exc


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
        self.publication_metadata = {}
        self.deadline = time.monotonic() + FLOW_TIMEOUT

    def remaining(self, cap):
        value = min(cap, self.deadline - time.monotonic())
        if value <= 0:
            raise TimeoutError('GaiaKeep operation deadline reached.')
        return value

    @contextmanager
    def _opening_socket(self, path):
        failure = None
        for _ in range(2):
            if failure is not None and time.monotonic() >= self.deadline:
                raise failure
            self.remaining(CONNECT_TIMEOUT)
            with ExitStack() as stack:
                try:
                    ws = stack.enter_context(self._socket(path))
                except ConnectionOpenError as exc:
                    failure = exc
                else:
                    yield ws
                    return
        raise failure

    @contextmanager
    def _rpc_socket(self):
        with self._opening_socket('/api/apisocket') as ws:
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
        opening_error = None
        with ParamikoTransport().open_tunnel(self.target, self.config.port, timeout=CONNECT_TIMEOUT) as channel:
            bridge = SocketBridge(channel, self.deadline)
            ws = None
            try:
                try:
                    ws = connect(f'wss://{self.config.tls_name}:{self.config.port}{path}',
                                 sock=bridge.socket, ssl=self.context,
                                 server_hostname=self.config.tls_name, proxy=None,
                                 additional_headers={'cresco_service_key': self.config.service_key},
                                 compression=None, max_size=MAX_FRAME, max_queue=8,
                                 open_timeout=self.remaining(CONNECT_TIMEOUT), close_timeout=1)
                except Exception as exc:
                    opening_error = mark_failure(exc, 'websocket_open')
                if opening_error is None:
                    yield ws
            finally:
                try:
                    if ws is not None:
                        try:
                            ws.close()
                        except Exception as exc:
                            mark_failure(exc, 'cleanup')
                            raise
                finally:
                    try:
                        bridge.close()
                    except Exception as exc:
                        mark_failure(exc, 'cleanup')
                        raise
        if opening_error is not None:
            _raise_opening_error(opening_error)

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
        phase = 'rpc_open'
        try:
            with self._rpc_socket() as ws:
                phase = 'rpc_send'
                ws.send(wire)
                phase = 'rpc_receive'
                raw = ws.recv(timeout=self.remaining(seconds))
                phase = 'rpc_decode'
                reply = _json(raw)
        except Exception as exc:
            mark_failure(exc, phase)
            raise
        if reply.get('client_rpc_id') != rpc_id:
            raise mark_failure(ProtocolError('Native reply correlation failed.'), 'rpc_response',
                               native_status=reply.get('status'))
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
        from agents.gaiakeep.client import observe_native_reply

        observe_native_reply(self, action, params, reply)
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
        seconds = self.remaining(CONNECT_TIMEOUT)
        connection = ws = opening_error = None
        try:
            try:
                connection = socket.create_connection(('127.0.0.1', self.port),
                                                      timeout=seconds)
            except Exception as exc:
                opening_error = mark_failure(exc, 'tcp_connect')
            if opening_error is None:
                try:
                    ws = connect(f'wss://{self.config.tls_name}:{self.port}{path}',
                                 sock=connection, ssl=self.context,
                                 server_hostname=self.config.tls_name, proxy=None,
                                 additional_headers={'cresco_service_key': self.config.service_key},
                                 compression=None, max_size=MAX_FRAME, max_queue=8,
                                 open_timeout=self.remaining(CONNECT_TIMEOUT), close_timeout=1)
                except Exception as exc:
                    opening_error = mark_failure(exc, 'websocket_open')
            if opening_error is None:
                yield ws
        finally:
            try:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception as exc:
                        mark_failure(exc, 'cleanup')
                        raise
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception as exc:
                        mark_failure(exc, 'cleanup')
                        raise
        if opening_error is not None:
            _raise_opening_error(opening_error)


class NativeStream:
    def __init__(self, transport, name, handler):
        self.name, self.transport, self.handler = name, transport, handler or (lambda frame: None)
        self.manager = transport._opening_socket('/api/dataplane')
        try:
            self.ws = self.manager.__enter__()
        except Exception as exc:
            mark_failure(exc, 'stream_open')
            raise
        self.error = None
        self.closed = False
        self.worker = None
        phase = 'stream_send'
        try:
            self.ws.send(json.dumps({'ident_key': 'stream_name', 'ident_id': name, 'io_type_key': 'type',
                                    'output_id': 'output', 'input_id': 'input'}))
            phase = 'stream_activation'
            activation = _json(self.ws.recv(timeout=transport.remaining(CONNECT_TIMEOUT)))
            if activation.get('status_code') != '10':
                raise mark_failure(ProtocolError('Native stream activation was refused.'), phase,
                                   native_status=activation.get('status_code'))
            self.worker = threading.Thread(target=self._receive, daemon=True)
            self.worker.start()
        except Exception as exc:
            mark_failure(exc, phase)
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
            mark_failure(exc, 'stream_receive')
            self.error = exc
            self.ws.close()

    def send(self, frame):
        try:
            self.transport.remaining(FLOW_TIMEOUT)
        except Exception as exc:
            mark_failure(exc, 'stream_send')
            raise
        if self.closed or self.error is not None:
            error = ProtocolError('Native stream failed.')
            detail = failure_detail(self.error)
            if detail is not None:
                error._gaiakeep_failure_detail = detail
            raise mark_failure(error, 'stream_send')
        if not isinstance(frame, bytes) or len(frame) > MAX_FRAME:
            raise ProtocolError('Invalid native data frame.')
        try:
            self.ws.send(frame)
        except Exception as exc:
            mark_failure(exc, 'stream_send')
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            try:
                self.ws.close()
            except Exception as exc:
                mark_failure(exc, 'cleanup')
                raise
        finally:
            try:
                if self.worker is not None and self.worker is not threading.current_thread():
                    self.worker.join(timeout=1)
            finally:
                try:
                    self.manager.__exit__(None, None, None)
                except Exception as exc:
                    mark_failure(exc, 'cleanup')
                    raise
