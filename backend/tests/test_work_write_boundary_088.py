"""Tests for the bounded ASGI Work write body parser
(backend/orchestrator/work_write_boundary.py): frame length limits, malformed JSON
refusal, and transport-snapshot isolation from headers and server state.
"""

import asyncio
import json

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
import pytest

from orchestrator import work_write_boundary as module
from persistent_agents.models import AssignmentError, StrictModel

pytestmark = pytest.mark.asyncio


class Body(StrictModel):
    expected_revision: int


class Route(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def boundary(request):
            try:
                snapshot = module.freeze_work_request(request)
                await module.cache_work_write_body(snapshot)
                return await handler(snapshot)
            except AssignmentError as exc:
                return JSONResponse({"error": exc.code}, status_code=exc.status_code)
        return boundary


async def send(*, body=b'{"expected_revision":1}', headers=(), messages=None, receive=None):
    app = FastAPI()
    router = APIRouter(route_class=Route)
    called = []
    @router.post("/")
    async def command(body: Body, request: Request):
        called.append(body.expected_revision)
        assert request.scope["state"] == {}
        assert await request.body() == await request.body()
        return {"revision": body.expected_revision}
    app.include_router(router)
    queue = list(messages) if messages is not None else [{"type": "http.request", "body": body}]
    frames, receives = [], []
    async def source():
        receives.append(True)
        return await receive() if receive is not None else queue.pop(0)
    async def sink(value):
        frames.append(value)
    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"",
             "scheme": "https", "server": ("app.invalid", 443), "state": {"unverified": "secret"},
             "headers": [(b"content-type", b"application/json"), *headers]}
    await app(scope, source, sink)
    result = json.loads(b"".join(frame.get("body", b"") for frame in frames))
    return frames[0]["status"], result, called, len(receives)


async def test_actual_fastapi_reads_validated_cache_without_extra_receive():
    raw = b'{"expected_revision":1}'
    status, body, called, reads = await send(headers=[(b"content-length", str(len(raw)).encode())],
        messages=[{"type": "http.request", "body": raw[:8], "more_body": True},
                  {"type": "http.request", "body": raw[8:], "more_body": False}])
    assert (status, body, called, reads) == (200, {"revision": 1}, [1], 2)


@pytest.mark.parametrize("headers,status", [
    ([(b"content-length", b"4"), (b"content-length", b"4")], 400),
    ([(b"content-length", b"-1")], 400),
    ([(b"content-length", b"16385")], 413),
    ([(b"content-encoding", b"gzip")], 415),
    ([(b"content-encoding", b"identity"), (b"content-encoding", b"identity")], 415),
])
async def test_framing_refuses_before_receiving_or_parsing(headers, status):
    actual, body, called, reads = await send(headers=headers)
    assert actual == status and set(body) == {"error"} and not called and reads == 0


@pytest.mark.parametrize("raw", [
    b"{", b"\xff", b'{"expected_revision":1,"expected_revision":2}',
    b'{"expected_revision":NaN}', b'{"expected_revision":1e400}',
    b'{"nested":{"x":1,"x":2}}', b'{"value":"\\ud800"}', b"[" * 1100 + b"]" * 1100,
])
async def test_invalid_json_never_reaches_fastapi_model(raw):
    status, body, called, reads = await send(body=raw)
    assert (status, body, called, reads) == (400, {"error": "work_body_invalid"}, [], 1)


@pytest.mark.parametrize("raw", [b'{"expected_revision":true}', b'{"expected_revision":1.5}',
                                   b'{"expected_revision":"1"}', b'{"expected_revision":1,"owner":"x"}'])
async def test_existing_strict_model_retains_numeric_and_extra_field_denials(raw):
    status, _, called, _ = await send(body=raw)
    assert status == 422 and not called


@pytest.mark.parametrize("messages,code", [
    ([{"type": "http.disconnect"}], "work_disconnected"),
    ([{"type": "http.request", "body": b"x" * 16385}], "work_body_too_large"),
    ([{"type": "http.request", "body": "not bytes"}], "work_body_invalid"),
    ([{"type": "http.request", "body": b"", "more_body": "false"}], "work_body_invalid"),
    ([{"type": "other"}], "work_body_invalid"),
    ([[]], "work_body_invalid"),
])
async def test_malformed_or_excessive_raw_frames_refuse(messages, code):
    status, body, called, _ = await send(messages=messages)
    assert status == (413 if code == "work_body_too_large" else 400)
    assert body == {"error": code} and not called


async def test_declared_length_mismatch_refuses():
    status, body, called, _ = await send(headers=[(b"content-length", b"1")])
    assert (status, body, called) == (400, {"error": "work_body_invalid"}, [])


@pytest.mark.parametrize("empty", [True, False])
async def test_empty_frames_or_stalled_peer_cannot_outlive_body_bound(monkeypatch, empty):
    monkeypatch.setattr(module, "BODY_SECONDS", .02)
    async def receive():
        if not empty:
            await asyncio.Event().wait()
        return {"type": "http.request", "body": b"", "more_body": True}
    status, body, called, _ = await asyncio.wait_for(send(receive=receive), .5)
    assert (status, body, called) == (408, {"error": "work_body_timeout"}, [])


async def test_snapshot_detaches_headers_state_and_server_client():
    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"one",
             "headers": [(b"Authorization", b"Bearer original")],
             "state": {"delegation_subject_token": "forged"},
             "server": ["app.invalid", 443], "client": ["127.0.0.1", 44]}
    snapshot = module.freeze_work_request(Request(scope))
    scope["headers"].clear()
    scope["server"][0] = "changed"
    scope["client"][0] = "changed"
    scope.update(method="GET", query_string=b"two")
    assert snapshot.method == "POST" and snapshot.scope["query_string"] == b"one"
    assert snapshot.headers["authorization"] == "Bearer original" and snapshot.scope["state"] == {}
    assert snapshot.scope["server"] == ("app.invalid", 443) and snapshot.scope["client"] == ("127.0.0.1", 44)


@pytest.mark.parametrize("change", ["headers", "size", "count", "query", "absent"])
async def test_malformed_transport_snapshot_is_closed_refusal(change):
    scope = {"type": "http", "headers": [], "query_string": b""}
    if change == "headers":
        scope["headers"] = [("cookie", "text")]
    elif change == "size":
        scope["headers"] = [(b"cookie", b"x" * 65536)]
    elif change == "count":
        scope["headers"] = [(b"x", b"y")] * 257
    elif change == "query":
        scope["query_string"] = "text"
    else:
        scope.pop("headers")
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        module.freeze_work_request(Request(scope))
