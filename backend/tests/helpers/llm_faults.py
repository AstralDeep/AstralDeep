"""Scripted fake model provider and a bare orchestrator stub for model-call reliability tests.
Tests build a script of steps, hand it to FaultyCompletions, and drive Orchestrator._call_llm against it.
"""

from __future__ import annotations

import json
import threading
import time
import types
from typing import Any, Callable, Dict, List, Optional

from llm_config.types import CredentialSource, LLMUnavailable
from orchestrator.orchestrator import Orchestrator

BLOCK_LIMIT_SECONDS = 5.0


def _chunk(delta: types.SimpleNamespace, finish_reason: Optional[str] = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta, finish_reason=finish_reason)], usage=None)


def content_chunk(text: str) -> types.SimpleNamespace:
    return _chunk(types.SimpleNamespace(role=None, content=text, tool_calls=None))


def tool_chunk(index: int = 0, call_id: Optional[str] = None, name: Optional[str] = None,
               arguments: Optional[str] = None) -> types.SimpleNamespace:
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    tc = types.SimpleNamespace(index=index, id=call_id, function=fn)
    return _chunk(types.SimpleNamespace(role=None, content=None, tool_calls=[tc]))


def finish_chunk(reason: str = "stop") -> types.SimpleNamespace:
    return _chunk(types.SimpleNamespace(role=None, content=None, tool_calls=None), reason)


def empty_chunk() -> types.SimpleNamespace:
    return types.SimpleNamespace(choices=[], usage=None)


class StatusError(Exception):
    def __init__(self, status_code: int, retry_after: Optional[float] = None):
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        headers = {} if retry_after is None else {"retry-after": str(int(retry_after)
                                                                      if float(retry_after).is_integer()
                                                                      else retry_after)}
        self.response = types.SimpleNamespace(status_code=status_code, headers=headers)


def _completion(content: Optional[str], tool_calls: Any = None) -> types.SimpleNamespace:
    message = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message, finish_reason="stop")],
        usage=types.SimpleNamespace(total_tokens=10))


def _wait(release: threading.Event) -> None:
    if not release.wait(BLOCK_LIMIT_SECONDS):
        raise TimeoutError("scripted stall was never released")


class _Step:
    def __init__(self, run: Callable[[Dict[str, Any], threading.Event], Any]):
        self.run = run


def reply(text: str) -> _Step:
    def run(kwargs, release):
        if not kwargs.get("stream"):
            return _completion(text)
        return iter([content_chunk(text), finish_chunk("stop")])
    return _Step(run)


def stream(*chunks: Any, finish: str = "stop") -> _Step:
    def run(kwargs, release):
        if not kwargs.get("stream"):
            text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
            return _completion(text)
        return iter([*chunks, finish_chunk(finish)])
    return _Step(run)


def fail(error: BaseException) -> _Step:
    def run(kwargs, release):
        raise error
    return _Step(run)


def status(code: int, retry_after: Optional[float] = None) -> _Step:
    return fail(StatusError(code, retry_after))


def stall(after: int = 0, *chunks: Any) -> _Step:
    def run(kwargs, release):
        def gen():
            for chunk in chunks[:after]:
                yield chunk
            _wait(release)
        if not kwargs.get("stream"):
            _wait(release)
        return gen()
    return _Step(run)


def drop(after: int, *chunks: Any) -> _Step:
    def run(kwargs, release):
        def gen():
            for chunk in chunks[:after]:
                yield chunk
            raise ConnectionError("dropped")
        if not kwargs.get("stream"):
            raise ConnectionError("dropped")
        return gen()
    return _Step(run)


def heartbeat_only() -> _Step:
    def run(kwargs, release):
        def gen():
            deadline = time.monotonic() + BLOCK_LIMIT_SECONDS
            while not release.is_set() and time.monotonic() < deadline:
                yield empty_chunk()
                time.sleep(0.005)
        return gen()
    return _Step(run)


class FaultyCompletions:
    def __init__(self, script: Optional[List[_Step]] = None, *, by_model: Optional[Dict[str, List[_Step]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self.release = threading.Event()
        self._script = list(script or [])
        self._by_model = {name: list(steps) for name, steps in (by_model or {}).items()}
        self._used: Dict[Optional[str], int] = {}
        self._lock = threading.Lock()

    def create(self, **kwargs):
        model = kwargs.get("model")
        key = model if self._by_model else None
        steps = self._by_model[model] if self._by_model else self._script
        with self._lock:
            self.calls.append(kwargs)
            index = min(self._used.get(key, 0), len(steps) - 1)
            self._used[key] = self._used.get(key, 0) + 1
        return steps[index].run(kwargs, self.release)


def bare_orch(completions: FaultyCompletions, *, model: str = "m1", base_url: str = "https://ep/v1") -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch._llm_unsupported_params = {}
    orch.llm_reasoning_effort = None
    orch.audit_recorder = None
    orch._CredentialSource = CredentialSource
    orch._LLMUnavailable = LLMUnavailable
    resolved = types.SimpleNamespace(model=model, base_url=base_url)
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    orch._llm_audit_principals = lambda ws: ("u", "p")

    async def _resolve(ws, **kwargs):
        return (client, CredentialSource.SYSTEM, resolved)

    orch._resolve_llm_client_for = _resolve
    audits: List[Dict[str, Any]] = []
    rendered: List[List[Dict[str, Any]]] = []
    sent: List[Dict[str, Any]] = []

    async def _record(*args, **kwargs):
        audits.append(kwargs)

    async def _noop(*args, **kwargs):
        return None

    async def _render(ws, components, *args, **kwargs):
        rendered.append(components)

    async def _capture(ws, payload):
        sent.append(json.loads(payload))

    orch._record_llm_call = _record
    orch._record_llm_unconfigured = _noop
    orch._emit_llm_usage_report = _noop
    orch.send_ui_render = _render
    orch.rote = types.SimpleNamespace(get_profile=lambda ws: None, adapt=lambda ws, comps: comps)
    orch._safe_send = _capture
    orch._audits = audits
    orch._rendered = rendered
    orch._sent_frames = sent
    return orch


def no_retry_sleep(monkeypatch) -> List[float]:
    delays: List[float] = []

    async def _sleep(seconds, *args, **kwargs):
        delays.append(seconds)

    monkeypatch.setattr("orchestrator.orchestrator.asyncio.sleep", _sleep)
    return delays


def tiny_deadlines(monkeypatch, *, first_token: float = 0.05, idle: float = 0.05, attempt: float = 2.0) -> None:
    from llm_config.deadlines import LLMDeadlines

    monkeypatch.setattr(
        "llm_config.deadlines.deadlines_for",
        lambda base_url: LLMDeadlines(0.05, first_token, idle, attempt))
