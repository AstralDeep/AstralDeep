"""Authenticated HTTP-upgrade boundary for the Feature 065 voice worker.

The endpoint owns only the short-lived upgrade challenge and WebSocket
transport lifecycle.  Once authenticated, every registration and session
frame is delegated to :class:`WorkerPool`, which remains the sole frame,
direction, sequence, rate, capacity, and assignment authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, WebSocket
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect

from .voice_coordinator import (
    MAX_CONTROL_FRAME_BYTES,
    AdmissionRefusal,
    ControlProtocolError,
    RegistrationError,
    StaleFence,
    WorkerConnectionRelease,
    WorkerPool,
    WorkerRegistrationReceipt,
    WorkerStatusEntry,
    SessionReservation,
)


WORKER_CONTROL_PATH = "/api/voice/worker-control"
CHALLENGE_NONCE_HEADER = "X-Astral-Voice-Challenge"
CHALLENGE_ISSUED_HEADER = "X-Astral-Voice-Challenge-Issued-At"
CHALLENGE_EXPIRES_HEADER = "X-Astral-Voice-Challenge-Expires-At"
WORKER_HEADER = "X-Astral-Voice-Worker"
NONCE_HEADER = "X-Astral-Voice-Nonce"
TIMESTAMP_HEADER = "X-Astral-Voice-Timestamp"
SIGNATURE_HEADER = "X-Astral-Voice-Signature"

_CHALLENGE_DOMAIN = b"astraldeep.voice.worker-control.challenge.v1"
_AUTH_HEADERS = (WORKER_HEADER, NONCE_HEADER, TIMESTAMP_HEADER, SIGNATURE_HEADER)
_NONCE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_CLOCK_SKEW_SECONDS = 5
_APP_STATE_KEY = "_astraldeep_voice_worker_control_endpoint_065"
logger = logging.getLogger(__name__)


class WorkerControlEndpointError(RuntimeError):
    """A content-free endpoint failure safe for process diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code if _ERROR_CODE.fullmatch(code) else "worker_control_error"
        super().__init__(self.code)


class WorkerControlConfigError(WorkerControlEndpointError):
    """The orchestrator worker-control boundary is not safely configured."""


class WorkerControlAuthError(WorkerControlEndpointError):
    """An upgrade challenge was missing, stale, replayed, or invalid."""


@dataclass(frozen=True, slots=True)
class WorkerControlSettings:
    """Validated server-only challenge and transport bounds."""

    secret: bytes = field(repr=False)
    challenge_ttl_seconds: int = 15
    challenge_capacity: int = 256
    registration_timeout_seconds: float = 5.0
    lease_sweep_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.secret, bytes) or not 32 <= len(self.secret) <= 512:
            raise WorkerControlConfigError("invalid_control_secret")
        if not 1 <= self.challenge_ttl_seconds <= 30:
            raise WorkerControlConfigError("invalid_challenge_ttl")
        if not 1 <= self.challenge_capacity <= 4_096:
            raise WorkerControlConfigError("invalid_challenge_capacity")
        for name, lower, upper in (
            ("registration_timeout_seconds", 0.1, 10.0),
            ("lease_sweep_seconds", 0.1, 60.0),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not lower <= float(value) <= upper
            ):
                raise WorkerControlConfigError("invalid_endpoint_timeout")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> WorkerControlSettings:
        values = os.environ if environ is None else environ
        raw = values.get("VOICE_CONTROL_SECRET", "")
        if (
            not isinstance(raw, str)
            or not raw
            or raw != raw.strip()
            or "\x00" in raw
        ):
            raise WorkerControlConfigError("missing_control_secret")
        return cls(secret=raw.encode("utf-8"))


@dataclass(frozen=True, slots=True, repr=False)
class UpgradeChallenge:
    """One memory-only, single-use HTTP-upgrade challenge."""

    nonce: str
    issued_at: int
    expires_at: int

    def __repr__(self) -> str:
        return "UpgradeChallenge(<redacted>)"


