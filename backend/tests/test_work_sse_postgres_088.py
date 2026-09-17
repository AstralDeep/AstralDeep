"""Bounded Work SSE delivery and the versioned decide command over real IAM.

Only external JWKS/refresh replies are synthetic. The mounted router, cookie
rows, Plane operations/actions, audit rows and every SQL wait are real. A
minimal ASGI driver reads the event stream incrementally and can disconnect
on demand, which ``httpx.ASGITransport`` (buffered, no live disconnect) cannot.
"""
import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest

from orchestrator import auth, web_auth, work_api
from orchestrator.work_api import work_router
from orchestrator.work_service import WorkService
from tests.test_operation_session_authority_088 import create_operation
from tests.test_work_control_authority_088 import bound as bound
from tests.test_work_controls_postgres_088 import issued_fixture
from tests.test_work_submit_postgres_088 import (
    fixture as fixture, runtime as runtime, service as service, signing_key as signing_key,
)
from tests.test_work_write_http_postgres_088 import headers

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mounted(bound, fixture, runtime, monkeypatch):
    assignments, app = bound
    app.include_router(work_router, prefix="/api")
    monkeypatch.setattr(work_api, "SSE_INTERVAL_SECONDS", 0.05)
    record = create_operation(fixture, runtime)
    return assignments, app, record


def _path(record, suffix="/events"):
    return f"/api/work/v1/operations/{record.assignment_id}{suffix}"


def read_headers(fixture, kind="cookie", **token_changes):
    if kind == "cookie":
        return {"cookie": "astral_session=" + web_auth._sign(fixture[2])}
    return {"authorization": "Bearer " + fixture[3](**token_changes)}


