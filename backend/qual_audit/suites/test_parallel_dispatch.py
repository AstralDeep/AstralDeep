"""Benchmarks sequential and parallel batches through the real weather, general and
medical MCP servers, warming geocoding before timing three requests per agent.
Writes response-validated measurements for latex_export.py's console summary.
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

BENCHMARK_CALLS = AGENT_CALLS * 3


def _invoke_tool(server, tool_name: str, arguments: dict) -> MCPResponse:
    request = _build_request(tool_name, arguments, request_id=f"bench_{tool_name}")
    return server.process_request(request)


async def _invoke_tool_async(server, tool_name: str, arguments: dict) -> MCPResponse:
    return await asyncio.to_thread(_invoke_tool, server, tool_name, arguments)


def _require_success(results, calls):
    assert len(results) == len(calls)
    for (_, _, _, label), response in zip(calls, results):
        assert isinstance(response, MCPResponse), label
        assert response.error is None, f"{label}: {response.error}"
        assert response.result is not None or response.ui_components is not None, label


async def _warm_agents():
    results = await asyncio.gather(
        *[_invoke_tool_async(server, tool, args) for server, tool, args, _ in AGENT_CALLS]
    )
    _require_success(results, AGENT_CALLS)


async def _measure_batch(*, parallel):
    start = time.perf_counter()
    if parallel:
        results = await asyncio.gather(
            *[_invoke_tool_async(server, tool, args) for server, tool, args, _ in BENCHMARK_CALLS]
        )
    else:
        results = [
            await _invoke_tool_async(server, tool, args)
            for server, tool, args, _ in BENCHMARK_CALLS
        ]
    elapsed_ms = (time.perf_counter() - start) * 1000
    _require_success(results, BENCHMARK_CALLS)
    return elapsed_ms, results


class TestParallelDispatch:
    @pytest.mark.asyncio
    async def test_sequential_vs_parallel_latency(self):
        await _warm_agents()
        sequential_ms, sequential_results = await _measure_batch(parallel=False)
        parallel_ms, parallel_results = await _measure_batch(parallel=True)

        speedup = sequential_ms / parallel_ms
        _write_benchmark("latency", {
            "sequential_ms": round(sequential_ms, 1),
            "parallel_ms": round(parallel_ms, 1),
            "speedup": round(speedup, 2),
            "agent_count": len(AGENT_CALLS),
            "request_count": len(BENCHMARK_CALLS),
            "requests_per_agent": 3,
        })

        assert parallel_ms < sequential_ms, (
            f"Parallel ({parallel_ms:.1f}ms) should be faster than "
            f"sequential ({sequential_ms:.1f}ms)"
        )
        assert len(sequential_results) == len(parallel_results)

    @pytest.mark.asyncio
    async def test_parallel_dispatch_correctness(self):
        results = await asyncio.gather(
            *[_invoke_tool_async(server, tool, args) for server, tool, args, _ in AGENT_CALLS]
        )
        _require_success(results, AGENT_CALLS)

    @pytest.mark.asyncio
    async def test_parallel_speedup_factor(self):
        await _warm_agents()
        n_trials = 3
        speedups = []
        for trial in range(n_trials):
            if trial % 2:
                par_ms, _ = await _measure_batch(parallel=True)
                seq_ms, _ = await _measure_batch(parallel=False)
            else:
                seq_ms, _ = await _measure_batch(parallel=False)
                par_ms, _ = await _measure_batch(parallel=True)
            speedups.append(seq_ms / par_ms)

        avg_speedup = sum(speedups) / len(speedups)
        _write_benchmark("speedup_trials", {
            "n_trials": n_trials,
            "avg_speedup": round(avg_speedup, 2),
            "individual": [round(s, 2) for s in speedups],
            "request_count": len(BENCHMARK_CALLS),
            "requests_per_agent": 3,
        })

        assert avg_speedup > 1.0, (
            f"Average speedup {avg_speedup:.2f}x should exceed 1.0x"
        )