class WorkerChallengeStore:
    """Bounded single-use challenge registry with eager expiry pruning."""

    def __init__(
        self,
        settings: WorkerControlSettings,
        *,
        epoch_seconds: Callable[[], int] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._epoch_seconds = epoch_seconds or (lambda: int(time.time()))
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(32))
        self._active: dict[str, UpgradeChallenge] = {}

    @property
    def retained_count(self) -> int:
        return len(self._active)

    def issue(self) -> UpgradeChallenge:
        now = self._now()
        self._prune(now)
        if len(self._active) >= self._settings.challenge_capacity:
            raise WorkerControlAuthError("challenge_capacity_exhausted")
        for _attempt in range(4):
            nonce = self._nonce_factory()
            if (
                isinstance(nonce, str)
                and _NONCE.fullmatch(nonce) is not None
                and nonce not in self._active
            ):
                challenge = UpgradeChallenge(
                    nonce=nonce,
                    issued_at=now,
                    expires_at=now + self._settings.challenge_ttl_seconds,
                )
                self._active[nonce] = challenge
                return challenge
        raise WorkerControlAuthError("challenge_generation_failed")

    def consume(self, nonce: str) -> UpgradeChallenge:
        now = self._now()
        if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
            raise WorkerControlAuthError("invalid_challenge")
        challenge = self._active.pop(nonce, None)
        self._prune(now)
        if challenge is None:
            raise WorkerControlAuthError("invalid_challenge")
        if now < challenge.issued_at - _CLOCK_SKEW_SECONDS:
            raise WorkerControlAuthError("invalid_challenge")
        if now > challenge.expires_at:
            raise WorkerControlAuthError("expired_challenge")
        return challenge

    def _prune(self, now: int) -> None:
        for nonce, challenge in tuple(self._active.items()):
            if challenge.expires_at < now:
                del self._active[nonce]

    def _now(self) -> int:
        value = self._epoch_seconds()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WorkerControlAuthError("invalid_challenge_clock")
        return value


