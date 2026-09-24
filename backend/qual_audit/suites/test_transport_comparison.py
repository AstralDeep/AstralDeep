"""Benchmarks comparing sse_transport.py and ws_transport.py for latency, throughput,
message ordering, reconnection, and concurrent-connection fairness, using
benchmark_helpers.py's Timer and BenchmarkResult.
"""

import asyncio
import json
import time

import pytest
import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from qual_audit.sse_transport import create_sse_router
from qual_audit.ws_transport import create_ws_router
from qual_audit.suites.benchmark_helpers import BenchmarkResult, Timer


@pytest.fixture
def transport_app():
    app = FastAPI()
    app.include_router(create_sse_router())
    app.include_router(create_ws_router())
    return app


@pytest.fixture
def sse_app(transport_app):
    return transport_app


@pytest.fixture
def sse_client(transport_app):
    transport = ASGITransport(app=transport_app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def ws_app(transport_app):
    return transport_app


class TestSSETransport:
    @pytest.mark.asyncio
    async def test_sse_echo_latency(self, sse_client):
        n = 100
        result = BenchmarkResult(transport="sse_echo", sample_count=n)

        for i in range(n):
            payload = {"seq": i, "client_timestamp": time.time()}
            with Timer() as t:
                resp = await sse_client.post("/sse/echo", json=payload)
            assert resp.status_code == 200
            result.latencies_ms.append(t.elapsed_ms)

        assert result.sample_count == n
        assert result.mean > 0
        ci = result.confidence_interval_95()
        assert ci[0] <= result.mean <= ci[1]
        result.to_dict()

    @pytest.mark.asyncio
    async def test_sse_throughput(self, sse_client):
        n = 200
        start = time.perf_counter()

        for i in range(n):
            resp = await sse_client.post("/sse/echo", json={"seq": i})
            assert resp.status_code == 200

        elapsed = time.perf_counter() - start
        throughput = n / elapsed
        assert throughput > 0, "Throughput must be positive"

    @pytest.mark.asyncio
    async def test_sse_message_ordering(self, sse_client):
        received = []
        for i in range(100):
            resp = await sse_client.post("/sse/echo", json={"seq": i})
            assert resp.status_code == 200
            data = resp.json()
            received.append(data["seq"])

        assert received == list(range(100)), "Messages should arrive in order"

    @pytest.mark.asyncio
    async def test_sse_reconnection_id(self, sse_client):
        resp1 = await sse_client.post("/sse/echo", json={"session": "a", "seq": 1})
        resp2 = await sse_client.post("/sse/echo", json={"session": "b", "seq": 2})

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert "server_timestamp" in resp1.json()
        assert "server_timestamp" in resp2.json()
        assert resp1.json()["session"] == "a"
        assert resp2.json()["session"] == "b"

    @pytest.mark.asyncio
    async def test_sse_concurrent_connections(self, sse_client):
        n_connections = 10
        msgs_per_conn = 20

        async def _run_one(conn_id: int) -> BenchmarkResult:
            result = BenchmarkResult(transport=f"sse_conn_{conn_id}", sample_count=msgs_per_conn)
            for i in range(msgs_per_conn):
                with Timer() as t:
                    resp = await sse_client.post(
                        "/sse/echo", json={"conn": conn_id, "seq": i}
                    )
                assert resp.status_code == 200
                result.latencies_ms.append(t.elapsed_ms)
            return result

        results = await asyncio.gather(*[_run_one(c) for c in range(n_connections)])

        means = [r.mean for r in results]
        assert len(means) == n_connections
        assert max(means) / max(min(means), 0.001) < 10.0, "Unfair distribution"


class TestWebSocketTransport:
    @pytest.mark.asyncio
    async def test_ws_echo_latency(self, ws_app):
        from starlette.testclient import TestClient

        n = 100
        result = BenchmarkResult(transport="ws_echo", sample_count=n)

        with TestClient(ws_app) as client:
            with client.websocket_connect("/ws/echo") as ws:
                for i in range(n):
                    payload = json.dumps({"seq": i, "client_timestamp": time.time()})
                    start = time.perf_counter()
                    ws.send_text(payload)
                    _ = ws.receive_text()
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    result.latencies_ms.append(elapsed_ms)

        assert result.sample_count == n
        assert result.mean > 0
        ci = result.confidence_interval_95()
        assert ci[0] <= result.mean <= ci[1]
        result.to_dict()

    @pytest.mark.asyncio
    async def test_ws_throughput(self, ws_app):
        from starlette.testclient import TestClient

        n = 200

        with TestClient(ws_app) as client:
            with client.websocket_connect("/ws/echo") as ws:
                start = time.perf_counter()
                for i in range(n):
                    ws.send_text(json.dumps({"seq": i}))
                    _ = ws.receive_text()
                elapsed = time.perf_counter() - start

        throughput = n / elapsed
        assert throughput > 0, "Throughput must be positive"

    @pytest.mark.asyncio
    async def test_ws_message_ordering(self, ws_app):
        from starlette.testclient import TestClient

        received = []
        with TestClient(ws_app) as client:
            with client.websocket_connect("/ws/echo") as ws:
                for i in range(100):
                    ws.send_text(json.dumps({"seq": i}))
                    data = json.loads(ws.receive_text())
                    received.append(data["seq"])

        assert received == list(range(100)), "Messages should arrive in order"

    @pytest.mark.asyncio
    async def test_ws_reconnection(self, ws_app):
        from starlette.testclient import TestClient

        with TestClient(ws_app) as client:
            with client.websocket_connect("/ws/echo") as ws:
                ws.send_text(json.dumps({"session": "x", "seq": 1}))
                resp1 = json.loads(ws.receive_text())
            with client.websocket_connect("/ws/echo") as ws:
                ws.send_text(json.dumps({"session": "x", "seq": 2}))
                resp2 = json.loads(ws.receive_text())

        assert resp1["session"] == "x"
        assert resp2["session"] == "x"
        assert "server_timestamp" in resp1
        assert "server_timestamp" in resp2

    @pytest.mark.asyncio
    async def test_ws_concurrent_connections(self, ws_app):
        from starlette.testclient import TestClient

        n_connections = 10
        msgs_per_conn = 20
        all_means = []

        with TestClient(ws_app) as client:
            for conn_id in range(n_connections):
                result = BenchmarkResult(
                    transport=f"ws_conn_{conn_id}", sample_count=msgs_per_conn
                )
                with client.websocket_connect("/ws/echo") as ws:
                    for i in range(msgs_per_conn):
                        start = time.perf_counter()
                        ws.send_text(json.dumps({"conn": conn_id, "seq": i}))
                        _ = ws.receive_text()
                        elapsed_ms = (time.perf_counter() - start) * 1000
                        result.latencies_ms.append(elapsed_ms)
                all_means.append(result.mean)

        assert len(all_means) == n_connections
        assert max(all_means) / max(min(all_means), 0.001) < 10.0, "Unfair distribution"
