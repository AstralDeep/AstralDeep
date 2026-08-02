"""Orchestrator-only LiveKit grant and room-administration boundary.

Only this process receives the LiveKit API key pair. The isolated media worker
and clients receive separate short-lived, room-scoped join grants and never an
API credential or room-administration capability.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID


_LIVEKIT_API_VERSION = "1.2.0"
_OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class LiveKitConfigError(RuntimeError):
    """Deployment configuration cannot safely expose voice media."""


class LiveKitUnavailable(RuntimeError):
    """Content-free LiveKit operation failure safe for logs and clients."""


@dataclass(frozen=True, slots=True)
class LiveKitSettings:
    """Validated, explicit settings; credential fields are absent from repr."""

    internal_url: str
    public_url: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    environment: str = "production"
    grant_ttl_seconds: int = 300
    readiness_ttl_seconds: int = 10
    operation_timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        _validate_settings(self)

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> LiveKitSettings:
        values = environ if environ is not None else os.environ
        required = {
            name: values.get(name, "").strip()
            for name in (
                "LIVEKIT_INTERNAL_URL",
                "LIVEKIT_PUBLIC_URL",
                "LIVEKIT_API_KEY",
                "LIVEKIT_API_SECRET",
            )
        }
        if not all(required.values()):
            raise LiveKitConfigError("missing_livekit_configuration")
        environment = values.get("ASTRAL_ENV", "").strip().lower() or "production"
        return cls(
            internal_url=required["LIVEKIT_INTERNAL_URL"],
            public_url=required["LIVEKIT_PUBLIC_URL"],
            api_key=required["LIVEKIT_API_KEY"],
            api_secret=required["LIVEKIT_API_SECRET"],
            environment=environment,
        )


@dataclass(frozen=True, slots=True)
class LiveKitReadiness:
    status: str
    reason: str
    checked_at: str
    expires_at: str


class TokenIssuer(Protocol):
    def issue(
        self,
        *,
        room_name: str,
        identity: str,
        issued_at: datetime,
        ttl_seconds: int,
        can_publish: bool,
        can_subscribe: bool,
        can_publish_data: bool,
        can_publish_microphone: bool,
    ) -> str: ...


class RoomAdmin(Protocol):
    async def probe_room_service(self) -> None: ...

    async def create_room(self, room_name: str) -> None: ...

    async def remove_participant(self, room_name: str, identity: str) -> None: ...

    async def delete_room(self, room_name: str) -> None: ...

    async def close(self) -> None: ...


class WorkerReadiness(Protocol):
    """Credential-free exact-profile worker-pool readiness projection."""

    ready: bool
    reason: str
    worker_count: int
    capacity_available: int
    profile: Mapping[str, str | int]


class WorkerReadinessProvider(Protocol):
    def readiness(self) -> WorkerReadiness: ...


class LiveKitTokenIssuer:
    """Exact-version LiveKit token builder with explicit credentials only."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        _require_livekit_api()
        self._api_key = api_key
        self._api_secret = api_secret

    def issue(
        self,
        *,
        room_name: str,
        identity: str,
        issued_at: datetime,
        ttl_seconds: int,
        can_publish: bool,
        can_subscribe: bool,
        can_publish_data: bool,
        can_publish_microphone: bool,
    ) -> str:
        from livekit import api
        from livekit.api import access_token

        sources = ["microphone"] if can_publish_microphone else []
        grants = api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=can_publish,
            can_subscribe=can_subscribe,
            can_publish_data=can_publish_data,
            can_publish_sources=sources,
            can_update_own_metadata=False,
            room_create=False,
            room_list=False,
            room_record=False,
            room_admin=False,
            ingress_admin=False,
        )
        builder = (
            api.AccessToken(self._api_key, self._api_secret)
            .with_identity(identity)
            .with_ttl(timedelta(seconds=ttl_seconds))
            .with_grants(grants)
        )
        issued = _aware(issued_at, "issued_at")
        claims = builder.claims.asdict()
        claims.update(
            {
                "sub": identity,
                "iss": self._api_key,
                "nbf": int(issued.timestamp()),
                "exp": int((issued + timedelta(seconds=ttl_seconds)).timestamp()),
            }
        )
        return access_token.jwt.encode(claims, self._api_secret, algorithm="HS256")


