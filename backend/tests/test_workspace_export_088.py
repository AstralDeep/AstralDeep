"""Actual streamed HTTP export boundaries; no private capture persistence."""
import asyncio
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from orchestrator import auth
from orchestrator import workspace_export as export

REAL_AUTHENTICATE = auth.get_web_or_bearer_user_payload
PATH = "/api/export/canvas/chat/presentation"
QUERY = "render_revision=7"
TOKEN_HEADERS = {"authorization": "Bearer fixture.access.token", "content-type": "application/json"}
THEME = {key: "#123456" for key in (
    "bg", "surface", "surface2", "border", "primary", "secondary", "accent", "text",
    "muted", "success", "warning", "error", "info")}


def capture():
    return {"version": export.VERSION, "components": [{"type": "text", "content": "Visible transient B"}],
            "viewport": {"width": 393.5, "height": 852.25, "window_width": 421.5, "window_height": 900.25}, "theme": THEME,
            "display_state": [], "images": []}


class ReadRuntime:
    """A transaction fixture that refuses event-loop entry and records writes."""
    def __init__(self):
        self.threads = []

    @contextmanager
    def transaction(self, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        self.threads.append(threading.get_ident())
        yield object()


@pytest.fixture
def host(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    monkeypatch.setattr(export.flags, "is_enabled", lambda name: name == "artifact_export")
    claims = {"sub": "owner", "realm_access": {"roles": ["user"]}}
    authenticate = AsyncMock(return_value=claims)
    monkeypatch.setattr(auth, "get_web_or_bearer_user_payload", authenticate)
    runtime = ReadRuntime()
    repository = SimpleNamespace(get=Mock(side_effect=lambda tx, owner_id, conversation_id:
        SimpleNamespace(render_revision=7) if (owner_id, conversation_id) == ("owner", "chat") else None))
    source = SimpleNamespace(plane_runtime=runtime,
                             plane_repositories=SimpleNamespace(history=SimpleNamespace(conversations=repository)))
    orch = SimpleNamespace(plane_repository_source=source,
                           _save_user_profile=Mock(side_effect=AssertionError("read must not write a profile")))
    from orchestrator.api import export_router
    app = FastAPI()
    app.include_router(export_router)
    app.state.orchestrator = orch
    return SimpleNamespace(app=app, orch=orch, repository=repository, runtime=runtime,
                           authenticate=authenticate, claims=claims)


async def post(host, *, body=None, query=QUERY, headers=None, path=PATH):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://test") as client:
        return await client.post(path + ("?" + query if query else ""),
                                 content=json.dumps(capture()).encode() if body is None else body,
                                 headers=TOKEN_HEADERS if headers is None else headers)


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"error": code}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "X-Astral-Render-Revision" not in response.headers


async def streamed(host, chunks, *, headers=None, query=QUERY):
    """Drive the real ASGI request stream, including declared/actual mismatches."""
    received, sent = [], []
    messages = list(chunks)
    async def receive():
        received.append(True)
        if not messages:
            raise AssertionError("unexpected extra body read")
        value = messages.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value
    async def send(message):
        sent.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "https", "path": PATH, "raw_path": PATH.encode(), "query_string": query.encode(),
             "root_path": "", "headers": headers if headers is not None else [
                 (key.encode(), value.encode()) for key, value in TOKEN_HEADERS.items()],
             "server": ("test", 443), "client": ("127.0.0.1", 1)}
    await host.app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return httpx.Response(start["status"], content=body, headers=start["headers"]), received


@pytest.mark.asyncio
async def test_real_router_renderer_current_revision_transient_capture_and_read_only(host):
    response = await post(host)
    assert response.status_code == 200, response.text
    value = response.json()
    assert set(value) == {"version", "html", "viewport", "theme"}
    assert value["version"] == export.VERSION and "Visible transient B" in value["html"]
    assert value["viewport"] == capture()["viewport"] and value["theme"] == THEME
    assert response.headers["x-astral-render-revision"] == "7"
    assert response.headers["cache-control"] == "no-store"
    assert host.authenticate.await_count == 2
    assert host.repository.get.call_count == 2 and len(host.runtime.threads) == 2
    assert all(thread != threading.get_ident() for thread in host.runtime.threads)
    assert host.repository.get.call_args.kwargs == {"owner_id": "owner", "conversation_id": "chat"}
    host.orch._save_user_profile.assert_not_called()
    assert not hasattr(host.app.state, "capture")
    schema = host.app.openapi()["paths"][PATH.replace("chat", "{chat_id}")]["post"]
    assert next(p for p in schema["parameters"] if p["name"] == "render_revision")["required"] is True
    assert set(schema["requestBody"]["content"]["application/json"]["schema"]["required"]) == set(capture())


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "render_revision=-1", "render_revision=01", "render_revision=true",
    "render_revision=1.0", "render_revision=+7", "render_revision=9223372036854775808",
    "render_revision=7&render_revision=7", "render_revision=7&other=1"])