class AdmissionRefusalLog:
    """Bounded, memory-only retention of refused worker admission attempts.

    Recorded only at the three genuine refusal exits (authentication failure,
    registration timeout, registration refusal) — never on the healthy
    challenge-issue leg, and never for a worker that was already admitted.

    Retention is PER STAGE: the pre-accept authentication path is reachable
    by any unauthenticated client, so its churn must never evict a genuine
    registration-stage refusal (which requires a validly signed challenge)
    from the operator's FR-034 view.
    """

    def __init__(
        self,
        *,
        capacity: int = 8,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= capacity <= 64:
            raise WorkerControlConfigError("invalid_refusal_retention")
        self._entries: dict[str, deque[AdmissionRefusal]] = {
            "authentication": deque(maxlen=capacity),
            "registration": deque(maxlen=capacity),
        }
        self._utcnow = utcnow or (lambda: datetime.now(UTC))

    def record(self, stage: str, reason: str) -> None:
        code = (
            reason
            if isinstance(reason, str) and _ERROR_CODE.fullmatch(reason)
            else "admission_refused"
        )
        if stage not in self._entries:
            stage = "registration"
        logger.warning(
            "voice_worker_admission_refused stage=%s reason=%s", stage, code
        )
        self._entries[stage].appendleft(
            AdmissionRefusal(stage=stage, reason=code, occurred_at=self._utcnow())
        )

    def snapshot(self) -> tuple[AdmissionRefusal, ...]:
        """Return retained refusals, most recent first across both stages."""

        merged = [entry for stage in self._entries.values() for entry in stage]
        merged.sort(key=lambda entry: entry.occurred_at, reverse=True)
        return tuple(merged)


class WorkerDisconnectHook(Protocol):
    async def __call__(
        self,
        receipt: WorkerRegistrationReceipt,
        released_session_ids: tuple[str, ...],
    ) -> None: ...


class WorkerFrameHook(Protocol):
    """Consume one frame only after the pool authenticated every fence."""

    def __call__(
        self,
        receipt: WorkerRegistrationReceipt,
        frame: Mapping[str, Any],
    ) -> Awaitable[None]: ...


class _RouterOwner(Protocol):
    def include_router(self, router: APIRouter) -> Any: ...


class _StarletteWorkerSocket:
    """Small adapter satisfying the WorkerPool socket contract."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._closed = False

    async def send(self, payload: str) -> None:
        if self._closed:
            raise RuntimeError("worker_socket_closed")
        await self._websocket.send_text(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._websocket.close(code=code, reason=reason)
        except (RuntimeError, WebSocketDisconnect):
            return


class WorkerControlEndpoint:
    """Own challenge authentication and one bounded worker socket loop."""

    def __init__(
        self,
        pool: WorkerPool,
        settings: WorkerControlSettings,
        *,
        challenges: WorkerChallengeStore | None = None,
        disconnect_hook: WorkerDisconnectHook | None = None,
        frame_hook: WorkerFrameHook | None = None,
        refusals: AdmissionRefusalLog | None = None,
    ) -> None:
        if not isinstance(pool, WorkerPool):
            raise TypeError("pool must be WorkerPool")
        self.pool = pool
        self.settings = settings
        self.challenges = challenges or WorkerChallengeStore(settings)
        self.refusals = refusals or AdmissionRefusalLog()
        self._disconnect_hook = disconnect_hook
        self._frame_hook = frame_hook
        self.router = APIRouter()
        self.router.add_api_websocket_route(
            WORKER_CONTROL_PATH,
            self.handle,
            name="voice_worker_control",
        )

    def readiness(self) -> Any:
        """Expose the credential-free pool projection to capability wiring."""

        return self.pool.readiness()

    def worker_status(self) -> tuple[WorkerStatusEntry, ...]:
        """Expose the credential-free per-worker registry facts (FR-034)."""

        return self.pool.worker_status()

    def admission_refusals(self) -> tuple[AdmissionRefusal, ...]:
        """Expose retained admission refusals, most recent first (FR-034)."""

        return self.refusals.snapshot()

    async def handle(self, websocket: WebSocket) -> None:
        if websocket.scope.get("query_string", b""):
            await _deny(websocket, status_code=400)
            return
        if _has_forbidden_upgrade_credential(websocket):
            await _deny(websocket, status_code=401)
            return

        try:
            authenticated_identity = self._authenticate(websocket)
        except WorkerControlAuthError as exc:
            self.refusals.record("authentication", exc.code)
            await _deny(websocket, status_code=401)
            return
        if authenticated_identity is None:
            try:
                challenge = self.challenges.issue()
            except WorkerControlAuthError:
                await _deny(websocket, status_code=503)
                return
            await _deny(
                websocket,
                status_code=401,
                headers={
                    CHALLENGE_NONCE_HEADER: challenge.nonce,
                    CHALLENGE_ISSUED_HEADER: str(challenge.issued_at),
                    CHALLENGE_EXPIRES_HEADER: str(challenge.expires_at),
                },
            )
            return

        await websocket.accept()
        socket = _StarletteWorkerSocket(websocket)
        receipt: WorkerRegistrationReceipt | None = None
        try:
            event = await asyncio.wait_for(
                websocket.receive(),
                timeout=float(self.settings.registration_timeout_seconds),
            )
            registration_payload = _text_payload(event)
            registration = _decode_registration(registration_payload)
            receipt = await self.pool.register_worker(
                registration,
                socket,
                authenticated_identity=authenticated_identity,
            )
            await self._reconcile_registration_fences(receipt)
            await self._run_registered(websocket, receipt)
        except WebSocketDisconnect:
            return
        except asyncio.TimeoutError:
            if receipt is None:
                self.refusals.record("registration", "registration_timeout")
            await socket.close(1008, "registration_timeout")
        except (ControlProtocolError, RegistrationError, StaleFence) as exc:
            if receipt is None:
                self.refusals.record(
                    "registration", getattr(exc, "code", "admission_refused")
                )
            logger.warning(
                "voice_worker_control_closed reason=%s",
                getattr(exc, "code", str(exc)),
            )
            await socket.close(1008, "protocol_violation")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "voice_worker_control_closed reason=%s",
                getattr(exc, "code", type(exc).__name__),
            )
            await socket.close(1011, "worker_control_failed")
        finally:
            if receipt is not None:
                await _cleanup_connection(
                    self.pool,
                    receipt,
                    self._disconnect_hook,
                )

    async def _reconcile_registration_fences(
        self,
        receipt: WorkerRegistrationReceipt,
    ) -> None:
        """Reconcile assignments fenced while replacing the same worker.

        The replaced connection's later unregister is deliberately a no-op,
        so its assignment IDs must reach the same credential-free cleanup
        hook immediately after the replacement commits.
        """

        if receipt.fenced_assignments and self._disconnect_hook is not None:
            await self._disconnect_hook(receipt, ())

    def _authenticate(self, websocket: WebSocket) -> str | None:
        present = tuple(
            bool(_header_values(websocket.headers, name)) for name in _AUTH_HEADERS
        )
        if not any(present):
            return None
        if not all(present):
            raise WorkerControlAuthError("invalid_authentication")

        identity = _single_header(websocket.headers, WORKER_HEADER)
        nonce = _single_header(websocket.headers, NONCE_HEADER)
        timestamp_text = _single_header(websocket.headers, TIMESTAMP_HEADER)
        signature = _single_header(websocket.headers, SIGNATURE_HEADER)
        challenge = self.challenges.consume(nonce)
        if _OPAQUE_ID.fullmatch(identity) is None:
            raise WorkerControlAuthError("invalid_authentication")
        if (
            not timestamp_text.isascii()
            or not timestamp_text.isdigit()
            or len(timestamp_text) > 16
        ):
            raise WorkerControlAuthError("invalid_authentication")
        timestamp = int(timestamp_text, 10)
        if str(timestamp) != timestamp_text:
            raise WorkerControlAuthError("invalid_authentication")
        now = self.challenges._now()
        if (
            timestamp < challenge.issued_at
            or timestamp > challenge.expires_at
            or abs(timestamp - now) > _CLOCK_SKEW_SECONDS
            or _SHA256.fullmatch(signature) is None
        ):
            raise WorkerControlAuthError("invalid_authentication")
        canonical = b"\n".join(
            (
                _CHALLENGE_DOMAIN,
                identity.encode("ascii"),
                nonce.encode("ascii"),
                timestamp_text.encode("ascii"),
            )
        )
        expected = hmac.new(
            self.settings.secret,
            canonical,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WorkerControlAuthError("invalid_authentication")
        return identity

    async def _run_registered(
        self,
        websocket: WebSocket,
        receipt: WorkerRegistrationReceipt,
    ) -> None:
        while True:
            try:
                event = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=float(self.settings.lease_sweep_seconds),
                )
            except asyncio.TimeoutError:
                expired = await self.pool.expire_connection_leases()
                await self._reconcile_expired_connections(expired)
                if any(
                    item.connection_id == receipt.connection_id
                    for item in expired
                ):
                    return
                continue
            payload = _text_payload(event)
            try:
                frame = await self.pool.receive_worker_frame(
                    receipt.connection_id,
                    payload,
                )
            except (ControlProtocolError, StaleFence):
                attributable = (
                    await self.pool.attributable_worker_frame_reservation(
                        receipt.connection_id,
                        payload,
                    )
                )
                if attributable is None:
                    raise
                await self._quarantine_session_fault(
                    receipt,
                    *attributable,
                )
                continue
            if self._frame_hook is not None:
                try:
                    await self._frame_hook(receipt, frame)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    attributable = (
                        await self.pool.attributable_worker_frame_reservation(
                            receipt.connection_id,
                            payload,
                        )
                    )
                    if attributable is None:
                        raise
                    logger.warning(
                        "voice_worker_session_quarantined "
                        "reason=frame_effect_failed"
                    )
                    await self._quarantine_session_fault(
                        receipt,
                        *attributable,
                    )

    async def _quarantine_session_fault(
        self,
        receipt: WorkerRegistrationReceipt,
        reservation: SessionReservation,
        media_grant_revision: int,
    ) -> None:
        """End and release exactly one rejected assignment, preserving peers."""

        try:
            await self.pool.send_session_command(
                reservation,
                "end_session",
                {
                    "media_grant_revision": media_grant_revision,
                    "reason": "media_error",
                },
            )
        except StaleFence:
            pass
        released = (
            await self.pool.release_attributable_worker_frame_reservation(
                reservation,
            )
        )
        if not released or self._disconnect_hook is None:
            return
        await self._disconnect_hook(
            WorkerRegistrationReceipt(
                connection_id=receipt.connection_id,
                worker_identity=receipt.worker_identity,
                accepted_max_sessions=receipt.accepted_max_sessions,
                fenced_assignments=(reservation.assignment_id,),
            ),
            (reservation.session_id,),
        )

    async def _reconcile_expired_connections(
        self,
        releases: tuple[WorkerConnectionRelease, ...],
    ) -> None:
        """Deliver every lease-expiry fence before unregister becomes a no-op."""

        if self._disconnect_hook is None:
            return
        for release in releases:
            await self._disconnect_hook(
                WorkerRegistrationReceipt(
                    connection_id=release.connection_id,
                    worker_identity=release.worker_identity,
                    accepted_max_sessions=release.accepted_max_sessions,
                    fenced_assignments=release.assignment_ids,
                ),
                release.session_ids,
            )


def install_router(
    app: _RouterOwner,
    pool: WorkerPool,
    *,
    settings: WorkerControlSettings | None = None,
    challenges: WorkerChallengeStore | None = None,
    disconnect_hook: WorkerDisconnectHook | None = None,
    frame_hook: WorkerFrameHook | None = None,
) -> WorkerControlEndpoint:
    """Install the worker-only route without importing the application singleton."""

    state = getattr(app, "state", None)
    existing = getattr(state, _APP_STATE_KEY, None) if state is not None else None
    if existing is not None:
        if not isinstance(existing, WorkerControlEndpoint) or existing.pool is not pool:
            raise WorkerControlConfigError("worker_control_already_installed")
        return existing
    if _contains_route_path(app, WORKER_CONTROL_PATH):
        raise WorkerControlConfigError("worker_control_route_conflict")
    endpoint = WorkerControlEndpoint(
        pool,
        settings or WorkerControlSettings.from_environ(),
        challenges=challenges,
        disconnect_hook=disconnect_hook,
        frame_hook=frame_hook,
    )
    app.include_router(endpoint.router)
    if state is not None:
        setattr(state, _APP_STATE_KEY, endpoint)
    return endpoint


def _contains_route_path(owner: Any, path: str) -> bool:
    """Inspect both ordinary routes and FastAPI's included-router wrappers."""

    pending = [owner]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        for route in getattr(current, "routes", ()):
            if getattr(route, "path", None) == path:
                return True
            original_router = getattr(route, "original_router", None)
            if original_router is not None:
                pending.append(original_router)
    return False


def _decode_registration(payload: str) -> dict[str, Any]:
    if not isinstance(payload, str):
        raise ControlProtocolError("text_frame_required")
    try:
        encoded = payload.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ControlProtocolError("invalid_utf8") from None
    if len(encoded) > MAX_CONTROL_FRAME_BYTES:
        raise ControlProtocolError("frame_too_large")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: _invalid_number(),
        )
    except ControlProtocolError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ControlProtocolError("invalid_json") from None
    if not isinstance(value, dict):
        raise ControlProtocolError("object_frame_required")
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControlProtocolError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_number() -> None:
    raise ControlProtocolError("invalid_json_number")


