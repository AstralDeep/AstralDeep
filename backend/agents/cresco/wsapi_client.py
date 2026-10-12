"""Bounded read-only WebSocket RPC client for the Cresco fabric wsapi seam.

Dials the operator-configured gateway with verified TLS and the pinned JSON envelope,
correlates replies under strict time and size bounds, and maps every failure to a typed
error that never carries the service key. Tool handlers in mcp_tools.py translate those
errors into user-legible results; orchestrator/local_agents.py registers the owning agent
only when the cresco feature flag is on.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import socket
import ssl
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

MAX_RPC = 1 << 20
MAX_EXPANDED = 4 << 20
MAX_FRAME = MAX_RPC + 65536
CONNECT_TIMEOUT = 10
RPC_TIMEOUT = 20
OPEN_ATTEMPTS = 2
OPEN_RETRY_SLEEP = 0.25
WSAPI_PATH = "/api/apisocket"
DEFAULT_PORT = 8282
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}")
FINGERPRINT = re.compile(r"[0-9a-f]{64}")


class CrescoError(Exception):
    retryable = False


class CrescoUnavailable(CrescoError):
    pass


class CrescoInvalidArgument(CrescoError):
    pass


class CrescoUnreachable(CrescoError):
    retryable = True


class CrescoAuthError(CrescoError):
    pass


class CrescoTLSError(CrescoError):
    pass


class CrescoProtocolError(CrescoError):
    pass


@dataclass(frozen=True)
class FabricConfig:
    url: str = ""
    service_key: str = ""
    ca_bundle: str = ""
    fingerprint: str = ""
    allow_private_host: bool = False

    @classmethod
    def from_environment(cls) -> "FabricConfig":
        fingerprint = os.getenv("CRESCO_TLS_FINGERPRINT", "").strip().replace(":", "").lower()
        return cls(
            url=os.getenv("CRESCO_WSAPI_URL", "").strip(),
            service_key=os.getenv("CRESCO_SERVICE_KEY", "").strip(),
            ca_bundle=os.getenv("CRESCO_CA_BUNDLE", "").strip(),
            fingerprint=fingerprint,
            allow_private_host=os.getenv("CRESCO_ALLOW_PRIVATE_HOST", "").strip().lower()
            in ("1", "true", "yes"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url and self.service_key)

    def endpoint(self) -> Tuple[str, int]:
        parsed = urlparse(self.url)
        host = parsed.hostname or ""
        if parsed.scheme.lower() != "wss" or not IDENTIFIER.fullmatch(host):
            raise CrescoUnavailable("CRESCO_WSAPI_URL must be a wss:// URL with a plain hostname.")
        try:
            port = parsed.port
        except ValueError as exc:
            raise CrescoUnavailable("CRESCO_WSAPI_URL has an invalid port.") from exc
        port = DEFAULT_PORT if port is None else port
        if not 1 <= port <= 65535:
            raise CrescoUnavailable("CRESCO_WSAPI_URL port is out of range.")
        return host, port

    def tls_context(self) -> ssl.SSLContext:
        if self.fingerprint and not FINGERPRINT.fullmatch(self.fingerprint):
            raise CrescoUnavailable("CRESCO_TLS_FINGERPRINT must be a hex SHA-256 digest.")
        if self.ca_bundle:
            context = ssl.create_default_context(cafile=self.ca_bundle)
        elif self.fingerprint:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            # Identity is the digest match itself, proven before any credential is sent.
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context


def encode_compressed(value: Any) -> str:
    try:
        payload = json.dumps(value, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise CrescoInvalidArgument("Cresco bulk parameter is not JSON-encodable.") from exc
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as writer:
        writer.write(payload)
    return base64.b64encode(buffer.getvalue()).decode()


def decode_compressed(value: str) -> Any:
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
        raise CrescoProtocolError("Cresco reply carries invalid compressed content.") from exc


def _typed_failure(exc: BaseException) -> CrescoError:
    if isinstance(exc, ssl.SSLError):
        return CrescoTLSError("Cresco TLS verification failed.")
    if isinstance(exc, InvalidStatus):
        status = exc.response.status_code
        if status in (401, 403):
            return CrescoAuthError(f"Cresco gateway rejected the service credential (status {status}).")
        return CrescoUnreachable(f"Cresco gateway refused the connection (status {status}).")
    if isinstance(exc, ConnectionClosed):
        return CrescoUnreachable("Cresco gateway closed the connection mid-call.")
    if isinstance(exc, InvalidHandshake):
        return CrescoUnreachable("Cresco gateway handshake failed.")
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return CrescoUnreachable("Cresco gateway did not respond in time.")
    if isinstance(exc, OSError):
        return CrescoUnreachable("Cresco gateway connection failed.")
    return CrescoUnreachable("Cresco gateway exchange failed.")


class WsapiClient:
    def __init__(self, config: FabricConfig):
        self.config = config

    def call(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        region: str = "",
        agent: str = "",
        timeout: float = RPC_TIMEOUT,
    ) -> Dict[str, Any]:
        if not IDENTIFIER.fullmatch(str(action)):
            raise CrescoInvalidArgument("Cresco action names must be bounded identifiers.")
        if not self.config.configured:
            raise CrescoUnavailable("Cresco fabric not configured.")
        host, port = self.config.endpoint()
        seconds = max(1.0, min(float(timeout), RPC_TIMEOUT))
        payload: Dict[str, Any] = dict(params or {}, action=action, client_rpc_id=uuid.uuid4().hex)
        info: Dict[str, Any] = {
            "message_type": "global_plugin_msgevent",
            "message_event_type": "EXEC",
            "is_rpc": "true",
            "rpc_timeout_ms": str(max(1000, int(seconds * 1000) - 1000)),
        }
        if region:
            info["dst_region"] = region
        if agent:
            info["dst_agent"] = agent
        envelope = {"message_info": info, "message_payload": payload}
        try:
            wire = json.dumps(envelope, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise CrescoInvalidArgument("Cresco request parameters are not JSON-encodable.") from exc
        if len(wire.encode()) > MAX_RPC:
            raise CrescoInvalidArgument("Cresco request exceeds the RPC bound.")
        self._validate_egress(host, port)
        return self._exchange(host, port, wire, payload["client_rpc_id"], seconds)

    def _validate_egress(self, host: str, port: int) -> None:
        from shared.external_http import EgressBlockedError, validate_egress_url

        allowed = [host] if self.config.allow_private_host else None
        try:
            validate_egress_url(f"https://{host}:{port}", allowed_private_hosts=allowed)
        except EgressBlockedError as exc:
            failure = CrescoUnreachable("Cresco gateway host is blocked by egress policy.")
            failure.retryable = False
            raise failure from exc

    def _exchange(self, host: str, port: int, wire: str, rpc_id: str, seconds: float) -> Dict[str, Any]:
        failure: Optional[CrescoError] = None
        for _ in range(OPEN_ATTEMPTS):
            if failure is not None:
                time.sleep(OPEN_RETRY_SLEEP)
            try:
                connection = self._open(host, port)
            except CrescoUnreachable as exc:
                failure = exc
                continue
            with connection:
                try:
                    connection.send(wire)
                    raw = connection.recv(timeout=seconds)
                except Exception as exc:
                    raise _typed_failure(exc) from exc
                return self._reply(raw, rpc_id)
        raise failure

    def _open(self, host: str, port: int):
        from websockets.sync.client import connect

        raw = self._dial(host, port)
        try:
            if self.config.fingerprint:
                upgraded = self._pin_wrap(raw, host)
                return connect(
                    f"ws://{host}:{port}{WSAPI_PATH}",
                    sock=upgraded,
                    compression=None,
                    max_size=MAX_FRAME,
                    max_queue=8,
                    open_timeout=CONNECT_TIMEOUT,
                    close_timeout=1,
                    proxy=None,
                    additional_headers={"cresco_service_key": self.config.service_key},
                )
            return connect(
                f"wss://{host}:{port}{WSAPI_PATH}",
                sock=raw,
                ssl=self.config.tls_context(),
                server_hostname=host,
                compression=None,
                max_size=MAX_FRAME,
                max_queue=8,
                open_timeout=CONNECT_TIMEOUT,
                close_timeout=1,
                proxy=None,
                additional_headers={"cresco_service_key": self.config.service_key},
            )
        except CrescoError:
            raise
        except Exception as exc:
            raise _typed_failure(exc) from exc

    @staticmethod
    def _dial(host: str, port: int) -> socket.socket:
        try:
            return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except Exception as exc:
            raise _typed_failure(exc) from exc

    def _pin_wrap(self, raw: socket.socket, host: str) -> socket.socket:
        try:
            upgraded = self.config.tls_context().wrap_socket(raw, server_hostname=host)
        except Exception as exc:
            raise _typed_failure(exc) from exc
        presented = upgraded.getpeercert(binary_form=True) or b""
        if hashlib.sha256(presented).hexdigest() != self.config.fingerprint:
            upgraded.close()
            raise CrescoTLSError("Cresco gateway certificate does not match the pinned fingerprint.")
        return upgraded

    @staticmethod
    def _reply(raw: Any, rpc_id: str) -> Dict[str, Any]:
        try:
            reply = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise CrescoProtocolError("Cresco reply was not valid JSON.") from exc
        if not isinstance(reply, dict):
            raise CrescoProtocolError("Cresco reply was not an envelope object.")
        if reply.get("client_rpc_id") != rpc_id:
            raise CrescoProtocolError("Cresco reply correlation failed.")
        reply.pop("client_rpc_id")
        _expand_reply(reply)
        if reply.get("status") not in ("10", 10):
            raise CrescoProtocolError(f"Cresco fabric reported status {reply.get('status')!r}.")
        return reply


def _looks_compressed(value: Any) -> bool:
    if not isinstance(value, str) or len(value) < 8 or len(value) > MAX_RPC:
        return False
    try:
        return base64.b64decode(value, validate=True).startswith(b"\x1f\x8b")
    except (ValueError, TypeError):
        return False


def _expand_reply(reply: Dict[str, Any]) -> None:
    expanded = 0
    for key, value in list(reply.items()):
        if not _looks_compressed(value):
            continue
        decoded = decode_compressed(value)
        expanded += len(json.dumps(decoded, allow_nan=False).encode())
        if expanded > MAX_EXPANDED:
            raise CrescoProtocolError("Cresco reply exceeds the expansion bound.")
        reply[key] = decoded
