"""A single action's private context at the ordinary dispatch boundary.

The runner supplies durable admission/reservation/observation callbacks. Neither
model arguments nor socket messages can construct this capability. A physical
attempt consumes it once, including retries, fallback transports and nested calls.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


class DispatchDenied(PermissionError):
    """Bounded safe reason; no source text or credentials in exception values."""


_CURRENT: ContextVar[PersistentDispatchContext | None] = ContextVar(
    "persistent_assignment_dispatch", default=None,
)


def current_dispatch() -> PersistentDispatchContext | None:
    return _CURRENT.get()


@contextmanager
def bind_dispatch(context: PersistentDispatchContext) -> Iterator[None]:
    if _CURRENT.get() is not None:
        raise DispatchDenied("assignment_nested_dispatch_denied")
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


@dataclass(slots=True)
class PersistentDispatchContext:
    owner_id: str
    kind: str
    agent_id: str | None
    tool_name: str | None
    arguments: dict[str, Any]
    timeout_seconds: float
    max_input_bytes: int
    max_output_tokens: int
    authorize: Callable[[], Awaitable[None]]
    start: Callable[[], Awaitable[Any]]
    observe: Callable[[Any, str, Any], Awaitable[None]]
    remote_marker: str | None = None
    _consumed: bool = field(default=False, init=False)
    _arguments_json: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        if (self.kind not in {"tool", "model"} or not self.owner_id
                or not 0 < self.timeout_seconds <= 120
                or not 1 <= self.max_input_bytes <= 262144
                or not 1 <= self.max_output_tokens <= 8192):
            raise ValueError("invalid persistent dispatch bounds")
        self._arguments_json = canonical(self.arguments)

    @property
    def consumed(self) -> bool:
        return self._consumed

    async def validate_tool(self, owner: str, agent: str, tool: str,
                            arguments: dict[str, Any]) -> None:
        arguments = dict(arguments)
        if (self.remote_marker is not None
                and arguments.pop("_remote_op_proposal_id", None) != self.remote_marker):
            raise DispatchDenied("assignment_confirmation_binding_changed")
        if (self.kind != "tool" or self._consumed or owner != self.owner_id
                or agent != self.agent_id or tool != self.tool_name
                or canonical(arguments) != self._arguments_json):
            raise DispatchDenied("assignment_action_binding_changed")
        await self.authorize()

    async def invoke_tool(self, invoke: Callable[[], Awaitable[Any]]) -> Any:
        if self.kind != "tool":
            raise DispatchDenied("assignment_unreserved_tool_call")
        return await self._invoke(invoke)

    async def invoke_model(self, invoke: Callable[[], Awaitable[Any]],
                           kwargs: dict[str, Any]) -> Any:
        if self.kind != "model":
            raise DispatchDenied("assignment_unreserved_model_call")
        # UTF-8 bytes plus framing are a conservative upper bound for text-only
        # input tokens. Images/audio and unknown provider extensions are denied.
        messages = kwargs.get("messages", [])
        if (len(canonical(messages).encode("utf-8")) > self.max_input_bytes
                or any(not isinstance(m, dict) or not isinstance(m.get("content"), str)
                       for m in messages)):
            raise DispatchDenied("assignment_model_input_limit")
        kwargs["max_completion_tokens"] = self.max_output_tokens
        return await self._invoke(invoke)

    async def _invoke(self, invoke: Callable[[], Awaitable[Any]]) -> Any:
        if self._consumed:
            raise DispatchDenied("assignment_attempt_already_started")
        # Set before awaiting: inherited concurrent/nested dispatches cannot race
        # this one-time capability. A refused attempt is recreated only from the
        # durable ledger, never by resetting an in-memory boolean.
        self._consumed = True
        await self.authorize()
        permit = await self.start()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await invoke()
        except BaseException:
            # A cancelled thread/remote request can still finish. Keep its full
            # reservation and immutable uncertain receipt; never infer no-send.
            await asyncio.shield(self.observe(permit, "uncertain", None))
            raise
        await asyncio.shield(self.observe(permit, "succeeded", result))
        return result
