"""SDK for authoring streaming MCP tools as async generators or StreamCtx.emit()
callbacks, additive to single-response tools. Used by agents/general/mcp_tools.py and
agents/weather/mcp_tools.py.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional


class StreamPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class StreamComponents:
    components: List[Dict[str, Any]]
    raw: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None
    terminal: bool = False

    def serialized_size(self) -> int:
        return len(json.dumps(asdict(self), default=str))


class StreamCtx:
    def __init__(self, stream_id: str, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.stream_id = stream_id
        self._loop = loop or asyncio.get_event_loop()
        self._queue: asyncio.Queue[Optional[StreamComponents]] = asyncio.Queue()
        self._cancelled = asyncio.Event()

    def emit(self, payload: StreamComponents) -> None:
        if self._cancelled.is_set():
            return
        if not isinstance(payload, StreamComponents):
            raise StreamPayloadError(
                f"ctx.emit expects a StreamComponents, got {type(payload).__name__}"
            )
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            self._queue.put_nowait(payload)
        else:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    async def until_cancelled(self) -> None:
        await self._cancelled.wait()

    def _cancel(self) -> None:
        self._cancelled.set()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover
            pass

    async def _drain(self) -> Optional[StreamComponents]:
        item = await self._queue.get()
        return item


def streaming_tool(
    *,
    name: str,
    description: str,
    input_schema: Dict[str, Any],
    max_fps: int = 30,
    min_fps: int = 5,
    max_chunk_bytes: int = 65536,
    scope: Optional[str] = None,
) -> Callable[[Callable], Callable]:
    if not (1 <= min_fps <= max_fps <= 60):
        raise ValueError(
            f"@streaming_tool: must satisfy 1 <= min_fps <= max_fps <= 60, "
            f"got min_fps={min_fps}, max_fps={max_fps}"
        )
    if not isinstance(max_chunk_bytes, int) or max_chunk_bytes <= 0:
        raise ValueError(
            f"@streaming_tool: max_chunk_bytes must be a positive int, "
            f"got {max_chunk_bytes!r}"
        )

    def decorate(fn: Callable) -> Callable:
        if not asyncio.iscoroutinefunction(fn) and not inspect.isasyncgenfunction(fn):
            raise TypeError(
                f"@streaming_tool: {fn.__name__} must be `async def` "
                f"(either an async generator with `yield`, or an async "
                f"function that takes a StreamCtx parameter)"
            )

        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        uses_ctx = any(
            p.annotation is StreamCtx
            or (p.name == "ctx" and p.annotation is inspect.Parameter.empty)
            for p in params
        )

        fn.__streaming_tool__ = True
        fn.__stream_metadata__ = {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "scope": scope,
            "uses_ctx": uses_ctx,
            "metadata": {
                "streamable": True,
                "streaming_kind": "push",
                "max_fps": max_fps,
                "min_fps": min_fps,
                "max_chunk_bytes": max_chunk_bytes,
            },
        }
        return fn

    return decorate


def is_streaming_tool(fn: Any) -> bool:
    return bool(getattr(fn, "__streaming_tool__", False))


def get_stream_metadata(fn: Any) -> Optional[Dict[str, Any]]:
    return getattr(fn, "__stream_metadata__", None)


def assign_stream_id_to_components(
    components: List[Dict[str, Any]],
    stream_id: str,
) -> List[Dict[str, Any]]:
    out = []
    for c in components:
        if not isinstance(c, dict):
            raise StreamPayloadError(
                f"streaming tool yielded a non-dict component: {type(c).__name__}"
            )
        if "type" not in c:
            raise StreamPayloadError(
                f"streaming tool yielded a component without a 'type' key: {c!r}"
            )
        copy = dict(c)
        copy["id"] = stream_id
        out.append(copy)
    return out


def validate_chunk_size(chunk: StreamComponents, max_chunk_bytes: int) -> None:
    size = chunk.serialized_size()
    if size > max_chunk_bytes:
        raise StreamPayloadError(
            f"streaming tool emitted a {size}-byte chunk, exceeds "
            f"max_chunk_bytes={max_chunk_bytes}"
        )
