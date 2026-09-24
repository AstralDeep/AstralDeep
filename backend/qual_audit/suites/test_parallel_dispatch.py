"""Benchmarks that asyncio.gather parallel dispatch beats sequential dispatch using real
MCP calls to the weather, general, and medical agent servers; writes results to a
sidecar JSON file for latex_export.py's console summary.
"""

import asyncio
import json
import os
import tempfile
import time
from typing import List, Tuple

import pytest

from shared.protocol import MCPRequest, MCPResponse

_BENCHMARK_FILE = os.path.join(tempfile.gettempdir(), "astral_parallel_dispatch_bench.json")


def _write_benchmark(key: str, data: dict):
    existing = {}
    if os.path.exists(_BENCHMARK_FILE):
        with open(_BENCHMARK_FILE, "r") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                pass
    existing[key] = data
    with open(_BENCHMARK_FILE, "w") as f:
        json.dump(existing, f, indent=2)

from agents.weather.mcp_server import MCPServer as WeatherMCPServer  # noqa: E402
from agents.general.mcp_server import MCPServer as GeneralMCPServer  # noqa: E402
from agents.medical.mcp_server import MCPServer as MedicalMCPServer  # noqa: E402


def _build_request(tool_name: str, arguments: dict, request_id: str = "bench") -> MCPRequest:
    return MCPRequest(
        request_id=request_id,
        method="tools/call",
        params={"name": tool_name, "arguments": arguments},
    )


AGENT_CALLS: List[Tuple] = [
    (WeatherMCPServer(), "get_current_weather", {"city": "New York"}, "weather_agent"),
    (GeneralMCPServer(), "get_system_status", {}, "general_agent"),
    (MedicalMCPServer(), "search_patients", {"min_age": 30, "max_age": 60, "condition": "diabetes"}, "medical_agent"),
]


def _invoke_tool(server, tool_name: str, arguments: dict) -> MCPResponse:
    request = _build_request(tool_name, arguments, request_id=f"bench_{tool_name}")
    return server.process_request(request)


async def _invoke_tool_async(server, tool_name: str, arguments: dict) -> MCPResponse:
    return await asyncio.to_thread(_invoke_tool, server, tool_name, arguments)


class TestParallelDispatch:
    @pytest.mark.asyncio
    async def test_sequential_vs_parallel_latency(self):
        sequential_start = time.perf_counter()
        sequential_results = []
        for server, tool, args, _label in AGENT_CALLS:
            result = await _invoke_tool_async(server, tool, args)
            sequential_results.append(result)
        sequential_ms = (time.perf_counter() - sequential_start) * 1000

        parallel_start = time.perf_counter()
        parallel_results = await asyncio.gather(
            *[_invoke_tool_async(server, tool, args) for server, tool, args, _label in AGENT_CALLS]
        )
        parallel_ms = (time.perf_counter() - parallel_start) * 1000

        speedup = sequential_ms / parallel_ms
        _write_benchmark("latency", {
            "sequential_ms": round(sequential_ms, 1),
            "parallel_ms": round(parallel_ms, 1),
            "speedup": round(speedup, 2),
            "agent_count": len(AGENT_CALLS),
        })

        assert parallel_ms < sequential_ms, (
            f"Parallel ({parallel_ms:.1f}ms) should be faster than "
            f"sequential ({sequential_ms:.1f}ms)"
        )

        assert len(sequential_results) == len(list(parallel_results))

    @pytest.mark.asyncio
    async def test_parallel_dispatch_correctness(self):
        results = await asyncio.gather(
            *[_invoke_tool_async(server, tool, args) for server, tool, args, _label in AGENT_CALLS]
        )

        assert len(results) == len(AGENT_CALLS)

        for i, ((_server, _tool, _args, label), response) in enumerate(zip(AGENT_CALLS, results)):
            assert isinstance(response, MCPResponse), (
                f"Result {i} ({label}) is not an MCPResponse"
            )
            assert response.error is None, (
                f"Result {i} ({label}) returned error: {response.error}"
            )
            assert response.result is not None or response.ui_components is not None, (
                f"Result {i} ({label}) has no result data"
            )

    @pytest.mark.asyncio
    async def test_parallel_speedup_factor(self):
        n_trials = 3
        speedups = []

        for _ in range(n_trials):
            seq_start = time.perf_counter()
            for server, tool, args, _label in AGENT_CALLS:
                await _invoke_tool_async(server, tool, args)
            seq_ms = (time.perf_counter() - seq_start) * 1000

            par_start = time.perf_counter()
            await asyncio.gather(
                *[_invoke_tool_async(s, t, a) for s, t, a, _ in AGENT_CALLS]
            )
            par_ms = (time.perf_counter() - par_start) * 1000

            speedups.append(seq_ms / par_ms)

        avg_speedup = sum(speedups) / len(speedups)
        _write_benchmark("speedup_trials", {
            "n_trials": n_trials,
            "avg_speedup": round(avg_speedup, 2),
            "individual": [round(s, 2) for s in speedups],
        })

        assert avg_speedup > 1.0, (
            f"Average speedup {avg_speedup:.2f}x should exceed 1.0x"
        )