async def test_revision_must_be_one_exact_canonical_bounded_decimal_before_body(host, query):
    response, received = await streamed(host, [], query=query)
    assert_error(response, 422, "presentation_revision_invalid")
    assert received == [] and host.repository.get.call_count == 0


@pytest.mark.asyncio
async def test_query_credentials_never_authorize_post_or_consume_body(host):
    response, received = await streamed(host, [], query=QUERY + "&token=private")
    assert_error(response, 403, "presentation_query_token_refused")
    assert not received and host.authenticate.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("header,value,code", [("content-type", "text/plain", "presentation_json_required"),
    ("content-encoding", "gzip", "presentation_encoding_refused")])
async def test_type_and_encoding_denied_before_body(host, header, value, code):
    headers = {**TOKEN_HEADERS, header: value}
    response, received = await streamed(host, [], headers=[(key.encode(), val.encode()) for key, val in headers.items()])
    assert_error(response, 415, code)
    assert not received and host.repository.get.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "null", "https://other", "https://test:0", "https://test/path",
    "https://test?x=1", "https://test#fragment", "https://user@test", " https://test", "https://test:bad"])
async def test_cookie_same_origin_is_required_before_body(host, origin):
    headers = {"content-type": "application/json"}
    if origin is not None:
        headers["origin"] = origin
    response, received = await streamed(host, [], headers=[(key.encode(), val.encode()) for key, val in headers.items()])
    assert_error(response, 403, "presentation_origin_refused")
    assert not received and host.repository.get.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("base,origin", [(None, "https://test:443"), ("http://public:80/base", "http://public"),
                                      ("https://public/base", "https://public:443")])
async def test_cookie_exact_public_base_or_request_origin_and_default_port(host, monkeypatch, base, origin):
    if base:
        monkeypatch.setenv("PUBLIC_BASE_URL", base)
    response = await post(host, headers={"content-type": "application/json", "origin": origin})
    assert response.status_code == 200
    assert all(call.args[1] is None for call in host.authenticate.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("header,value,status,code", [
    ("content-type", "application/json", 415, "presentation_json_required"),
    ("content-encoding", "identity", 415, "presentation_encoding_refused"),
    ("origin", "https://test", 403, "presentation_origin_refused"),
    ("content-length", "0", 400, "presentation_length_invalid")])
async def test_duplicate_sensitive_headers_fail_closed(host, header, value, status, code):
    headers = [(b"content-type", b"application/json")]
    if header != "origin":
        headers.append((b"authorization", b"Bearer fixture"))
    if header != "content-type":
        headers.append((header.encode(), value.encode()))
    headers.append((header.encode(), value.encode()))
    response, received = await streamed(host, [], headers=headers)
    assert_error(response, status, code)
    assert not received


@pytest.mark.asyncio
@pytest.mark.parametrize("length", ["-1", "01", "2.0", "true", "99999999999999999999"])
async def test_malformed_declared_length_is_refused_without_body(host, length):
    headers = [(key.encode(), value.encode()) for key, value in {**TOKEN_HEADERS, "content-length": length}.items()]
    response, received = await streamed(host, [], headers=headers)
    assert_error(response, 400, "presentation_length_invalid")
    assert not received


@pytest.mark.asyncio
async def test_stream_actual_overflow_and_declared_limit_do_not_drain_remaining_body(host, monkeypatch):
    monkeypatch.setattr(export, "MAX_INPUT_BYTES", 5)
    response, received = await streamed(host, [], headers=[(key.encode(), value.encode()) for key, value in
        {**TOKEN_HEADERS, "content-length": "6"}.items()])
    assert_error(response, 413, "presentation_too_large")
    assert not received
    response, received = await streamed(host, [{"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": True}, AssertionError("must not drain")])
    assert_error(response, 413, "presentation_too_large")
    assert len(received) == 2


@pytest.mark.asyncio
async def test_stream_exact_bound_and_declared_mismatch(host, monkeypatch):
    body = json.dumps(capture()).encode()
    monkeypatch.setattr(export, "MAX_INPUT_BYTES", len(body))
    response, received = await streamed(host, [{"type": "http.request", "body": body[:8], "more_body": True},
        {"type": "http.request", "body": body[8:], "more_body": False}])
    assert response.status_code == 200 and len(received) == 2
    response, _ = await streamed(host, [{"type": "http.request", "body": b"{}", "more_body": False}],
        headers=[(key.encode(), value.encode()) for key, value in {**TOKEN_HEADERS, "content-length": "3"}.items()])
    assert_error(response, 400, "presentation_length_invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"{}", b"\xff", b'{"private":NaN}', b'{"private":1,"private":2}', b'"private"'])
async def test_real_projection_invalid_input_maps_to_one_data_free_code(host, body, caplog):
    response = await post(host, body=body)
    assert_error(response, 422, "presentation_invalid")
    assert "private" not in response.text and "private" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", ["foreign", "absent", "x" * 257, " "])
async def test_foreign_absent_and_oversized_chat_are_uniform_before_body(host, chat):
    response = await post(host, path=PATH.replace("chat/", chat + "/"), body=b"private")
    assert_error(response, 404, "presentation_not_found")
    assert host.authenticate.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, 8, True, "7", None])