class Stream:
    """Drive one GET through the ASGI app, reading SSE frames as they arrive."""

    def __init__(self, app, path, request_headers, query=b""):
        self._app = app
        self.scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "https",
            "path": path, "raw_path": path.encode(), "root_path": "", "query_string": query,
            "headers": [(b"host", b"app.invalid")] + [
                (key.lower().encode(), value.encode()) for key, value in request_headers.items()],
            "server": ("app.invalid", 443), "client": ("127.0.0.1", 40000),
        }
        self.chunks = asyncio.Queue()
        self.disconnect = asyncio.Event()
        self.status, self.headers, self.ticks, self.ended = None, {}, 0, False
        self._buffer, self._body_sent, self.task = b"", False, None

    async def __aenter__(self):
        self.task = asyncio.create_task(self._app(self.scope, self._receive, self._send))
        return self

    async def __aexit__(self, *_):
        self.disconnect.set()
        await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 5)

    async def _receive(self):
        if not self._body_sent:
            self._body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {key.decode().lower(): value.decode() for key, value in message["headers"]}
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                self.chunks.put_nowait(body)
            if not message.get("more_body", False):
                self.chunks.put_nowait(None)

    def _parse(self, frame):
        event = {"id": None, "event": "message", "data": None}
        for line in frame.split(b"\n"):
            if line.startswith(b":"):
                self.ticks += 1
                return None
            name, _, value = line.partition(b":")
            value = value.decode().lstrip(" ")
            if name == b"id":
                event["id"] = int(value)
            elif name == b"event":
                event["event"] = value
            elif name == b"data":
                event["data"] = json.loads(value)
        return event

    async def next(self, timeout=5):
        """Return the next non-comment event, or None once the response ended.

        ``timeout`` bounds the whole wait, not each keepalive comment chunk.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            while b"\n\n" in self._buffer:
                frame, self._buffer = self._buffer.split(b"\n\n", 1)
                parsed = self._parse(frame)
                if parsed is not None:
                    return parsed
            if self.ended:
                return None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            chunk = await asyncio.wait_for(self.chunks.get(), remaining)
            if chunk is None:
                self.ended = True
                continue
            self._buffer += chunk

    async def drain(self, timeout=5):
        events = []
        while (event := await self.next(timeout)) is not None:
            events.append(event)
        return events


async def _control(app, fixture, record, command, revision):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        return await client.post(_path(record, "/" + command), headers=headers(fixture),
                                 json={"submission_id": str(uuid4()), "expected_revision": revision})


@pytest.mark.parametrize("kind", ["cookie", "bearer"])
async def test_stream_emits_revision_events_for_committed_controls_without_blocking_them(
    mounted, fixture, kind,
):
    _, app, record = mounted
    async with Stream(app, _path(record), read_headers(fixture, kind)) as stream:
        first = await stream.next()
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert stream.headers["cache-control"] == "no-store"
        assert first["event"] == "revision" and first["id"] == 1
        assert first["data"]["revision"] == 1 and first["data"]["changed"] is True
        assert first["data"]["operation"]["id"] == record.assignment_id
        assert "authority" not in json.dumps(first) and "checkpoint" not in json.dumps(first)
        started = time.monotonic()
        paused = await _control(app, fixture, record, "pause", 1)
        # A concurrent control commits promptly: the stream holds no transaction.
        assert paused.status_code == 200 and time.monotonic() - started < 2
        second = await stream.next()
        assert second["event"] == "revision" and second["id"] == paused.json()["operation"]["revision"]
        assert second["data"]["operation"]["disposition"] == "paused"
        cancelled = await _control(app, fixture, record, "cancel", second["id"])
        third = await stream.next()
        assert third["id"] == cancelled.json()["operation"]["revision"] > second["id"]
        assert third["data"]["operation"]["disposition"] == "cancelled"
        assert stream.ticks >= 1
    assert fixture[-1] == []


async def test_original_bearer_expiry_mid_stream_ends_with_bounded_error_and_no_further_frames(
    mounted, fixture,
):
    _, app, record = mounted
    expiry = int(time.time()) + 3
    async with Stream(app, _path(record), read_headers(fixture, "bearer", exp=expiry)) as stream:
        assert (await stream.next())["event"] == "revision"
        final = await stream.next(timeout=6)
        finished = time.time()
        assert final == {"id": 1, "event": "error", "data": {"error": "work_authentication_required"}}
        # Ended against the actual signed credential's deadline, not later.
        assert expiry - 0.5 <= finished <= expiry + 1.5
        assert await stream.next() is None and stream.ended
        await asyncio.wait_for(stream.task, 5)
    assert fixture[-1] == []


async def test_cookie_deletion_mid_stream_ends_delivery(mounted, fixture, runtime):
    _, app, record = mounted
    async with Stream(app, _path(record), read_headers(fixture)) as stream:
        assert (await stream.next())["event"] == "revision"
        with runtime.transaction() as tx:
            runtime.repositories.history.sessions.delete_owner(tx, owner_id=fixture[1])
        final = await stream.next()
        assert final["event"] == "error" and final["data"] == {"error": "work_authentication_required"}
        assert await stream.next() is None
    assert fixture[-1] == []


async def test_client_policy_revocation_mid_stream_ends_delivery(mounted, fixture, monkeypatch):
    _, app, record = mounted
    async with Stream(app, _path(record), read_headers(fixture, "bearer", azp="astral-native")) as stream:
        assert (await stream.next())["event"] == "revision"
        monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "")
        final = await stream.next()
        assert final["event"] == "error" and final["data"] == {"error": "work_authentication_required"}
        assert await stream.next() is None


async def test_same_issuance_rotation_never_substitutes_the_original_verified_token(
    mounted, fixture, monkeypatch,
):
    _, app, record = mounted
    original_token = (await asyncio.to_thread(fixture[0].get, fixture[2]))["access_token"]
    verified = []
    verify = auth.verify_production_token

    async def record_token(token, *args, **kwargs):
        verified.append(token)
        return await verify(token, *args, **kwargs)

    monkeypatch.setattr(auth, "verify_production_token", record_token)
    rotated = fixture[3](jti="next-generation")
    async with Stream(app, _path(record), read_headers(fixture)) as stream:
        assert (await stream.next())["event"] == "revision"
        fixture[0].update_tokens(fixture[2], access_token=rotated, refresh_token="synthetic-next-sse-refresh")
        verified.clear()
        # Only the stream verifies during this quiet window: every tick still
        # presents the original token, never the rotated same-issuance one.
        with pytest.raises(TimeoutError):
            await stream.next(timeout=0.4)
        assert len(verified) >= 2 and set(verified) == {original_token} and rotated != original_token
        paused = await _control(app, fixture, record, "pause", 1)
        assert paused.status_code == 200
        second = await stream.next()
        assert second["event"] == "revision" and second["data"]["operation"]["disposition"] == "paused"
    assert fixture[-1] == []


async def test_last_event_id_and_after_revision_resume_without_duplicate_frames(mounted, fixture):
    _, app, record = mounted
    paused = await _control(app, fixture, record, "pause", 1)
    current = paused.json()["operation"]["revision"]
    async with Stream(app, _path(record), {**read_headers(fixture), "last-event-id": str(current)}) as stream:
        await asyncio.sleep(0.3)
        cancelled = await _control(app, fixture, record, "cancel", current)
        event = await stream.next()
        # Nothing was replayed for the already-seen revision; the first frame
        # is the later committed control.
        assert event["event"] == "revision" and event["id"] == cancelled.json()["operation"]["revision"]
        assert event["data"]["operation"]["disposition"] == "cancelled"
        assert stream.ticks >= 1
        latest = event["id"]
    async with Stream(app, _path(record), read_headers(fixture),
                      query=("after_revision=%d" % latest).encode()) as stream:
        with pytest.raises(TimeoutError):
            await stream.next(timeout=0.3)
        assert stream.status == 200 and stream.ticks >= 1
    # A Last-Event-ID ahead of the store is an explicit resync, never trusted state.
    async with Stream(app, _path(record), {**read_headers(fixture), "last-event-id": str(latest + 50)}) as stream:
        event = await stream.next()
        assert event["event"] == "revision" and event["data"]["resync_required"] is True
        assert event["id"] == event["data"]["revision"]


async def test_disconnect_stops_the_loop_with_no_lingering_task(mounted, fixture, monkeypatch):
    assignments, app, record = mounted
    polls = 0
    original = assignments.store.transaction

    async def counted(callback, **kwargs):
        nonlocal polls
        polls += 1
        return await original(callback, **kwargs)

    monkeypatch.setattr(assignments.store, "transaction", counted)
    before = {task for task in asyncio.all_tasks() if not task.done()}
    stream = Stream(app, _path(record), read_headers(fixture))
    async with stream:
        assert (await stream.next())["event"] == "revision"
        with pytest.raises(TimeoutError):
            await stream.next(timeout=0.3)
        assert stream.ticks >= 1
        stream.disconnect.set()
        await asyncio.wait_for(stream.task, 5)
    settled = polls
    await asyncio.sleep(0.4)
    assert polls == settled
    lingering = {task for task in asyncio.all_tasks() if not task.done()} - before - {asyncio.current_task()}
    assert lingering == set()


async def test_time_bound_ends_with_a_resumable_end_event_and_is_capped(mounted, fixture):
    _, app, record = mounted
    async with Stream(app, _path(record), read_headers(fixture), query=b"max_seconds=1") as stream:
        started = time.monotonic()
        assert (await stream.next())["event"] == "revision"
        final = await stream.next()
        assert final == {"id": 1, "event": "end", "data": {"reason": "work_stream_bounded", "revision": 1}}
        assert 0.8 <= time.monotonic() - started <= 2.5
        assert await stream.next() is None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        for value in ("0", "901", "1.5", "abc"):
            response = await client.get(_path(record), params={"max_seconds": value}, headers=read_headers(fixture))
            assert response.status_code == 422 and response.json() == {"error": "work_query_invalid"}


async def test_refusals_before_streaming_are_ordinary_closed_json(mounted, fixture, monkeypatch):
    _, app, record = mounted
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        for identity in (str(uuid4()), "not-an-id"):
            response = await client.get(f"/api/work/v1/operations/{identity}/events", headers=read_headers(fixture))
            assert response.status_code == 404 and response.json() == {"error": "work_not_found"}
            assert response.headers["cache-control"] == "no-store"
        for value in ("abc", "0", "-1", "1.0"):
            response = await client.get(_path(record), headers={**read_headers(fixture), "Last-Event-ID": value})
            assert response.status_code == 422 and response.json() == {"error": "work_query_invalid"}
        unauthenticated = await client.get(_path(record))
        assert unauthenticated.status_code == 401 and unauthenticated.json() == {"error": "work_authentication_required"}
        expired = await client.get(_path(record), headers=read_headers(fixture, "bearer", exp=int(time.time()) - 1))
        assert expired.status_code == 401 and record.assignment_id not in expired.text
        # A buffered client only returns once the bounded stream ends.
        with_token = await client.get(_path(record), params={"token": fixture[3](), "max_seconds": "1"},
                                      headers={"Last-Event-ID": ""})
        assert with_token.status_code == 200
        assert with_token.headers["content-type"].startswith("text/event-stream")


async def test_mounted_decide_commits_decision_audit_and_is_observed_by_the_stream(mounted, fixture):
    assignments, app, record = mounted
    action, _, _ = await issued_fixture(WorkService(assignments), record.assignment_id,
                                        owner=fixture[1], proposed=True)
    body = {"submission_id": str(uuid4()), "expected_revision": 0,
            "proposal_digest": action.intent.request_digest, "decision": "approve"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        current = (await client.get(_path(record, ""), headers=read_headers(fixture))).json()["operation"]
        assert current["disposition"] == "awaiting_approval"
        body["expected_revision"] = current["revision"]
        target = _path(record, f"/actions/{action.action_id}/decide")
        async with Stream(app, _path(record), {**read_headers(fixture),
                                               "last-event-id": str(current["revision"])}) as stream:
            decided = await client.post(target, json=body, headers=headers(fixture))
            assert decided.status_code == 200, decided.text
            assert decided.json()["applied"] is True and decided.json()["state"] == "approved"
            assert decided.json()["action_id"] == action.action_id
            assert set(decided.json()) == {"operation", "action_id", "state", "applied"}
            event = await stream.next()
            assert event["event"] == "revision" and event["id"] == decided.json()["operation"]["revision"]
        replay = await client.post(target, json=body, headers=headers(fixture, cookie=False))
        assert replay.status_code == 200 and replay.json() == {**decided.json(), "applied": False}
        missing = await client.post(_path(record, f"/actions/{uuid4()}/decide"), json=body, headers=headers(fixture))
        assert missing.status_code == 404 and missing.json() == {"error": "work_not_found"}
        invalid = await client.post(target, json={**body, "decision": "maybe"}, headers=headers(fixture))
        assert invalid.status_code == 422 and invalid.json() == {"error": "work_control_invalid"}
    rows, _ = app.state.orchestrator.audit_repo.list_for_user(fixture[1])
    decisions = [row for row in rows if row.action_type == "work.action.decide"]
    assert len(decisions) == 1
    assert decisions[0].outputs_meta["decision"] == "approve"
    assert decisions[0].outputs_meta["action_id"] == action.action_id
    assert decisions[0].outputs_meta["submission_id"] == body["submission_id"]
    assert "proposal_digest" not in json.dumps(decisions[0].outputs_meta)
    assert app.state.orchestrator.audit_repo.verify_chain(fixture[1]) is None
    assert fixture[-1] == []