def _text_payload(event: Mapping[str, Any]) -> str:
    if event.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(
            code=int(event.get("code", 1000)),
            reason=str(event.get("reason", "")),
        )
    if event.get("type") != "websocket.receive":
        raise ControlProtocolError("invalid_websocket_event")
    payload = event.get("text")
    if not isinstance(payload, str) or event.get("bytes") is not None:
        raise ControlProtocolError("text_frame_required")
    return payload


def _header_values(headers: Any, name: str) -> list[str]:
    try:
        values = headers.getlist(name)
    except AttributeError:
        values = [
            value
            for key, value in headers.items()
            if isinstance(key, str) and key.lower() == name.lower()
        ]
    return [value for value in values if isinstance(value, str)]


def _single_header(headers: Any, name: str) -> str:
    values = _header_values(headers, name)
    if len(values) != 1:
        raise WorkerControlAuthError("invalid_authentication")
    value = values[0]
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise WorkerControlAuthError("invalid_authentication") from None
    if (
        not value
        or value != value.strip()
        or len(encoded) > 256
        or any(byte < 0x20 or byte == 0x7F for byte in encoded)
    ):
        raise WorkerControlAuthError("invalid_authentication")
    return value


def _has_forbidden_upgrade_credential(websocket: WebSocket) -> bool:
    return bool(
        _header_values(websocket.headers, "authorization")
        or _header_values(websocket.headers, "cookie")
    )