async def test_noncurrent_or_malformed_server_revision_cannot_export(host, revision):
    host.repository.get.return_value = SimpleNamespace(render_revision=revision)
    host.repository.get.side_effect = None
    assert_error(await post(host), 409, "presentation_revision_changed")


@pytest.mark.asyncio
@pytest.mark.parametrize("change,status,code", [
    ("revision", 409, "presentation_revision_changed"), ("owner", 404, "presentation_not_found"),
    ("flag", 404, "presentation_not_found"), ("identity", 401, "presentation_identity_changed"),
    ("expiry", 401, "presentation_authentication_required"), ("roles", 403, "presentation_authentication_required"),
    ("application", 503, "presentation_unavailable")])
async def test_current_authority_and_revision_rechecked_after_render(host, monkeypatch, change, status, code):
    original = export.render_presentation
    def render(payload):
        output = original(payload)
        if change in {"revision", "owner"}:
            host.repository.get.side_effect = None
            host.repository.get.return_value = None if change == "owner" else SimpleNamespace(render_revision=8)
        elif change == "flag":
            monkeypatch.setattr(export.flags, "is_enabled", lambda name: False)
        elif change == "identity":
            host.authenticate.return_value = {"sub": "other", "realm_access": {"roles": ["user"]}}
        elif change == "expiry":
            host.authenticate.side_effect = HTTPException(401, "private expired token")
        elif change == "roles":
            host.authenticate.return_value = {"sub": "owner", "realm_access": {"roles": []}}
        else:
            host.app.state.orchestrator = object()
        return output
    monkeypatch.setattr(export, "render_presentation", render)
    assert_error(await post(host), status, code)


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [None, {}, {"sub": ""}, {"sub": " \t"}, {"sub": True}, {"sub": "x" * 513}])
async def test_missing_and_malformed_subject_never_reaches_render_or_repository(host, claims):
    host.authenticate.return_value = claims
    assert_error(await post(host), 401, "presentation_authentication_required")
    assert host.repository.get.call_count == 0


@pytest.mark.asyncio
async def test_disabled_missing_runtime_and_unexpected_failures_are_safe(host, monkeypatch, caplog):
    monkeypatch.setattr(export.flags, "is_enabled", lambda name: False)
    assert_error(await post(host), 404, "presentation_not_found")
    assert host.repository.get.call_count == 0
    monkeypatch.setattr(export.flags, "is_enabled", lambda name: True)
    host.app.state.orchestrator = None
    assert_error(await post(host), 503, "presentation_unavailable")
    host.app.state.orchestrator = SimpleNamespace()
    assert_error(await post(host), 503, "presentation_unavailable")
    host.app.state.orchestrator = host.orch
    def fail(_payload):
        raise RuntimeError("private capture")
    monkeypatch.setattr(export, "render_presentation", fail)
    assert_error(await post(host), 503, "presentation_unavailable")
    assert "private capture" not in caplog.text


