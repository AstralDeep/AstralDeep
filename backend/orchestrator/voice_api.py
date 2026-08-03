"""Authenticated Feature-065 conversational-voice control API.

The media and speech planes are deliberately absent from this router.  Clients
may request server-owned session state and short-lived media grants, but cannot
submit audio, choose a speech endpoint/model/voice, or obtain service secrets.
"""

from __future__ import annotations

import inspect
import math
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Mapping

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from orchestrator.auth import require_user_id
from orchestrator.voice_control_binding import VoiceControlBindingError


router = APIRouter(prefix="/api/voice", tags=["Voice"])

_UUID4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UUID4 = re.compile(_UUID4_PATTERN)
_BINDING = re.compile(r"^[A-Za-z0-9._~-]{32,512}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_NO_STORE = {"Cache-Control": "no-store"}
_CAPABILITY_RATE_LIMIT = 30
_CAPABILITY_RATE_WINDOW_SECONDS = 60.0
_CAPABILITY_RATE_MAX_SUBJECTS = 4_096
_CAPABILITY_LIMITER_INSTALL_LOCK = Lock()

Uuid4 = str


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ClientMediaCapability(_StrictModel):
    has_microphone: bool
    has_audio_output: bool
    microphone_permission: Literal[
        "not_determined", "authorized", "denied", "restricted"
    ]
    full_duplex: bool = False
    transport: Literal["livekit", "watch_pcm_websocket"]


class CreateSessionRequest(_StrictModel):
    device_id: str = Field(pattern=_UUID4_PATTERN)
    device_kind: Literal["web", "windows", "android", "ios", "macos", "watchos"]
    visible_chat_id: str = Field(pattern=_UUID4_PATTERN)
    activation_id: str = Field(pattern=_UUID4_PATTERN)
    capability: ClientMediaCapability
    foreground_active: Literal[True]


class TakeoverSessionRequest(CreateSessionRequest):
    expected_generation: int = Field(ge=1, strict=True)
    expected_media_grant_revision: int = Field(ge=1, strict=True)


class GenerationRequest(_StrictModel):
    expected_generation: int = Field(ge=1, strict=True)
    expected_media_grant_revision: int = Field(ge=1, strict=True)


class RefreshGrantRequest(GenerationRequest):
    refresh_id: str = Field(pattern=_UUID4_PATTERN)
    device_id: str = Field(pattern=_UUID4_PATTERN)


class SensitiveConsentRequest(GenerationRequest):
    turn_id: str = Field(pattern=_UUID4_PATTERN)
    consent_method: Literal["tap", "strict_spoken_control"]


class UpdateSessionRequest(GenerationRequest):
    visible_chat_id: str | None = Field(default=None, pattern=_UUID4_PATTERN)
    speech_muted: bool | None = None
    microphone_enabled: bool | None = None
    foreground_active: bool | None = None
    foreground_reason: Literal[
        "foreground",
        "backgrounded",
        "locked",
        "audio_interrupted",
        "route_unavailable",
        "connection_lost",
    ] | None = None
    interaction: Literal[True] | None = None

    @model_validator(mode="after")
    def validate_mutation(self) -> UpdateSessionRequest:
        changed = (
            self.visible_chat_id,
            self.speech_muted,
            self.microphone_enabled,
            self.foreground_active,
            self.foreground_reason,
            self.interaction,
        )
        if all(item is None for item in changed):
            raise ValueError("session update requires a mutation")
        if (self.foreground_active is None) != (self.foreground_reason is None):
            raise ValueError("foreground state and reason must be supplied together")
        if self.foreground_active is True and self.foreground_reason != "foreground":
            raise ValueError("active foreground state requires foreground reason")
        if self.foreground_active is False:
            if self.foreground_reason == "foreground":
                raise ValueError("inactive foreground state requires an inactive reason")
            if self.microphone_enabled is not False:
                raise ValueError("inactive foreground state must disable microphone")
        return self


@dataclass(frozen=True, slots=True)
class VoiceHttpResult:
    """Runtime result with an explicit HTTP status and safe response payload."""

    payload: Mapping[str, Any] | None = None
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)


