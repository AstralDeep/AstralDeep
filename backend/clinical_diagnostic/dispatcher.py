"""Drives one clinical prompt through Astral's ordinary authenticated WebSocket
dispatcher (register, chat, terminal frames) with bounded frames/bytes/time, and offers
owner-isolation probes over the REST chat route; it never fabricates an answer or bypasses
identity, tool permission, or audit gates.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple
from uuid import uuid4

from clinical_diagnostic.models import AnswerSource, CaseAnswer, ClinicalCase, FailureCode
from clinical_diagnostic.provider import ProducerError

MAX_FRAMES = 2000
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ANSWER_CHARS = 64 * 1024
TERMINAL_TYPES = ("turn_complete", "chat_complete")
CHAT_TARGETS = ("chat", "history")
ASSISTANT_TEXT_TYPES = ("assistant_message", "chat_message", "message")
_TEXT_FIELDS = ("text", "content", "label", "title", "message")
_DENIAL_MARKERS = (
    "restricted for this agent",
    "permission denied",
    "not authorized",
    "outside this unattended task",
    "system-blocked",
)
_HTML_TAG = re.compile(r"<[^>]+>")


class DispatcherError(ProducerError):
    def __init__(self, detail: str, *, code: FailureCode = FailureCode.PROTOCOL_ERROR,
                 retryable: bool = False) -> None:
        super().__init__(detail, code=code, retryable=retryable)


class DispatcherTimeout(DispatcherError):
    def __init__(self, detail: str = "dispatcher response timed out") -> None:
        super().__init__(detail, code=FailureCode.TIMEOUT, retryable=True)


class DispatcherDenied(DispatcherError):
    def __init__(self, detail: str = "dispatcher denied the request") -> None:
        super().__init__(detail, code=FailureCode.DISPATCHER_DENIED)


@dataclass(frozen=True)
class DispatchedAnswer:
    text: str
    chat_id: str
    frame_types: Tuple[str, ...] = ()
    tool_denials: Tuple[str, ...] = ()
    audit_event_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("dispatched answer must be non-empty")
        if len(self.text) > MAX_ANSWER_CHARS:
            raise ValueError("dispatched answer exceeds the size bound")
        if not isinstance(self.chat_id, str) or not self.chat_id.strip():
            raise ValueError("dispatched answer must retain its chat identity")
        for identifier in self.audit_event_ids:
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError("dispatched answer audit ids must be non-empty strings")


@dataclass(frozen=True)
class IsolationProbe:
    status: int
    denied: bool
    detail: str

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise ValueError("isolation probe status must be an integer")


class WebsocketTransport(Protocol):
    def send(self, text: str) -> None:
        ...

    def recv(self, timeout: float) -> str:
        ...

    def close(self) -> None:
        ...


class HttpResponse(Protocol):
    status: int
    body: bytes


class DispatcherAdapter(Protocol):
    identity: str

    def answer(self, case: ClinicalCase) -> DispatchedAnswer:
        ...

    def probe_foreign_chat(self, chat_id: str) -> IsolationProbe:
        ...


def render_dispatcher_prompt(case: ClinicalCase) -> str:
    if not case.conversation:
        raise ValueError("case conversation is empty")
    lines = [
        "Synthetic clinical diagnostic transcript. Answer the final user request only.",
        "",
    ]
    for message in case.conversation:
        lines.append(f"{message.role}: {message.content}")
    return "\n".join(lines)


class WebSocketDispatcher:
    def __init__(
        self,
        *,
        token: str,
        ws_uri: str,
        http_uri: str,
        identity: str = "astral-dispatcher/ws-v1",
        timeout_seconds: float = 120.0,
        max_frames: int = MAX_FRAMES,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
        transport_factory: Optional[Callable[[], WebsocketTransport]] = None,
        http: Optional[Callable[[str, str, Mapping[str, str], Optional[bytes]], HttpResponse]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("dispatcher requires an authenticated bearer token")
        for name, value in (("ws_uri", ws_uri), ("http_uri", http_uri)):
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme not in ("ws", "wss", "http", "https") or not parsed.netloc:
                raise ValueError(f"{name} must be an absolute ws(s)/http(s) URL")
        if not 1.0 <= timeout_seconds <= 600.0:
            raise ValueError("timeout_seconds must be within 1..600")
        if not 1 <= max_frames <= MAX_FRAMES:
            raise ValueError("max_frames is outside the bound")
        if not 1024 <= max_message_bytes <= MAX_MESSAGE_BYTES:
            raise ValueError("max_message_bytes is outside the bound")
        self._token = token
        self.ws_uri = ws_uri.rstrip("/")
        self.http_uri = http_uri.rstrip("/")
        self.identity = identity
        self.timeout_seconds = float(timeout_seconds)
        self.max_frames = max_frames
        self.max_message_bytes = max_message_bytes
        self._transport_factory = transport_factory or self._default_transport
        self._http = http or _http_request
        self._clock = clock or time.monotonic

    def __repr__(self) -> str:
        return f"WebSocketDispatcher(identity={self.identity!r}, ws_uri={self.ws_uri!r})"

    def create_chat(self) -> str:
        response = self._http(
            "POST",
            self.http_uri + "/api/chats",
            {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            b"{}",
        )
        if response.status in (401, 403):
            raise DispatcherDenied("chat creation was denied")
        if response.status not in (200, 201):
            raise DispatcherError(f"chat creation failed with status {response.status}")
        payload = _bounded_json(response)
        if not isinstance(payload, Mapping):
            raise DispatcherError("chat creation returned no mapping")
        chat_id = payload.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            raise DispatcherError("chat creation returned no chat identity")
        return chat_id

    def probe_foreign_chat(self, chat_id: str) -> IsolationProbe:
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("chat_id is required for the isolation probe")
        response = self._http(
            "GET",
            self.http_uri + "/api/chats/" + urllib.parse.quote(chat_id, safe=""),
            {"Authorization": f"Bearer {self._token}"},
            None,
        )
        if response.status in (403, 404):
            return IsolationProbe(response.status, True, "foreign chat is not readable")
        if response.status == 200:
            return IsolationProbe(response.status, False, "foreign chat was readable")
        if response.status in (401,):
            raise DispatcherDenied("isolation probe was not authenticated")
        raise DispatcherError(f"isolation probe failed with status {response.status}")

    def answer(self, case: ClinicalCase) -> DispatchedAnswer:
        prompt = render_dispatcher_prompt(case)
        chat_id = self.create_chat()
        connection_generation = str(uuid4())
        transport = self._transport_factory()
        frames: List[dict] = []
        text_parts: List[str] = []
        denials: List[str] = []
        audit_event_ids: List[str] = []
        terminal_seen = False
        deadline = self._clock() + self.timeout_seconds
        total_bytes = 0
        try:
            transport.send(json.dumps({
                "type": "register_ui",
                "token": self._token,
                "connection_generation": connection_generation,
            }))
            transport.send(json.dumps({
                "type": "ui_event",
                "action": "chat_message",
                "submission_id": str(uuid4()),
                "request_generation": str(uuid4()),
                "connection_generation": connection_generation,
                "payload": {"message": prompt, "chat_id": chat_id},
            }))
            while len(frames) < self.max_frames:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise DispatcherTimeout()
                raw = transport.recv(remaining)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="strict")
                if not isinstance(raw, str):
                    raise DispatcherError("dispatcher returned a non-text frame")
                total_bytes += len(raw.encode("utf-8"))
                if len(raw.encode("utf-8")) > self.max_message_bytes:
                    raise DispatcherError("dispatcher frame exceeds the size bound")
                if total_bytes > MAX_RESPONSE_BYTES:
                    raise DispatcherError("dispatcher response exceeds the size bound")
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise DispatcherError("dispatcher returned invalid JSON") from exc
                if not isinstance(frame, Mapping):
                    raise DispatcherError("dispatcher frame is not an object")
                frames.append(dict(frame))
                _collect_text(frame, text_parts)
                _collect_denials(frame, denials)
                _collect_audit_ids(frame, audit_event_ids)
                if _denied(frame):
                    raise DispatcherDenied(_denial_text(frame))
                if _terminal(frame):
                    status = str(frame.get("state") or frame.get("status") or "completed")
                    if status.lower() in ("failed", "denied", "cancelled", "error"):
                        raise DispatcherDenied(f"dispatcher terminal state {status!r}")
                    terminal_seen = True
                    break
        finally:
            transport.close()
        if not terminal_seen:
            raise DispatcherError("dispatcher ended without a terminal frame")
        text = "\n".join(part for part in _dedupe(text_parts) if part).strip()
        if not text:
            raise DispatcherError(
                "dispatcher completed without visible answer text",
                code=FailureCode.MALFORMED_ANSWER,
            )
        if len(text) > MAX_ANSWER_CHARS:
            raise DispatcherError("dispatcher answer exceeds the size bound")
        return DispatchedAnswer(
            text=text,
            chat_id=chat_id,
            frame_types=tuple(frame.get("type", "") for frame in frames),
            tool_denials=tuple(_dedupe(denials)),
            audit_event_ids=tuple(_dedupe(audit_event_ids)),
        )

    def _default_transport(self) -> WebsocketTransport:
        from websockets.sync.client import connect

        return _SyncWebsocketTransport(
            connect(
                self.ws_uri,
                open_timeout=self.timeout_seconds,
                close_timeout=2,
                max_size=self.max_message_bytes,
            )
        )


class _SyncWebsocketTransport:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def send(self, text: str) -> None:
        self._connection.send(text)

    def recv(self, timeout: float) -> str:
        from websockets.exceptions import ConnectionClosed

        try:
            return self._connection.recv(timeout=timeout)
        except TimeoutError as exc:
            raise DispatcherTimeout() from exc
        except ConnectionClosed as exc:
            raise DispatcherError("dispatcher closed the connection") from exc

    def close(self) -> None:
        try:
            self._connection.close()
        except Exception:
            pass


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body


def _http_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
) -> HttpResponse:
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return _Response(response.status, response.read(MAX_MESSAGE_BYTES + 1))
    except urllib.error.HTTPError as exc:
        return _Response(exc.code, exc.read(MAX_MESSAGE_BYTES + 1))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DispatcherError(
            "dispatcher HTTP request failed", code=FailureCode.TIMEOUT, retryable=True
        ) from exc


def _bounded_json(response: HttpResponse) -> object:
    if len(response.body) > MAX_MESSAGE_BYTES:
        raise DispatcherError("dispatcher HTTP response exceeds the size bound")
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatcherError("dispatcher HTTP response is not valid JSON") from exc


def _terminal(frame: Mapping[str, object]) -> bool:
    if frame.get("type") in TERMINAL_TYPES:
        return True
    if frame.get("type") == "operation_status" and frame.get("terminal") is True:
        return True
    if frame.get("type") == "chat_status":
        return str(frame.get("status") or "").lower() in (
            "done", "completed", "failed", "denied", "cancelled", "error"
        )
    return False


def _denied(frame: Mapping[str, object]) -> bool:
    return frame.get("type") in ("error", "auth_error", "submission_rejected")


def _denial_text(frame: Mapping[str, object]) -> str:
    for key in ("message", "detail", "error"):
        value = frame.get(key)
        if isinstance(value, str) and value.strip():
            return value[:500]
    return "dispatcher refused the turn"


def _collect_text(frame: Mapping[str, object], parts: List[str]) -> None:
    frame_type = frame.get("type")
    target = frame.get("target")
    if frame_type in ("ui_render", "ui_update", "ui_append"):
        if target in CHAT_TARGETS:
            before = len(parts)
            _extract_components(frame.get("components"), parts)
            html = frame.get("html")
            if len(parts) == before and isinstance(html, str):
                stripped = _HTML_TAG.sub(" ", html)
                if stripped.strip():
                    parts.append(" ".join(stripped.split()))
        return
    if frame_type in ASSISTANT_TEXT_TYPES:
        role = frame.get("role")
        if role not in (None, "assistant"):
            return
        for key in _TEXT_FIELDS:
            value = frame.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())


def _extract_components(components: object, parts: List[str]) -> None:
    if isinstance(components, str):
        if components.strip():
            parts.append(components.strip())
        return
    if isinstance(components, Mapping):
        component_type = components.get("type")
        if component_type in ("text", "card", "alert", "markdown", "notification"):
            for key in _TEXT_FIELDS:
                value = components.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        for key in ("components", "children", "items"):
            _extract_components(components.get(key), parts)
        return
    if isinstance(components, Iterable):
        for component in components:
            _extract_components(component, parts)


def _collect_denials(frame: Mapping[str, object], denials: List[str]) -> None:
    denials.extend(_frame_denials(frame))


def _collect_audit_ids(frame: Mapping[str, object], identifiers: List[str]) -> None:
    if frame.get("type") != "audit_append":
        return
    event = frame.get("event")
    if isinstance(event, Mapping):
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id.strip():
            identifiers.append(event_id)


def _frame_denials(frame: Mapping[str, object]) -> List[str]:
    frame_type = frame.get("type")
    if frame_type == "error":
        parts: List[str] = []
        for key in _TEXT_FIELDS:
            value = frame.get(key)
            if isinstance(value, str):
                parts.append(value)
        text = " ".join(parts).casefold()
    elif frame_type in ("ui_render", "ui_update", "ui_append"):
        parts = []
        _alert_components(frame.get("components"), parts)
        text = " ".join(parts).casefold()
    else:
        return []
    return [marker for marker in _DENIAL_MARKERS if marker in text]


def _alert_components(components: object, parts: List[str]) -> None:
    if isinstance(components, Mapping):
        if components.get("type") == "alert":
            for key in _TEXT_FIELDS:
                value = components.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        for key in ("components", "children", "items"):
            _alert_components(components.get(key), parts)
    elif isinstance(components, Iterable) and not isinstance(components, str):
        for component in components:
            _alert_components(component, parts)


def _dedupe(values: Sequence[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


class DispatcherProducer:
    source = AnswerSource.ASTRAL

    def __init__(self, adapter: DispatcherAdapter) -> None:
        self.adapter = adapter
        self.identity = adapter.identity

    def produce(self, case: ClinicalCase) -> CaseAnswer:
        answer = self.adapter.answer(case)
        return CaseAnswer(
            case_id=case.case_id,
            source=self.source,
            text=answer.text,
            model_identity=self.identity,
            tool_denials=answer.tool_denials,
            audit_event_ids=answer.audit_event_ids,
        )


__all__ = [
    "DispatchedAnswer",
    "DispatcherAdapter",
    "DispatcherDenied",
    "DispatcherError",
    "DispatcherProducer",
    "DispatcherTimeout",
    "IsolationProbe",
    "WebSocketDispatcher",
    "render_dispatcher_prompt",
]