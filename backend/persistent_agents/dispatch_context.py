"""One-time-use dispatch capability binding a physical attempt to runner-supplied
admission/reservation callbacks; unconstructable from model output or sockets. Used
throughout execution.py, chat_episode.py, and orchestrator dispatch.
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
    pass


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
    strict_final_arguments: bool = False
    conversation_id: str | None = None
    research_input: Any = field(default=None, repr=False)
    _consumed: bool = field(default=False, init=False)
    _arguments_json: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        if (self.kind not in {"tool", "model"} or not self.owner_id
                or not 0 < self.timeout_seconds <= 120
                or not 1 <= self.max_input_bytes <= 262144
                or not 1 <= self.max_output_tokens <= 8192):
            raise ValueError("invalid persistent dispatch bounds")
        self._arguments_json = canonical(self.arguments)
        if self.research_input is not None:
            from persistent_agents.research_input import ResearchInput, route
            if (type(self.research_input) is not ResearchInput or self.kind != "model"
                    or self.owner_id != self.research_input.owner_id or self.arguments != route()
                    or self.timeout_seconds != 65 or self.max_output_tokens != 1024
                    or not self.strict_final_arguments):
                raise DispatchDenied("assignment_research_binding_changed")

    @property
    def consumed(self) -> bool:
        return self._consumed

    def validate_final_tool_arguments(self, arguments: dict[str, Any] | None) -> None:
        if not self.strict_final_arguments:
            return
        if self.kind != "tool" or not isinstance(arguments, dict):
            raise DispatchDenied("assignment_action_binding_changed")
        if "_credentials" in arguments or "_credentials_encrypted" in arguments:
            credentials = arguments.get("_credentials")
            if (arguments.get("_credentials_encrypted") is not True
                    or not isinstance(credentials, dict) or not credentials
                    or any(not isinstance(key, str) or not key or key.startswith("_")
                           or not isinstance(value, str) or not value
                           for key, value in credentials.items())):
                raise DispatchDenied("assignment_action_binding_changed")
        public = {}
        for key, value in arguments.items():
            if key in {"_credentials", "_credentials_encrypted", "_session_llm_credentials",
                       "_delegation_token", "_cap_job_id"}:
                continue
            if key == "user_id":
                if value != self.owner_id:
                    raise DispatchDenied("assignment_action_binding_changed")
                continue
            if key == "session_id":
                if self.conversation_id is None or value != self.conversation_id:
                    raise DispatchDenied("assignment_action_binding_changed")
                continue
            public[key] = value
        if canonical(public) != self._arguments_json:
            raise DispatchDenied("assignment_action_binding_changed")

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

    async def invoke_tool(self, invoke: Callable[[], Awaitable[Any]], *,
                          final_arguments: dict[str, Any] | None = None) -> Any:
        if self.kind != "tool":
            raise DispatchDenied("assignment_unreserved_tool_call")
        return await self._invoke(invoke, final_arguments=final_arguments)

    async def invoke_model(self, invoke: Callable[[], Awaitable[Any]],
                           kwargs: dict[str, Any]) -> Any:
        if self.kind != "model":
            raise DispatchDenied("assignment_unreserved_model_call")
        if self.research_input is not None:
            self.research_input.assert_body(self.owner_id, kwargs)
            return await self._invoke(invoke, final_arguments=kwargs)
        messages = kwargs.get("messages", [])
        if (len(canonical(messages).encode("utf-8")) > self.max_input_bytes
                or any(not isinstance(m, dict) or not isinstance(m.get("content"), str)
                       for m in messages)):
            raise DispatchDenied("assignment_model_input_limit")
        kwargs["max_completion_tokens"] = self.max_output_tokens
        return await self._invoke(invoke)

    def _validate_final(self, arguments):
        if self.research_input is not None:
            self.research_input.assert_body(self.owner_id, arguments)
        else:
            self.validate_final_tool_arguments(arguments)

    async def _observe_research_once(self, permit, outcome, result):
        observer = asyncio.create_task(self.observe(permit, outcome, result))
        cancelled = False
        while not observer.done():
            try:
                await asyncio.shield(observer)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            return observer.result()
        finally:
            if cancelled:
                raise asyncio.CancelledError

    async def _invoke(self, invoke: Callable[[], Awaitable[Any]], *,
                      final_arguments: dict[str, Any] | None = None) -> Any:
        if self._consumed:
            raise DispatchDenied("assignment_attempt_already_started")
        # Set before await: blocks nested dispatch from reusing this
        self._consumed = True
        self._validate_final(final_arguments)
        await self.authorize()
        self._validate_final(final_arguments)
        permit = await self.start()
        try:
            self._validate_final(final_arguments)
            if self.research_input is not None:
                result = await invoke()
            else:
                async with asyncio.timeout(self.timeout_seconds):
                    result = await invoke()
        except BaseException:
            if self.research_input is not None:
                await self._observe_research_once(permit, "uncertain", None)
            else:
                await asyncio.shield(self.observe(permit, "uncertain", None))
            raise
        if self.research_input is not None:
            await self._observe_research_once(permit, "succeeded", result)
        else:
            await asyncio.shield(self.observe(permit, "succeeded", result))
        return result