class VoiceApiError(RuntimeError):
    """Typed, content-free runtime refusal mapped to a problem response."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.payload = dict(payload or {})
        self.headers = dict(headers or {})
        super().__init__(code)


class _CapabilityRateLimiter:
    """Bounded fixed-window limiter for the authenticated readiness surface."""

    def __init__(
        self,
        *,
        limit: int = _CAPABILITY_RATE_LIMIT,
        window_seconds: float = _CAPABILITY_RATE_WINDOW_SECONDS,
        max_subjects: int = _CAPABILITY_RATE_MAX_SUBJECTS,
        clock: Any = time.monotonic,
    ) -> None:
        if limit <= 0 or window_seconds <= 0 or max_subjects <= 0:
            raise ValueError("invalid_capability_rate_limit")
        self._limit = limit
        self._window_seconds = window_seconds
        self._max_subjects = max_subjects
        self._clock = clock
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def check(self, subject: str) -> None:
        now = float(self._clock())
        cutoff = now - self._window_seconds
        with self._lock:
            events = self._events.get(subject)
            if events is None:
                self._prune_subjects(cutoff)
                if len(self._events) >= self._max_subjects:
                    raise VoiceApiError(
                        "voice_rate_limited",
                        status_code=429,
                        headers={"Retry-After": str(math.ceil(self._window_seconds))},
                    )
                events = deque()
                self._events[subject] = events
            else:
                self._events.move_to_end(subject)
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self._limit:
                retry_after = max(1, math.ceil(events[0] + self._window_seconds - now))
                raise VoiceApiError(
                    "voice_rate_limited",
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
            events.append(now)

    def _prune_subjects(self, cutoff: float) -> None:
        for subject, events in tuple(self._events.items()):
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                self._events.pop(subject, None)


@router.get("/capability")
async def get_voice_capability(
    request: Request,
    user_id: str = Depends(require_user_id),
) -> Response:
    try:
        _capability_rate_limiter(request).check(user_id)
        runtime = _runtime(request)
        value = await _invoke(runtime, "get_capability", user_id=user_id)
        return _response(value)
    except Exception as exc:
        return _error_response(exc)


@router.post("/sessions")
async def create_voice_session(
    request: Request,
    user_id: str = Depends(require_user_id),
) -> Response:
    try:
        body = await _body(request, CreateSessionRequest)
        context = _control_context(request, user_id, body.device_id)
        runtime = _runtime(request)
        value = await _invoke(
            runtime,
            "create_session",
            user_id=user_id,
            control=context,
            request=body.model_dump(mode="json"),
        )
        await _publish_composer(
            request,
            user_id,
            context,
            selected_chat_id=body.visible_chat_id,
        )
        return _response(value, default_status=201)
    except Exception as exc:
        await _publish_composer(request, user_id)
        return _error_response(exc)


@router.post("/sessions/{session_id}/takeover")
async def take_over_voice_session(
    session_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
) -> Response:
    try:
        checked_session_id = _uuid4(session_id, "invalid_session_id")
        body = await _body(request, TakeoverSessionRequest)
        context = _control_context(request, user_id, body.device_id)
        runtime = _runtime(request)
        value = await _invoke(
            runtime,
            "take_over_session",
            user_id=user_id,
            session_id=checked_session_id,
            control=context,
            request=body.model_dump(mode="json"),
        )
        await _publish_composer(
            request,
            user_id,
            context,
            selected_chat_id=body.visible_chat_id,
        )
        return _response(value)
    except Exception as exc:
        await _publish_composer(request, user_id)
        return _error_response(exc)


@router.patch("/sessions/{session_id}")
async def update_voice_session(
    session_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
) -> Response:
    try:
        checked_session_id = _uuid4(session_id, "invalid_session_id")
        body = await _body(request, UpdateSessionRequest)
        context = _control_context(request, user_id)
        runtime = _runtime(request)
        value = await _invoke(
            runtime,
            "update_session",
            user_id=user_id,
            session_id=checked_session_id,
            control=context,
            request=body.model_dump(mode="json", exclude_none=True),
        )
        await _publish_composer(
            request,
            user_id,
            context,
            selected_chat_id=body.visible_chat_id,
        )
        return _response(value)
    except Exception as exc:
        await _publish_composer(request, user_id)
        return _error_response(exc)


@router.delete("/sessions/{session_id}")
async def end_voice_session(
    session_id: str,
    request: Request,
    expected_generation: int,
    expected_media_grant_revision: int,
    user_id: str = Depends(require_user_id),
) -> Response:
    try:
        checked_session_id = _uuid4(session_id, "invalid_session_id")
        fences = GenerationRequest.model_validate(
            {
                "expected_generation": expected_generation,
                "expected_media_grant_revision": expected_media_grant_revision,
            }
        )
        context = _control_context(request, user_id)
        runtime = _runtime(request)
        await _invoke(
            runtime,
            "end_session",
            user_id=user_id,
            session_id=checked_session_id,
            control=context,
            request=fences.model_dump(mode="json"),
        )
        await _publish_composer(request, user_id, context)
        return Response(status_code=204, headers=_NO_STORE)
    except Exception as exc:
        await _publish_composer(request, user_id)
        return _error_response(exc)


@router.post("/sessions/{session_id}/speech/stop")
async def stop_voice_speech(
    session_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
) -> Response:
    try:
        checked_session_id = _uuid4(session_id, "invalid_session_id")
        body = await _body(request, GenerationRequest)
        context = _control_context(request, user_id)
        runtime = _runtime(request)
        await _invoke(
            runtime,
            "stop_speech",
            user_id=user_id,
            session_id=checked_session_id,
            control=context,
            request=body.model_dump(mode="json"),
        )
        await _publish_composer(request, user_id, context)
        return Response(status_code=202, headers=_NO_STORE)
    except Exception as exc:
        await _publish_composer(request, user_id)
        return _error_response(exc)


@router.get("/sessions/{session_id}/media-grants")
async def get_voice_media_grant_state(
    session_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
) -> Response:
    """Return current owner/grant fences without any bearer material."""

    try:
        checked_session_id = _uuid4(session_id, "invalid_session_id")
        context = _control_context(request, user_id)
        value = await _invoke(
            _runtime(request),
            "get_media_grant_state",
            user_id=user_id,
            session_id=checked_session_id,
            control=context,
        )
        return _response(value)
    except Exception as exc:
        return _error_response(exc)


@router.post("/sessions/{session_id}/media-grants")
async def refresh_voice_media_grant(
    session_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
) -> Response:
    """Rotate one reconnect grant through the durable UUID4 CAS."""

    try:
        checked_session_id = _uuid4(session_id, "invalid_session_id")
        body = await _body(request, RefreshGrantRequest)
        context = _control_context(request, user_id, body.device_id)
        value = await _invoke(
            _runtime(request),
            "refresh_media_grant",
            user_id=user_id,
            session_id=checked_session_id,
            control=context,
            request=body.model_dump(mode="json"),
        )
        await _publish_composer(request, user_id, context)
        return _response(value, default_status=201)
    except Exception as exc:
        await _publish_composer(request, user_id)
        return _error_response(exc)


@router.post("/sessions/{session_id}/results/{result_id}/read-consent")
async def consent_to_sensitive_voice_recap(
    session_id: str,
    result_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
) -> Response:
    """Accept one fresh result-bound consent without widening authority."""

    try:
        checked_session_id = _uuid4(session_id, "invalid_session_id")
        if not isinstance(result_id, str) or _OPAQUE_ID.fullmatch(result_id) is None:
            raise VoiceApiError("invalid_result_id", status_code=400)
        body = await _body(request, SensitiveConsentRequest)
        context = _control_context(request, user_id)
        services = _voice_services(request)
        await _invoke(
            services,
            "consent_sensitive_recap",
            user_id=user_id,
            session_id=checked_session_id,
            result_id=result_id,
            control=context,
            request=body.model_dump(mode="json"),
        )
        return Response(status_code=202, headers=_NO_STORE)
    except Exception as exc:
        return _error_response(exc)


def _orchestrator(request: Request) -> Any:
    app = getattr(request.app, "_root_app", None) or request.app
    value = getattr(app.state, "orchestrator", None)
    if value is None:
        raise VoiceApiError("voice_unavailable", status_code=503)
    return value


def _capability_rate_limiter(request: Request) -> _CapabilityRateLimiter:
    state = request.app.state
    limiter = getattr(state, "voice_capability_rate_limiter", None)
    if isinstance(limiter, _CapabilityRateLimiter):
        return limiter
    with _CAPABILITY_LIMITER_INSTALL_LOCK:
        limiter = getattr(state, "voice_capability_rate_limiter", None)
        if limiter is None:
            limiter = _CapabilityRateLimiter()
            state.voice_capability_rate_limiter = limiter
        if not isinstance(limiter, _CapabilityRateLimiter):
            raise VoiceApiError("voice_unavailable", status_code=503)
        return limiter


def _runtime(request: Request) -> Any:
    orchestrator = _orchestrator(request)
    runtime = getattr(orchestrator, "voice_runtime", None)
    if runtime is None:
        raise VoiceApiError("voice_unavailable", status_code=503)
    return runtime


def _voice_services(request: Request) -> Any:
    services = getattr(_orchestrator(request), "voice_services", None)
    if services is None:
        raise VoiceApiError("voice_unavailable", status_code=503)
    return services


def _control_context(
    request: Request,
    user_id: str,
    body_device_id: str | None = None,
) -> Mapping[str, Any]:
    device_id = request.headers.get("X-Astral-Device-Id", "")
    connection = request.headers.get("X-Astral-Connection-Generation", "")
    bearer = request.headers.get("X-Astral-Voice-Control-Binding", "")
    if _UUID4.fullmatch(device_id) is None or _UUID4.fullmatch(connection) is None:
        raise VoiceApiError("binding_scope_mismatch", status_code=403)
    if body_device_id is not None and body_device_id != device_id:
        raise VoiceApiError("binding_scope_mismatch", status_code=403)
    if _BINDING.fullmatch(bearer) is None:
        raise VoiceApiError("invalid_binding", status_code=403)
    claims = _orchestrator(request).validate_voice_control_binding(
        bearer=bearer,
        subject=user_id,
        device_id=device_id,
        connection_generation=connection,
    )
    return {
        "subject": claims.subject,
        "device_id": claims.device_id,
        "connection_generation": claims.connection_generation,
        "binding_id": claims.binding_id,
        "binding_expires_at": claims.expires_at,
    }


async def _body(request: Request, model: type[_StrictModel]) -> _StrictModel:
    try:
        raw = await request.json()
        return model.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        raise VoiceApiError("invalid_request", status_code=400) from None


async def _invoke(runtime: Any, method_name: str, **kwargs: Any) -> Any:
    method = getattr(runtime, method_name, None)
    if not callable(method):
        raise VoiceApiError("voice_unavailable", status_code=503)
    value = method(**kwargs)
    if inspect.isawaitable(value):
        value = await value
    return value


async def _publish_composer(
    request: Request,
    user_id: str,
    control: Mapping[str, Any] | None = None,
    *,
    selected_chat_id: str | None = None,
) -> None:
    """Best-effort WS projection after the durable REST decision."""

    try:
        device_id = (
            control.get("device_id")
            if control is not None
            else request.headers.get("X-Astral-Device-Id", "")
        )
        connection_generation = (
            control.get("connection_generation")
            if control is not None
            else request.headers.get("X-Astral-Connection-Generation", "")
        )
        if (
            not isinstance(device_id, str)
            or _UUID4.fullmatch(device_id) is None
            or not isinstance(connection_generation, str)
            or _UUID4.fullmatch(connection_generation) is None
        ):
            return
        publisher = getattr(
            _orchestrator(request), "publish_voice_composer_state", None
        )
        if not callable(publisher):
            return
        result = publisher(
            user_id=user_id,
            device_id=device_id,
            connection_generation=connection_generation,
            selected_chat_id=selected_chat_id,
        )
        if inspect.isawaitable(result):
            await result
    except Exception:
        # The REST decision and generation fence remain authoritative. A
        # disconnected UI receives the projection after its next registration.
        return


def _response(value: Any, *, default_status: int = 200) -> Response:
    if isinstance(value, VoiceHttpResult):
        status_code = value.status_code
        payload = value.payload
        headers = dict(value.headers)
    else:
        status_code = default_status
        payload = value.to_dict() if callable(getattr(value, "to_dict", None)) else value
        headers = {}
    headers.update(_NO_STORE)
    if payload is None:
        return Response(status_code=status_code, headers=headers)
    if not isinstance(payload, Mapping):
        raise VoiceApiError("invalid_voice_runtime_response", status_code=503)
    return JSONResponse(dict(payload), status_code=status_code, headers=headers)


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, VoiceApiError):
        status_code = exc.status_code
        code = exc.code
        payload = exc.payload
        headers = exc.headers
    elif isinstance(exc, VoiceControlBindingError):
        status_code = 403
        code = exc.code
        payload = {}
        headers = {}
    else:
        code = getattr(exc, "code", "voice_unavailable")
        status_code = _status_for_code(code)
        payload = {}
        headers = {}
    body = {
        "type": f"urn:astraldeep:voice:{code}",
        "title": "Voice request could not be completed",
        "status": status_code,
        "code": code,
    }
    body.update(payload)
    safe_headers = dict(headers)
    safe_headers.update(_NO_STORE)
    return JSONResponse(body, status_code=status_code, headers=safe_headers)


def _status_for_code(code: object) -> int:
    if code in {"voice_session_not_found", "session_not_found"}:
        return 404
    if code in {"capacity_exhausted", "voice_rate_limited"}:
        return 429
    if code in {
        "voice_takeover_required",
        "stale_generation",
        "stale_media_grant_revision",
        "idempotency_conflict",
        "context_sync_pending",
        "sensitive_consent_scope_mismatch",
        "sensitive_consent_unavailable",
    }:
        return 409
    if code in {
        "invalid_binding",
        "binding_expired",
        "binding_not_current",
        "binding_scope_mismatch",
    }:
        return 403
    if code in {"invalid_request", "invalid_session_id", "invalid_result_id"}:
        return 400
    return 503


def _uuid4(value: object, code: str) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        raise VoiceApiError(code, status_code=400)
    return value