@pytest.mark.asyncio
async def test_output_byte_limit_applies_to_encoded_whole_response(host, monkeypatch):
    monkeypatch.setattr(export, "MAX_OUTPUT_BYTES", 1)
    assert_error(await post(host), 413, "presentation_output_too_large")


@pytest.mark.asyncio
async def test_disconnect_and_read_timeout_are_bounded_refusals(host, monkeypatch):
    response, _ = await streamed(host, [{"type": "http.disconnect"}])
    assert_error(response, 408, "presentation_interrupted")
    monkeypatch.setattr(export, "_READ_SECONDS", .01)
    async def stalled(self):
        await asyncio.Event().wait()
        yield b"unreachable"
    service = host.app.state.workspace_export_service
    with monkeypatch.context() as scoped:
        scoped.setattr(Request, "stream", stalled)
        assert_error(await post(host), 408, "presentation_interrupted")
    assert (await post(host)).status_code == 200
    assert host.app.state.workspace_export_service is service


@pytest.mark.asyncio
@pytest.mark.parametrize("abandon", ["cancel", "timeout"])
async def test_cancelled_cpu_keeps_admission_until_worker_finishes_and_never_queues(host, monkeypatch, abandon):
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    lock = threading.Lock()
    count = 0
    def held(_payload):
        nonlocal count
        with lock:
            position = count
            count += 1
        entered[position].set()
        assert release.wait(5), "fixture worker must always be released"
        raise RuntimeError("private failed rendering after cancellation")
    monkeypatch.setattr(export, "render_presentation", held)
    monkeypatch.setattr(export, "_RENDER_SECONDS", .05 if abandon == "timeout" else 5)
    tasks = [asyncio.create_task(post(host)) for _ in range(2)]
    try:
        assert all(await asyncio.gather(*(asyncio.to_thread(event.wait, 2) for event in entered)))
        if abandon == "cancel":
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(value, asyncio.CancelledError) for value in results)
        else:
            for response in await asyncio.gather(*tasks):
                assert_error(response, 408, "presentation_interrupted")
        for _ in range(3):
            response, reads = await streamed(host, [])
            assert_error(response, 429, "presentation_busy")
            assert not reads
        assert count == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    service = host.app.state.workspace_export_service
    for _ in range(100):
        if service.capacity.acquire(blocking=False):
            service.capacity.release()
            break
        await asyncio.sleep(.01)
    monkeypatch.setattr(export, "render_presentation", lambda payload: {"version": export.VERSION, "html": "ok", "viewport": {}, "theme": {}})
    assert (await post(host)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["cookie", "bearer"])
async def test_original_iam_rechecked_after_render_without_profile_write(host, monkeypatch, transport):
    from jose import JWTError
    from orchestrator import web_auth
    # Restore the real shared IAM function, while substituting only the network/signature boundary.
    monkeypatch.setattr(auth, "get_web_or_bearer_user_payload", REAL_AUTHENTICATE)
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setattr(auth, "_get_keycloak_config", lambda: ("https://iam.example/realms/test", "astral-frontend", ""))
    monkeypatch.setattr("shared.jwks_cache.get_jwks", AsyncMock(return_value={"keys": []}))
    session = AsyncMock(return_value={"access_token": "fixture.access.token"})
    monkeypatch.setattr(web_auth, "ensure_session", session)
    decode = Mock(return_value={"sub": "owner", "realm_access": {"roles": ["user"]}})
    monkeypatch.setattr("jose.jwt.decode", decode)
    headers = TOKEN_HEADERS if transport == "bearer" else {"content-type": "application/json", "origin": "https://test"}
    assert (await post(host, headers=headers)).status_code == 200
    assert decode.call_count == 2 and session.call_count == (2 if transport == "cookie" else 0)
    original = export.render_presentation
    def expire(payload):
        value = original(payload)
        decode.side_effect = JWTError("fixture expired")
        if transport == "cookie":
            session.return_value = None
        return value
    monkeypatch.setattr(export, "render_presentation", expire)
    assert_error(await post(host, headers=headers), 401, "presentation_authentication_required")
    host.orch._save_user_profile.assert_not_called()
    assert host.repository.get.call_count == 3
