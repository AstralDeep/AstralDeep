"""Drives one clinical prompt through Astral's ordinary authenticated WebSocket
dispatcher (register, chat, terminal frames) with bounded frames/bytes/time, and offers
owner-isolation probes over the REST chat route; it never fabricates an answer or bypasses
identity, tool permission, or audit gates.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import ssl
from dataclasses import dataclass
from typing import Callable, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple
from uuid import uuid4

from clinical_diagnostic.models import AnswerSource, CaseAnswer, ClinicalCase, FailureCode
from clinical_diagnostic.provider import ProducerError
from clinical_diagnostic.budget import (
    BudgetExhausted, DeadlineControl, cancellation_event, current_control, operation_control, use_control,
)
from clinical_diagnostic.execution import supervise
from clinical_diagnostic.identity import public_url

MAX_FRAMES = 2000
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ANSWER_CHARS = 64 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 20_000
_TEXT_FIELDS = ("text", "content", "label", "title", "message")
_ANSWER_TEXT_FIELDS = {"text": ("content",), "card": ("title",),
                       "alert": ("title", "message"), "container": ()}
_DENIAL_MARKERS = (
    "restricted for this agent",
    "permission denied",
    "not authorized",
    "outside this unattended task",
    "system-blocked",
)


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
        model_observation=None,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("dispatcher requires an authenticated bearer token")
        for name, value in (("ws_uri", ws_uri), ("http_uri", http_uri)):
            public_url(value, ("ws", "wss") if name == "ws_uri" else ("http", "https"))
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
        self._concrete = transport_factory is None and http is None and clock is None
        self.model_observation = model_observation

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
        parent = current_control()
        control = (operation_control(self.timeout_seconds) if parent is not None else
                   DeadlineControl(self._clock() + self.timeout_seconds, cancellation_event(), clock=self._clock))
        if self._concrete:
            return supervise("dispatcher", {"config": {
                "token": self._token, "ws_uri": self.ws_uri, "http_uri": self.http_uri,
                "identity": self.identity, "timeout_seconds": self.timeout_seconds,
                "max_frames": self.max_frames, "max_message_bytes": self.max_message_bytes,
            }, "case": case}, control)
        with use_control(control):
            try:
                return self._answer(case)
            except BudgetExhausted as exc:
                if parent is None:
                    raise DispatcherTimeout() from exc
                raise

    def _answer(self, case: ClinicalCase) -> DispatchedAnswer:
        from websockets.exceptions import WebSocketException

        try:
            return self._collect_answer(case)
        except TimeoutError as exc:
            raise DispatcherTimeout() from exc
        except (OSError, ssl.SSLError, WebSocketException) as exc:
            raise DispatcherError("dispatcher connection failed") from exc

    def _collect_answer(self, case: ClinicalCase) -> DispatchedAnswer:
        control = current_control()
        control.remaining_seconds()
        prompt = render_dispatcher_prompt(case)
        chat_id = self.create_chat()
        control.remaining_seconds()
        self._verify_empty_chat(chat_id)
        control.remaining_seconds()
        connection_generation = str(uuid4())
        request_generation = str(uuid4())
        submission_id = str(uuid4())
        transport = self._transport_factory()
        frames: List[dict] = []
        text_parts: List[str] = []
        denials: List[str] = []
        audit_event_ids: List[str] = []
        terminal_seen = False
        snapshot_seen = False
        submitted = False
        total_bytes = 0
        try:
            control.remaining_seconds()
            transport.send(json.dumps({
                "type": "register_ui",
                "token": self._token,
                "connection_generation": connection_generation,
            }))
            while len(frames) < self.max_frames:
                remaining = control.remaining_seconds()
                raw = transport.recv(remaining)
                control.remaining_seconds()
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8", errors="strict")
                    except UnicodeDecodeError as exc:
                        raise DispatcherError("dispatcher frame is not valid UTF-8") from exc
                if not isinstance(raw, str):
                    raise DispatcherError("dispatcher returned a non-text frame")
                total_bytes += len(raw.encode("utf-8"))
                if len(raw.encode("utf-8")) > self.max_message_bytes:
                    raise DispatcherError("dispatcher frame exceeds the size bound")
                if total_bytes > MAX_RESPONSE_BYTES:
                    raise DispatcherError("dispatcher response exceeds the size bound")
                try:
                    frame = _load_json(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise DispatcherError("dispatcher returned invalid JSON") from exc
                if not isinstance(frame, Mapping):
                    raise DispatcherError("dispatcher frame is not an object")
                frames.append(dict(frame))
                if frame.get("type") in ("auth_error", "auth_required"):
                    raise DispatcherDenied()
                if not submitted:
                    if frame.get("type") != "rote_config":
                        continue
                    if len(frames) >= self.max_frames or total_bytes >= MAX_RESPONSE_BYTES:
                        raise DispatcherError("dispatcher registration exhausted the response bound")
                    control.remaining_seconds()
                    transport.send(json.dumps({
                        "type": "ui_event",
                        "action": "chat_message",
                        "submission_id": submission_id,
                        "request_generation": request_generation,
                        "connection_generation": connection_generation,
                        "payload": {"message": prompt, "chat_id": chat_id},
                    }))
                    submitted = True
                    continue
                if frame.get("type") == "submission_rejected" and frame.get("submission_id") == submission_id:
                    raise DispatcherDenied(_denial_text(frame))
                event = frame.get("event")
                if isinstance(event, Mapping) and event.get("conversation_id") == chat_id:
                    _collect_audit_ids(frame, audit_event_ids)
                if not _matches_turn(frame, chat_id, connection_generation, request_generation):
                    continue
                _collect_audit_ids(frame, audit_event_ids)
                _collect_denials(frame, denials)
                if frame.get("type") == "error":
                    raise DispatcherDenied(_denial_text(frame))
                if frame.get("type") == "conversation_snapshot" and frame.get("snapshot_purpose") == "commit":
                    if snapshot_seen:
                        raise DispatcherError("dispatcher returned ambiguous committed answers")
                    text_parts = _committed_text(frame, prompt, chat_id)
                    _collect_denials({"type": "ui_render", "components": frame["canvas"]["components"]}, denials)
                    snapshot_seen = True
                if _terminal(frame):
                    status = frame.get("state")
                    if status != "completed":
                        raise DispatcherDenied("dispatcher did not complete the current operation")
                    if not snapshot_seen:
                        raise DispatcherError("dispatcher completed without a committed answer")
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

    def _verify_empty_chat(self, chat_id: str) -> None:
        base = self.http_uri + "/api/chats/" + urllib.parse.quote(chat_id, safe="")
        headers = {"Authorization": f"Bearer {self._token}"}
        for url, key in ((base, "chat"), (base + "/components", "components")):
            current_control().remaining_seconds()
            response = self._http("GET", url, headers, None)
            if response.status in (401, 403, 404):
                raise DispatcherDenied("new chat ownership verification was denied")
            if response.status != 200:
                raise DispatcherError("new chat verification failed")
            payload = _bounded_json(response)
            value = payload.get(key) if isinstance(payload, Mapping) else None
            if key == "chat":
                if not isinstance(value, Mapping) or value.get("id") != chat_id or value.get("messages") != []:
                    raise DispatcherError("new chat is not an empty owned conversation")
            elif value != []:
                raise DispatcherError("new chat canvas is not empty")

    def _default_transport(self) -> WebsocketTransport:
        from websockets.sync.client import connect

        return _SyncWebsocketTransport(
            connect(
                self.ws_uri,
                open_timeout=(current_control() or operation_control(self.timeout_seconds)).remaining_seconds(),
                close_timeout=0.5,
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
        control = current_control() or operation_control(60)
        with urllib.request.urlopen(request, timeout=control.remaining_seconds(60)) as response:
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
        return _load_json(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatcherError("dispatcher HTTP response is not valid JSON") from exc


def _load_json(text: str) -> object:
    try:
        value = json.loads(text)
    except RecursionError as exc:
        raise DispatcherError("dispatcher JSON exceeds the nesting bound") from exc
    stack = [(value, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > MAX_JSON_DEPTH or count > MAX_JSON_NODES:
            raise DispatcherError("dispatcher JSON exceeds the nesting or node bound")
        children = node.values() if isinstance(node, Mapping) else node if isinstance(node, list) else ()
        stack.extend((child, depth + 1) for child in children)
    return value


def _matches_turn(frame: Mapping[str, object], chat_id: str, connection: str, request: str) -> bool:
    return (frame.get("chat_id") == chat_id
            and frame.get("connection_generation") == connection
            and frame.get("request_generation") == request)


def _terminal(frame: Mapping[str, object]) -> bool:
    return (frame.get("type") == "operation_status" and frame.get("action") == "chat_message"
            and frame.get("terminal") is True)


def _denial_text(frame: Mapping[str, object]) -> str:
    for key in ("message", "detail", "error"):
        value = frame.get(key)
        if isinstance(value, str) and value.strip():
            return value[:500]
    return "dispatcher refused the turn"


def _committed_text(frame: Mapping[str, object], prompt: str, chat_id: str) -> List[str]:
    revision = frame.get("render_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise DispatcherError("committed answer has no valid render revision")
    transcript = frame.get("transcript")
    canvas = frame.get("canvas")
    if not isinstance(transcript, list) or not transcript or not isinstance(canvas, Mapping):
        raise DispatcherError("committed answer has no transcript or canvas")
    if canvas.get("target") != "canvas" or not isinstance(canvas.get("components"), list):
        raise DispatcherError("committed answer has an invalid canvas")
    if any(not isinstance(message, Mapping) for message in transcript):
        raise DispatcherError("committed transcript contains an invalid message")
    users = [message for message in transcript if message.get("role") == "user"]
    if len(users) != 1 or transcript[0] is not users[0] or users[0].get("parts") != [{"type": "text", "text": prompt}]:
        raise DispatcherError("committed transcript does not match the single current request")
    final = transcript[-1]
    if final.get("role") != "assistant" or not isinstance(final.get("parts"), list) or not final["parts"]:
        raise DispatcherError("committed transcript has no final assistant answer")
    parts: List[str] = []
    for part in final["parts"]:
        if not isinstance(part, Mapping):
            raise DispatcherError("committed answer part is invalid")
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"].strip())
        elif kind == "components" and isinstance(part.get("components"), list):
            _extract_components(part["components"], parts, chat_id)
        else:
            raise DispatcherError("committed answer part is unsupported")
    _extract_components(canvas["components"], parts, chat_id)
    if len("\n".join(parts)) > MAX_ANSWER_CHARS:
        raise DispatcherError("dispatcher answer exceeds the size bound")
    return parts


def _extract_components(components: object, parts: List[str], chat_id: str) -> None:
    if isinstance(components, list):
        for component in components:
            _extract_components(component, parts, chat_id)
        return
    if not isinstance(components, Mapping):
        raise DispatcherError("committed component must be an object")
    if any(key in components for key in ("_source_agent", "_source_tool", "_source_correlation_id")):
        return
    if (components.get("intermediate") is True or components.get("preview") is True
            or components.get("phase") in ("intermediate", "preview", "queued", "running")):
        return
    kind = components.get("type")
    if kind in ("button", "icon", "spacer", "divider"):
        return
    if not isinstance(kind, str) or kind not in _ANSWER_TEXT_FIELDS:
        raise DispatcherError("committed answer component is unsupported")
    if components.get("hidden") is True or components.get("visible") is False:
        return
    title = components.get("title", "")
    for key in ("id", "component_id"):
        identity = components.get(key)
        if isinstance(identity, str) and identity.startswith("doc_"):
            if not isinstance(title, str):
                raise DispatcherError("promoted document title is invalid")
            digest = hashlib.sha1(f"{chat_id}|{title}".encode("utf-8")).hexdigest()[:12]
            if kind != "card" or identity != f"doc_{digest}":
                raise DispatcherError("promoted document identity does not match this chat")
    for key in _TEXT_FIELDS:
        value = components.get(key)
        if isinstance(value, str):
            if key in _ANSWER_TEXT_FIELDS[kind] and value.strip():
                parts.append(value.strip())
        elif value is not None:
            if key != "content" or kind not in ("card", "container") or not isinstance(value, list):
                raise DispatcherError("committed component text field is invalid")
    if kind in ("card", "container"):
        children = components.get("children") or components.get("content") or []
        if not isinstance(children, list):
            raise DispatcherError("committed component children are invalid")
        _extract_components(children, parts, chat_id)


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
        observation = getattr(adapter, "model_observation", None)
        self.identity = observation.model if observation is not None else "unknown"
        self.config = self

    def to_dict(self):
        observation = getattr(self.adapter, "model_observation", None)
        return {"adapter": self.adapter.identity,
                "http_uri": getattr(self.adapter, "http_uri", None),
                "ws_uri": getattr(self.adapter, "ws_uri", None),
                "model": observation.to_dict() if observation is not None else {"status": "unknown"}}

    def produce(self, case: ClinicalCase) -> CaseAnswer:
        answer = self.adapter.answer(case)
        return CaseAnswer(
            case_id=case.case_id,
            source=self.source,
            text=answer.text,
            model_identity=getattr(answer, "model_identity", "unknown"),
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