async def _deny(
    websocket: WebSocket,
    *,
    status_code: int,
    headers: Mapping[str, str] | None = None,
) -> None:
    safe_headers = {
        "cache-control": "no-store",
        "pragma": "no-cache",
        **dict(headers or {}),
    }
    response = Response(
        content=b"",
        status_code=status_code,
        headers=safe_headers,
        media_type=None,
    )
    # Uvicorn supplies the HTTP-upgrade denial Content-Length itself.  Keeping
    # Starlette's automatically generated copy produces two Content-Length
    # headers on the wire; strict WebSocket clients correctly reject that as
    # an invalid HTTP response before they can read the challenge headers.
    response.raw_headers = [
        (name, value)
        for name, value in response.raw_headers
        if name.lower() != b"content-length"
    ]
    try:
        await websocket.send_denial_response(response)
    except RuntimeError:
        await websocket.close(code=1008, reason="upgrade_refused")


async def _cleanup_connection(
    pool: WorkerPool,
    receipt: WorkerRegistrationReceipt,
    hook: WorkerDisconnectHook | None,
) -> None:
    async def cleanup() -> None:
        released = await pool.unregister_worker(receipt.connection_id)
        if hook is not None:
            await hook(receipt, released)

    cleanup_task = asyncio.create_task(cleanup())
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        await cleanup_task
        raise


__all__ = [
    "CHALLENGE_EXPIRES_HEADER",
    "CHALLENGE_ISSUED_HEADER",
    "CHALLENGE_NONCE_HEADER",
    "NONCE_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "WORKER_CONTROL_PATH",
    "WORKER_HEADER",
    "UpgradeChallenge",
    "WorkerChallengeStore",
    "WorkerControlAuthError",
    "WorkerControlConfigError",
    "WorkerControlEndpoint",
    "WorkerControlSettings",
    "install_router",
]
