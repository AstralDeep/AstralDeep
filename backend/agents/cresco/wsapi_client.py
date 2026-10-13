"""Sync wsapi WebSocket client for the Cresco bridge: pinned envelope/bulk-param profile,
egress-validated dialing with verified TLS, bounded timeouts, the closed fault set, and
bounded reconnect. mcp_tools.py drives it per tool call; frames that drift from the
pinned profile fail safe into the protocol fault state.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import ssl
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from shared.external_http import _allowed_private_hosts_from_env, validate_egress_url
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, ProtocolError
from websockets.sync.client import connect

FAULT_STATES = ("unavailable", "unreachable", "auth", "protocol", "tls")
_AUTH_MARKERS = ("auth", "credential", "unauthorized", "forbidden", "service_key", "service key")
_STALE_MARKERS = ("unknown_agent", "unknown agent", "revoked", "not_found", "no such agent")
_MAX_FRAME_BYTES = 2 * 1024 * 1024
_MAX_WORK_CHARS = 4000


class FabricFault(Exception):
    def __init__(self, state: str, *, retryable: bool, detail: str = "", after_send: bool = False):
        if state not in FAULT_STATES:
            state = "protocol"
        self.state = state
        self.retryable = retryable
        self.detail = detail
        self.after_send = after_send
        super().__init__(state)


class UnconfirmedResult(Exception):
    def __init__(self, work_id: str, reason: str):
        self.work_id = work_id
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class FabricConfig:
    wsapi_url: str
    service_key: str
    connect_timeout: float = 10.0
    rpc_timeout: float = 30.0
    ca_bundle: str = ""
    tls_fingerprint: str = ""
    allow_private_host: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "FabricConfig":
        source = os.environ if env is None else env
        return cls(
            wsapi_url=str(source.get("CRESCO_WSAPI_URL", "") or "").strip(),
            service_key=str(source.get("CRESCO_SERVICE_KEY", "") or ""),
            connect_timeout=_bounded_float(source.get("CRESCO_CONNECT_TIMEOUT"), 10.0, 1.0, 60.0),
            rpc_timeout=_bounded_float(source.get("CRESCO_RPC_TIMEOUT"), 30.0, 1.0, 300.0),
            ca_bundle=str(source.get("CRESCO_CA_BUNDLE", "") or "").strip(),
            tls_fingerprint=_normalized_fingerprint(source.get("CRESCO_TLS_FINGERPRINT", "")),
            allow_private_host=str(source.get("CRESCO_ALLOW_PRIVATE_HOST", "") or "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.wsapi_url and self.service_key)

    @property
    def host(self) -> str:
        return urlsplit(self.wsapi_url).hostname or ""

    @property
    def https_form(self) -> str:
        parts = urlsplit(self.wsapi_url if "://" in self.wsapi_url else f"wss://{self.wsapi_url}")
        port = parts.port or (443 if parts.scheme in ("wss", "https") else 80)
        return f"https://{parts.hostname}:{port}"


def _bounded_float(raw: object, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _normalized_fingerprint(raw: object) -> str:
    return str(raw or "").strip().replace(":", "").lower()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def encode_bulk(value: object) -> str:
    return base64.b64encode(gzip.compress(canonical_json(value), compresslevel=9, mtime=0)).decode()


def decode_bulk(raw: object) -> object:
    if not isinstance(raw, str):
        raise ValueError("bulk parameter must be a base64 string")
    return json.loads(gzip.decompress(base64.b64decode(raw)))


def build_request(action: str, params: dict, message_id: str | None = None) -> dict:
    return {
        "message_info": {
            "message_type": "REQUEST",
            "message_event_type": "EVENT",
            "is_rpc": True,
            "message_id": message_id or uuid.uuid4().hex,
        },
        "message_payload": {"action": action, **params},
    }


def parse_frame(raw: object) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        raise FabricFault("protocol", retryable=False, detail="non_text_frame")
    try:
        frame = json.loads(raw)
    except (ValueError, TypeError):
        raise FabricFault("protocol", retryable=False, detail="undecodable_frame") from None
    if not isinstance(frame, dict) or not isinstance(frame.get("message_payload"), dict):
        raise FabricFault("protocol", retryable=False, detail="unexpected_frame_shape")
    return frame


def response_matches(frame: dict, message_id: str) -> bool:
    info = frame.get("message_info")
    if isinstance(info, dict) and info.get("message_id") == message_id:
        return True
    payload = frame["message_payload"]
    return payload.get("message_id") == message_id or payload.get("work_id") == message_id


def classify_payload_error(error: object) -> FabricFault:
    text = json.dumps(error, default=str).lower() if not isinstance(error, str) else error.lower()
    if any(marker in text for marker in _STALE_MARKERS):
        return FabricFault("protocol", retryable=False, detail="stale_target")
    if any(marker in text for marker in _AUTH_MARKERS):
        return FabricFault("auth", retryable=False, detail="fabric_rejected_credential")
    return FabricFault("protocol", retryable=False, detail="fabric_reported_failure")


def build_ssl_context(config: FabricConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=config.ca_bundle or None)
    if config.ca_bundle:
        context.load_verify_locations(cafile=config.ca_bundle)
    if config.tls_fingerprint:
        if len(config.tls_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in config.tls_fingerprint):
            raise FabricFault("tls", retryable=False, detail="malformed_fingerprint")
        # Pinned-fingerprint mode enforces identity by the post-handshake SHA-256 pin below.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def pin_matches(config: FabricConfig, der_certificate: object) -> bool:
    if not config.tls_fingerprint:
        return True
    if not isinstance(der_certificate, bytes):
        return False
    return hashlib.sha256(der_certificate).hexdigest() == config.tls_fingerprint


def backoff_delays(attempts: int, base: float, cap: float) -> list[float]:
    delays = []
    for index in range(max(0, attempts - 1)):
        delays.append(min(cap, base * (2 ** index)))
    return delays


def dial_attempts(env: dict[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    try:
        value = int(source.get("CRESCO_DIAL_ATTEMPTS", ""))
    except ValueError:
        value = 2
    return max(1, min(3, value))


def dial_backoff_seconds(env: dict[str, str] | None = None) -> float:
    source = os.environ if env is None else env
    try:
        value = float(source.get("CRESCO_BACKOFF_SECONDS", ""))
    except ValueError:
        value = 0.25
    return max(0.0, min(2.0, value))


class WsapiClient:
    def __init__(self, config: FabricConfig):
        self.config = config

    def validate_egress(self) -> None:
        if urlsplit(self.config.wsapi_url if "://" in self.config.wsapi_url else f"wss://{self.config.wsapi_url}").scheme.lower() != "wss":
            raise FabricFault("tls", retryable=False, detail="insecure_scheme")
        allowed = set(_allowed_private_hosts_from_env())
        if self.config.allow_private_host:
            if self.config.allow_private_host != self.config.host:
                raise FabricFault("tls", retryable=False, detail="private_host_opt_in_mismatch")
            allowed.add(self.config.host)
        try:
            validate_egress_url(self.config.https_form, allowed_private_hosts=allowed)
        except Exception as exc:
            raise FabricFault("tls", retryable=False, detail="egress_blocked") from exc

    def _connect(self):
        self.validate_egress()
        context = build_ssl_context(self.config)
        try:
            connection = connect(
                self.config.wsapi_url,
                ssl=context,
                additional_headers={"cresco_service_key": self.config.service_key},
                open_timeout=self.config.connect_timeout,
                max_size=_MAX_FRAME_BYTES,
            )
        except InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                raise FabricFault("auth", retryable=False, detail="fabric_rejected_credential") from exc
            raise FabricFault("unreachable", retryable=True, detail="fabric_refused_connection") from exc
        except ssl.SSLError as exc:
            raise FabricFault("tls", retryable=False, detail="certificate_verification_failed") from exc
        except (InvalidHandshake, TimeoutError, OSError) as exc:
            raise FabricFault("unreachable", retryable=True, detail="fabric_unreachable") from exc
        if not pin_matches(self.config, connection.socket.getpeercert(True)):
            connection.close()
            raise FabricFault("tls", retryable=False, detail="fingerprint_mismatch")
        return connection

    def dial(self, *, attempts: int = 1, base_delay: float = 0.5, cap_delay: float = 4.0):
        last: FabricFault | None = None
        for index, delay in enumerate([0.0] + backoff_delays(attempts, base_delay, cap_delay)):
            if index:
                time.sleep(delay)
            try:
                return self._connect()
            except FabricFault as exc:
                last = exc
                if not exc.retryable:
                    raise
        raise last if last is not None else FabricFault("unreachable", retryable=True)

    def call(self, action: str, params: dict, *, timeout: float | None = None,
             attempts: int | None = None, base_delay: float | None = None,
             cap_delay: float = 4.0) -> dict:
        request = build_request(action, params)
        message_id = request["message_info"]["message_id"]
        budget = timeout if timeout is not None else self.config.rpc_timeout
        with self.dial(attempts=dial_attempts() if attempts is None else attempts,
                       base_delay=dial_backoff_seconds() if base_delay is None else base_delay,
                       cap_delay=cap_delay) as connection:
            try:
                connection.send(json.dumps(request))
                deadline = time.monotonic() + budget
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise FabricFault("unreachable", retryable=True, detail="rpc_window_elapsed",
                                          after_send=True)
                    frame = parse_frame(self._recv(connection, remaining))
                    if not response_matches(frame, message_id):
                        continue
                    payload = dict(frame["message_payload"])
                    payload.pop("action", None)
                    if "error" in payload and payload["error"] is not None:
                        raise classify_payload_error(payload["error"])
                    bulk = payload.pop("result_bulk", None)
                    if bulk is not None:
                        try:
                            decoded = decode_bulk(bulk)
                        except (ValueError, OSError) as exc:
                            raise FabricFault("protocol", retryable=False,
                                              detail="undecodable_result_bulk") from exc
                        if isinstance(decoded, dict):
                            payload.update(decoded)
                    return payload
            except FabricFault as exc:
                if exc.detail not in ("stale_target", "fabric_rejected_credential"):
                    exc.after_send = True
                raise
            except ConnectionClosed as exc:
                raise FabricFault("unreachable", retryable=True, detail="fabric_closed_connection",
                                  after_send=True) from exc
            except ProtocolError as exc:
                raise FabricFault("protocol", retryable=False, detail="wire_protocol_error",
                                  after_send=True) from exc

    @staticmethod
    def _recv(connection, remaining: float) -> object:
        try:
            return connection.recv(timeout=remaining)
        except TimeoutError:
            raise FabricFault("unreachable", retryable=True, detail="rpc_window_elapsed",
                              after_send=True) from None

    def submit_work(self, target: str, work_id: str, work: dict, *, timeout: float | None = None,
                    attempts: int | None = None, base_delay: float | None = None,
                    cap_delay: float = 4.0) -> dict:
        request = build_request("submitwork", {
            "region_agent": target,
            "work_id": work_id,
            "work_bulk": encode_bulk(work),
        })
        message_id = request["message_info"]["message_id"]
        budget = timeout if timeout is not None else self.config.rpc_timeout
        with self.dial(attempts=dial_attempts() if attempts is None else attempts,
                       base_delay=dial_backoff_seconds() if base_delay is None else base_delay,
                       cap_delay=cap_delay) as connection:
            try:
                connection.send(json.dumps(request))
                deadline = time.monotonic() + budget
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise UnconfirmedResult(work_id, "result_window_elapsed")
                    try:
                        raw = connection.recv(timeout=remaining)
                    except TimeoutError:
                        raise UnconfirmedResult(work_id, "result_window_elapsed") from None
                    frame = parse_frame(raw)
                    if not response_matches(frame, message_id):
                        continue
                    payload = dict(frame["message_payload"])
                    payload.pop("action", None)
                    if "error" in payload and payload["error"] is not None:
                        raise classify_payload_error(payload["error"])
                    bulk = payload.pop("result_bulk", None)
                    if bulk is not None:
                        try:
                            decoded = decode_bulk(bulk)
                        except (ValueError, OSError) as exc:
                            raise UnconfirmedResult(work_id, "undecodable_result_bulk") from exc
                        if isinstance(decoded, dict):
                            payload.update(decoded)
                    result = payload.get("result")
                    return {"work_id": str(payload.get("work_id") or work_id),
                            "result": result if isinstance(result, dict) else payload}
            except FabricFault as exc:
                if exc.detail not in ("stale_target", "fabric_rejected_credential"):
                    exc.after_send = True
                raise
            except ConnectionClosed as exc:
                raise FabricFault("unreachable", retryable=True, detail="fabric_closed_connection",
                                  after_send=True) from exc
            except ProtocolError as exc:
                raise FabricFault("protocol", retryable=False, detail="wire_protocol_error",
                                  after_send=True) from exc
