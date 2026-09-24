"""Tests for shared/stream_sdk.py: the @streaming_tool decorator's async-generator
marking and metadata, StreamComponents size validation,
assign_stream_id_to_components, and the StreamCtx smoke behavior.
"""

import asyncio
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.stream_sdk import (
    StreamComponents,
    StreamCtx,
    StreamPayloadError,
    streaming_tool,
    is_streaming_tool,
    get_stream_metadata,
    assign_stream_id_to_components,
    validate_chunk_size,
)


class TestStreamingToolDecorator:
    def test_marks_async_generator(self):
        @streaming_tool(name="t", description="d", input_schema={})
        async def my_tool(args, creds):
            yield StreamComponents(components=[{"type": "metric"}])

        assert is_streaming_tool(my_tool)
        meta = get_stream_metadata(my_tool)
        assert meta is not None
        assert meta["name"] == "t"
        assert meta["uses_ctx"] is False
        assert meta["metadata"]["streamable"] is True
        assert meta["metadata"]["streaming_kind"] == "push"
        assert inspect.isasyncgenfunction(my_tool)

    def test_marks_streamctx_form(self):
        @streaming_tool(name="t", description="d", input_schema={})
        async def my_tool(args, creds, ctx: StreamCtx):
            await ctx.until_cancelled()

        assert is_streaming_tool(my_tool)
        meta = get_stream_metadata(my_tool)
        assert meta["uses_ctx"] is True

    def test_rejects_sync_function(self):
        with pytest.raises(TypeError, match="async"):
            @streaming_tool(name="t", description="d", input_schema={})
            def not_async(args, creds):  # noqa: ARG001
                return None

    def test_rejects_bad_fps_clamp(self):
        with pytest.raises(ValueError, match="fps"):
            @streaming_tool(
                name="t", description="d", input_schema={},
                min_fps=20, max_fps=10,
            )
            async def bad(args, creds):
                yield StreamComponents(components=[])

    def test_rejects_zero_fps(self):
        with pytest.raises(ValueError, match="fps"):
            @streaming_tool(
                name="t", description="d", input_schema={},
                min_fps=0, max_fps=30,
            )
            async def bad(args, creds):
                yield StreamComponents(components=[])

    def test_rejects_excessive_fps(self):
        with pytest.raises(ValueError, match="fps"):
            @streaming_tool(
                name="t", description="d", input_schema={},
                min_fps=5, max_fps=120,
            )
            async def bad(args, creds):
                yield StreamComponents(components=[])

    def test_rejects_negative_max_chunk_bytes(self):
        with pytest.raises(ValueError, match="positive int"):
            @streaming_tool(
                name="t", description="d", input_schema={},
                max_chunk_bytes=-1,
            )
            async def bad(args, creds):
                yield StreamComponents(components=[])


class TestStreamComponents:
    def test_serialized_size_for_simple_payload(self):
        sc = StreamComponents(components=[{"type": "metric", "value": "12C"}])
        assert sc.serialized_size() > 0
        assert sc.serialized_size() < 1024

    def test_validate_chunk_size_passes_when_under_cap(self):
        sc = StreamComponents(components=[{"type": "metric", "value": "12C"}])
        validate_chunk_size(sc, max_chunk_bytes=65536)

    def test_validate_chunk_size_rejects_oversized(self):
        big = StreamComponents(components=[
            {"type": "text", "content": "x" * 200_000},
        ])
        with pytest.raises(StreamPayloadError, match="exceeds"):
            validate_chunk_size(big, max_chunk_bytes=65536)


class TestAssignStreamId:
    def test_overwrites_authors_id(self):
        comps = [{"type": "metric", "id": "i-set-this", "value": "12"}]
        out = assign_stream_id_to_components(comps, "stream-canonical")
        assert out[0]["id"] == "stream-canonical"
        assert comps[0]["id"] == "i-set-this"

    def test_assigns_id_when_missing(self):
        comps = [{"type": "metric", "value": "12"}]
        out = assign_stream_id_to_components(comps, "stream-x")
        assert out[0]["id"] == "stream-x"

    def test_rejects_non_dict_component(self):
        with pytest.raises(StreamPayloadError, match="non-dict"):
            assign_stream_id_to_components(["not a dict"], "s")  # type: ignore

    def test_rejects_component_without_type(self):
        with pytest.raises(StreamPayloadError, match="without a 'type'"):
            assign_stream_id_to_components([{"value": "12"}], "s")

    def test_handles_multiple_components(self):
        comps = [
            {"type": "metric", "value": "1"},
            {"type": "metric", "value": "2"},
        ]
        out = assign_stream_id_to_components(comps, "s")
        assert out[0]["id"] == "s"
        assert out[1]["id"] == "s"


class TestStreamCtx:
    @pytest.mark.asyncio
    async def test_emit_and_drain(self):
        ctx = StreamCtx(stream_id="s1")
        sc = StreamComponents(components=[{"type": "metric", "value": "1"}])
        ctx.emit(sc)
        result = await ctx._drain()
        assert result is sc

    @pytest.mark.asyncio
    async def test_until_cancelled_resolves_on_cancel(self):
        ctx = StreamCtx(stream_id="s1")

        async def waiter():
            await ctx.until_cancelled()
            return "done"

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        ctx._cancel()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == "done"

    @pytest.mark.asyncio
    async def test_emit_after_cancel_is_silent(self):
        ctx = StreamCtx(stream_id="s1")
        ctx._cancel()
        ctx.emit(StreamComponents(components=[{"type": "metric"}]))

    def test_emit_rejects_non_streamcomponents(self):
        # get_event_loop() raises outside async context on 3.12+
        loop = asyncio.new_event_loop()
        try:
            ctx = StreamCtx(stream_id="s1", loop=loop)
            with pytest.raises(StreamPayloadError):
                ctx.emit({"not": "streamcomponents"})  # type: ignore
        finally:
            loop.close()