class _LiveKitRoomAdmin:
    def __init__(self, settings: LiveKitSettings) -> None:
        _require_livekit_api()
        self._settings = settings
        self._api_module: Any | None = None
        self._client: Any | None = None

    def _runtime_client(self) -> tuple[Any, Any]:
        """Create aiohttp-backed LiveKitAPI only inside a running event loop."""

        if self._client is None:
            from livekit import api

            self._api_module = api
            self._client = api.LiveKitAPI(
                url=self._settings.internal_url,
                api_key=self._settings.api_key,
                api_secret=self._settings.api_secret,
                failover=False,
            )
        return self._api_module, self._client

    async def probe_room_service(self) -> None:
        api, client = self._runtime_client()
        await client.room.list_rooms(api.ListRoomsRequest(names=[]))

    async def create_room(self, room_name: str) -> None:
        api, client = self._runtime_client()
        await client.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                empty_timeout=60,
                departure_timeout=20,
                max_participants=2,
                sync_streams=False,
                replay_enabled=False,
                agents=[],
            )
        )

    async def remove_participant(self, room_name: str, identity: str) -> None:
        api, client = self._runtime_client()
        await client.room.remove_participant(
            api.RoomParticipantIdentity(
                room=room_name,
                identity=identity,
            )
        )

    async def delete_room(self, room_name: str) -> None:
        api, client = self._runtime_client()
        await client.room.delete_room(
            api.DeleteRoomRequest(room=room_name)
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._api_module = None


class LiveKitService:
    """Mint least-privilege grants and perform bounded room operations."""

    def __init__(
        self,
        settings: LiveKitSettings,
        *,
        token_issuer: TokenIssuer | None = None,
        admin: RoomAdmin | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._token_issuer = token_issuer or LiveKitTokenIssuer(
            settings.api_key,
            settings.api_secret,
        )
        self._admin = admin or _LiveKitRoomAdmin(settings)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._readiness_cache: tuple[datetime, LiveKitReadiness] | None = None
        self._readiness_lock = asyncio.Lock()

    def mint_client_grant(
        self,
        *,
        grant_id: str,
        session_id: str,
        generation: int,
        media_grant_revision: int,
        room_name: str,
        participant_identity: str,
        worker_identity: str,
        issued_at: datetime,
    ) -> dict[str, Any]:
        """Mint one client grant restricted to its assigned room."""

        _opaque(grant_id, "grant_id")
        _uuid4(session_id, "session_id")
        _positive(generation, "generation")
        _positive(media_grant_revision, "media_grant_revision")
        _opaque(room_name, "room_name")
        _opaque(participant_identity, "participant_identity")
        _opaque(worker_identity, "worker_identity")
        expires_at = _grant_expiry(issued_at, self._settings.grant_ttl_seconds)
        token = self._issue_join_token(
            room_name,
            participant_identity,
            issued_at,
            can_publish_data=False,
        )
        return {
            "grant_id": grant_id,
            "transport": "livekit",
            "session_id": session_id,
            "generation": generation,
            "media_grant_revision": media_grant_revision,
            "expires_at": _iso(expires_at),
            "url": self._settings.public_url,
            "join_token": token,
            "room_name": room_name,
            "participant_identity": participant_identity,
            "worker_identity": worker_identity,
        }

    def mint_worker_grant(
        self,
        *,
        revision: int,
        room_name: str,
        worker_identity: str,
        issued_at: datetime,
    ) -> dict[str, Any]:
        """Mint a distinct memory-only direct-RTC worker grant."""

        _positive(revision, "revision")
        _opaque(room_name, "room_name")
        _opaque(worker_identity, "worker_identity")
        expires_at = _grant_expiry(issued_at, self._settings.grant_ttl_seconds)
        return {
            "revision": revision,
            "livekit_url": _rtc_url(self._settings.internal_url),
            "join_token": self._issue_join_token(
                room_name,
                worker_identity,
                issued_at,
                can_publish_data=True,
            ),
            "issued_at": _iso(issued_at),
            "expires_at": _iso(expires_at),
            "room_name": room_name,
            "worker_identity": worker_identity,
        }

    def _issue_join_token(
        self,
        room_name: str,
        identity: str,
        issued_at: datetime,
        *,
        can_publish_data: bool,
    ) -> str:
        token = self._token_issuer.issue(
            room_name=room_name,
            identity=identity,
            issued_at=_aware(issued_at, "issued_at"),
            ttl_seconds=self._settings.grant_ttl_seconds,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=can_publish_data,
            can_publish_microphone=True,
        )
        if not isinstance(token, str) or not 32 <= len(token) <= 8192:
            raise LiveKitUnavailable("grant_mint_failed")
        return token

    async def rotate_client_participant(
        self,
        *,
        previous_identity: str,
        grant_id: str,
        session_id: str,
        generation: int,
        media_grant_revision: int,
        room_name: str,
        participant_identity: str,
        worker_identity: str,
        issued_at: datetime,
    ) -> dict[str, Any]:
        """Fence the prior participant before exposing the replacement grant."""

        await self.remove_participant(room_name, previous_identity)
        return self.mint_client_grant(
            grant_id=grant_id,
            session_id=session_id,
            generation=generation,
            media_grant_revision=media_grant_revision,
            room_name=room_name,
            participant_identity=participant_identity,
            worker_identity=worker_identity,
            issued_at=issued_at,
        )

    async def readiness(self) -> LiveKitReadiness:
        """Return a short-lived, coalesced, content-free room-service probe."""

        now = _aware(self._clock(), "clock")
        cached = self._readiness_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        async with self._readiness_lock:
            now = _aware(self._clock(), "clock")
            cached = self._readiness_cache
            if cached is not None and now < cached[0]:
                return cached[1]
            try:
                async with asyncio.timeout(self._settings.operation_timeout_seconds):
                    await self._admin.probe_room_service()
            except Exception:
                status = "unavailable"
                reason = "media_unreachable"
            else:
                status = "ready"
                reason = "ready"
            expires = now + timedelta(seconds=self._settings.readiness_ttl_seconds)
            result = LiveKitReadiness(
                status=status,
                reason=reason,
                checked_at=_iso(now),
                expires_at=_iso(expires),
            )
            self._readiness_cache = (expires, result)
            return result

    async def remove_participant(self, room_name: str, identity: str) -> None:
        _opaque(room_name, "room_name")
        _opaque(identity, "participant_identity")
        await self._admin_operation(
            self._admin.remove_participant(room_name, identity),
            "participant_removal_failed",
            ignore_not_found=True,
        )

    async def ensure_room(self, room_name: str) -> None:
        """Create the bounded two-party room through admin authority only."""

        _opaque(room_name, "room_name")
        await self._admin_operation(
            self._admin.create_room(room_name),
            "room_creation_failed",
        )

    async def delete_room(self, room_name: str) -> None:
        _opaque(room_name, "room_name")
        await self._admin_operation(
            self._admin.delete_room(room_name),
            "room_deletion_failed",
            ignore_not_found=True,
        )

    async def _admin_operation(
        self,
        operation: Any,
        reason: str,
        *,
        ignore_not_found: bool = False,
    ) -> None:
        try:
            async with asyncio.timeout(self._settings.operation_timeout_seconds):
                await operation
        except Exception as exc:
            # Teardown and participant fencing are retry-safe. A participant
            # that already left, or a room already deleted by LiveKit after
            # its last participant departed, is the requested end state.
            if ignore_not_found and _is_livekit_not_found(exc):
                return
            raise LiveKitUnavailable(reason) from exc

    async def close(self) -> None:
        try:
            async with asyncio.timeout(self._settings.operation_timeout_seconds):
                await self._admin.close()
        except Exception as exc:
            raise LiveKitUnavailable("admin_close_failed") from exc


def _is_livekit_not_found(exc: Exception) -> bool:
    """Recognize only the pinned SDK's typed 404 response."""

    try:
        from livekit.api.twirp_client import ServerError
    except (ImportError, ModuleNotFoundError):
        return False
    return (
        isinstance(exc, ServerError)
        and exc.status == 404
        and exc.code == "not_found"
    )


_CAPABILITY_PROFILE = {
    "asr_model": "Systran/faster-whisper-large-v3",
    "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
    "voice": "af_heart",
    "output_locale": "en-US",
    "output_format": "wav",
    "output_sample_rate_hz": 24_000,
}
_WORKER_PROFILE = {
    "asr_model": _CAPABILITY_PROFILE["asr_model"],
    "tts_model": _CAPABILITY_PROFILE["tts_model"],
    "voice": _CAPABILITY_PROFILE["voice"],
    "output_locale": _CAPABILITY_PROFILE["output_locale"],
    "format": _CAPABILITY_PROFILE["output_format"],
    "sample_rate_hz": _CAPABILITY_PROFILE["output_sample_rate_hz"],
}
_CAPABILITY_STATUSES = {"ready", "unavailable", "degraded", "checking"}
_CAPABILITY_REASONS = {
    "ready",
    "feature_disabled",
    "media_unconfigured",
    "media_unreachable",
    "worker_unavailable",
    "asr_unavailable",
    "tts_unavailable",
    "voice_unavailable",
    "output_language_unsupported",
    "capacity_exhausted",
    "permission_denied",
    "permission_restricted",
    "no_microphone",
    "no_audio_output",
    "unsupported_transport",
    "authentication_required",
    "internal_error",
}


@dataclass(frozen=True, slots=True)
class VoiceCapability:
    """Safe, exact-profile capability response with no endpoint or credential."""

    status: str
    reason: str
    checked_at: datetime
    expires_at: datetime
    supported_transports: tuple[str, ...]
    components: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.status not in _CAPABILITY_STATUSES:
            raise ValueError("invalid_capability_status")
        if self.reason not in _CAPABILITY_REASONS:
            raise ValueError("invalid_capability_reason")
        if not self.supported_transports or not set(self.supported_transports) <= {
            "livekit",
            "watch_pcm_websocket",
        }:
            raise ValueError("invalid_capability_transports")
        if set(self.components) != {"livekit", "worker", "asr", "tts", "voice"}:
            raise ValueError("invalid_capability_components")
        if not set(self.components.values()) <= _CAPABILITY_STATUSES:
            raise ValueError("invalid_component_status")
        object.__setattr__(
            self,
            "components",
            MappingProxyType(dict(self.components)),
        )
        if _aware(self.expires_at, "expires_at") <= _aware(
            self.checked_at, "checked_at"
        ):
            raise ValueError("invalid_capability_expiry")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "status": self.status,
            "reason": self.reason,
            "checked_at": _iso(self.checked_at),
            "expires_at": _iso(self.expires_at),
            "profile": dict(_CAPABILITY_PROFILE),
            "supported_transports": list(self.supported_transports),
            "components": dict(self.components),
        }


class VoiceCapabilityService:
    """Coalesce bounded LiveKit and preflight-gated worker readiness probes.

    A worker is permitted to register only after its exact model-inventory,
    bounded batch-ASR, Kokoro/af_heart and 24-kHz WAV startup probes pass.  The
    worker pool therefore acts as the credential-free projection of those
    worker-local checks; this process never receives the speech endpoint/key.
    """

    def __init__(
        self,
        *,
        livekit: LiveKitService,
        workers: WorkerReadinessProvider,
        feature_enabled: Callable[[], bool],
        clock: Callable[[], datetime] | None = None,
        cache_ttl_seconds: int = 10,
        supported_transports: tuple[str, ...] = ("livekit",),
        worker_probe: Callable[[], Awaitable[WorkerReadiness]] | None = None,
        observability: Any | None = None,
    ) -> None:
        if not 1 <= cache_ttl_seconds <= 30:
            raise ValueError("invalid_capability_cache_ttl")
        if not supported_transports or not set(supported_transports) <= {
            "livekit",
            "watch_pcm_websocket",
        }:
            raise ValueError("invalid_capability_transports")
        self._livekit = livekit
        self._workers = workers
        self._feature_enabled = feature_enabled
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache_ttl = cache_ttl_seconds
        self._supported_transports = tuple(dict.fromkeys(supported_transports))
        self._worker_probe = worker_probe
        self._observability = observability
        self._cache: VoiceCapability | None = None
        self._lock = asyncio.Lock()

    async def readiness(self) -> VoiceCapability:
        now = _aware(self._clock(), "clock")
        cached = self._cache
        if cached is not None and now < cached.expires_at:
            return cached
        async with self._lock:
            now = _aware(self._clock(), "clock")
            cached = self._cache
            if cached is not None and now < cached.expires_at:
                return cached
            result = await self._probe(now)
            self._cache = result
            if self._observability is not None:
                self._observability.record_voice_event(
                    "readiness",
                    result.status,
                    reason=result.reason,
                )
            return result

    def invalidate(self) -> None:
        self._cache = None

    async def _probe(self, now: datetime) -> VoiceCapability:
        expires = now + timedelta(seconds=self._cache_ttl)
        if not self._safe_feature_enabled():
            return self._result(
                status="unavailable",
                reason="feature_disabled",
                now=now,
                expires=expires,
                components={name: "unavailable" for name in _CAPABILITY_COMPONENTS},
            )

        try:
            livekit_status, worker_status = await asyncio.gather(
                self._livekit.readiness(),
                self._read_workers(),
            )
        except Exception:
            return self._result(
                status="unavailable",
                reason="internal_error",
                now=now,
                expires=expires,
                components={name: "unavailable" for name in _CAPABILITY_COMPONENTS},
            )

        media_ready = livekit_status.status == "ready"
        worker_profile_ready = (
            worker_status.worker_count > 0
            and dict(worker_status.profile) == _WORKER_PROFILE
        )
        components = {
            "livekit": "ready" if media_ready else "unavailable",
            "worker": "ready" if worker_profile_ready else "unavailable",
            "asr": "ready" if worker_profile_ready else "unavailable",
            "tts": "ready" if worker_profile_ready else "unavailable",
            "voice": "ready" if worker_profile_ready else "unavailable",
        }
        speech_reason = (
            worker_status.reason
            if worker_status.reason
            in {"asr_unavailable", "tts_unavailable", "voice_unavailable"}
            else None
        )
        if speech_reason is not None:
            components[speech_reason.removesuffix("_unavailable")] = "unavailable"
        if not media_ready:
            status, reason = "unavailable", "media_unreachable"
        elif not worker_profile_ready:
            status, reason = "unavailable", "worker_unavailable"
        elif speech_reason is not None:
            status, reason = "unavailable", speech_reason
        elif worker_status.capacity_available <= 0 or worker_status.reason == (
            "capacity_exhausted"
        ):
            status, reason = "degraded", "capacity_exhausted"
        elif not worker_status.ready or worker_status.reason != "ready":
            status, reason = "unavailable", "worker_unavailable"
        else:
            status, reason = "ready", "ready"
        return self._result(
            status=status,
            reason=reason,
            now=now,
            expires=expires,
            components=components,
        )

    async def _read_workers(self) -> WorkerReadiness:
        if self._worker_probe is not None:
            return await self._worker_probe()
        return self._workers.readiness()

    def _safe_feature_enabled(self) -> bool:
        try:
            return self._feature_enabled() is True
        except Exception:
            return False

    def _result(
        self,
        *,
        status: str,
        reason: str,
        now: datetime,
        expires: datetime,
        components: Mapping[str, str],
    ) -> VoiceCapability:
        return VoiceCapability(
            status=status,
            reason=reason,
            checked_at=now,
            expires_at=expires,
            supported_transports=self._supported_transports,
            components=components,
        )


_CAPABILITY_COMPONENTS = ("livekit", "worker", "asr", "tts", "voice")


def _require_livekit_api() -> None:
    try:
        installed = importlib.metadata.version("livekit-api")
    except importlib.metadata.PackageNotFoundError as exc:
        raise LiveKitConfigError("livekit_api_dependency_missing") from exc
    if installed != _LIVEKIT_API_VERSION:
        raise LiveKitConfigError("livekit_api_dependency_mismatch")


def _validate_settings(settings: LiveKitSettings) -> None:
    if not settings.api_key or not settings.api_secret:
        raise LiveKitConfigError("missing_livekit_configuration")
    if (
        settings.environment not in {"development", "dev"}
        and len(settings.api_secret.encode("utf-8")) < 32
    ):
        raise LiveKitConfigError("weak_livekit_api_secret")
    _origin(settings.internal_url, {"http", "https"}, "invalid_internal_url")
    public = _origin(settings.public_url, {"ws", "wss"}, "invalid_public_url")
    if settings.environment not in {"development", "dev"} and public.scheme != "wss":
        raise LiveKitConfigError("insecure_public_url")
    if not 1 <= settings.grant_ttl_seconds <= 300:
        raise LiveKitConfigError("invalid_grant_ttl")
    if not 1 <= settings.readiness_ttl_seconds <= 30:
        raise LiveKitConfigError("invalid_readiness_ttl")
    if not 0 < settings.operation_timeout_seconds <= 10:
        raise LiveKitConfigError("invalid_operation_timeout")


def _origin(value: str, schemes: set[str], reason: str):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise LiveKitConfigError(reason)
    return parsed


def _rtc_url(internal_url: str) -> str:
    parsed = urlsplit(internal_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}"


def _opaque(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _OPAQUE.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _uuid4(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be UUID4")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field_name} must be UUID4")
    return value


def _positive(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be positive")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _grant_expiry(issued_at: datetime, ttl_seconds: int) -> datetime:
    return _aware(issued_at, "issued_at") + timedelta(seconds=ttl_seconds)


def _iso(value: datetime) -> str:
    return _aware(value, "timestamp").replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


__all__ = [
    "LiveKitConfigError",
    "LiveKitReadiness",
    "LiveKitService",
    "LiveKitSettings",
    "LiveKitUnavailable",
]
