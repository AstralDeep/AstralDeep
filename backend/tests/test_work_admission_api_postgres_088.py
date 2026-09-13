"""Closed ASGI submission with real IAM policy, installed Plane and atomic audit.

Only JWT/JWKS/refresh replies are synthetic. The real fixed handler is selected
but no runner loop or provider/tool execution starts in these transport tests.
"""

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from orchestrator import auth
from orchestrator.work_admission_api import work_admission_router
from persistent_agents.models import AssignmentError
from persistent_agents.research_episode import run_research_episode
from persistent_agents.runner import AssignmentRunner, OneShotLifecycle
from tests.test_request_session_authority_088 import request
from tests.test_work_research_preflight_postgres_088 import (
    fixture as fixture,
    no_acceptance,
    plane as plane,
    research_command,
    research_service as research_service,
    service as service,
    signing_key as signing_key,
    source_service as source_service,
)
from tests.test_work_submit_postgres_088 import totals

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def api(research_service, runtime, fixture, monkeypatch):
    """Build production-shaped bindings without an alternate service injection."""
    service = research_service
    orch = service.assignments.orch
    orch.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(runtime=runtime, repositories=runtime.repositories)
    )
    orch.persistent_assignments = service.assignments
    orch.web_sessions = service.sessions
    orch.audit_repo = service.audit
    orch._llm_store = service.research_preflight.config_store
    runner = AssignmentRunner(
        orch,
        service.assignments,
        one_shot=OneShotLifecycle(service.sessions, run_research_episode),
    )
    orch.persistent_assignment_runner = runner
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_admission_router, prefix="/api")
    tick_seen = asyncio.Event()

    async def no_dispatch():
        tick_seen.set()

    monkeypatch.setattr(runner, "tick", no_dispatch)
    runner.start()
    supervisor = runner._loop
    await asyncio.wait_for(tick_seen.wait(), 2)
    yield SimpleNamespace(
        app=app,
        orch=orch,
        runner=runner,
        service=service,
        fixture=fixture,
        runtime=runtime,
    )
    runner._stopping = True
    runner._wake.set()
    await asyncio.gather(supervisor, return_exceptions=True)


async def submit(
    api,
    body=None,
    *,
    chunks=None,
    headers=(),
    bearer=False,
    cookie=True,
    claims=None,
    query=b"",
    messages=None,
    receive=None,
    method="POST",
    state=None,
):
    """Send exact ASGI frames, including disconnect/framing cases HTTP clients hide."""
    fields = [
        (b"content-type", b"application/json"),
        (b"origin", b"https://app.invalid"),
        *headers,
    ]
    if bearer:
        fields.append(
            (b"authorization", ("Bearer " + api.fixture[3](**(claims or {}))).encode())
        )
    scope = dict(
        request(
            api.fixture[2] if cookie else None,
            headers=fields,
            query=query,
            method=method,
        ).scope
    )
    scope.update(
        path="/api/work/v1/operations",
        http_version="1.1",
        root_path="",
        client=("127.0.0.1", 30000),
        state=state if state is not None else {},
    )
    if messages is None:
        chunks = (
            chunks
            if chunks is not None
            else [body if body is not None else research_command(api.service)]
        )
        messages = [
            {"type": "http.request", "body": chunk, "more_body": i != len(chunks) - 1}
            for i, chunk in enumerate(chunks)
        ]
    received = 0
    sent = []

    async def next_message():
        nonlocal received
        received += 1
        if receive is not None:
            return await receive(received)
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await api.app(scope, next_message, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    raw = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    return SimpleNamespace(
        status=start["status"],
        value=json.loads(raw),
        raw=raw,
        headers=dict(start["headers"]),
        state=scope["state"],
        received=received,
    )


async def test_actual_admission_and_receipt_have_only_safe_fields(api):
    body = research_command(api.service)
    result = await submit(
        api,
        body,
        chunks=[body[:19], body[19:]],
        headers=[(b"content-length", str(len(body)).encode())],
    )
    assert result.status == 201
    assert set(result.value) == {"id", "revision", "created"}
    assert result.value["created"] is True and result.value["revision"] >= 1
    assert result.headers[b"cache-control"] == b"no-store"
    assert totals(api.runtime, api.fixture[1]) == (1, 1, 1)
    assert set(result.state) == {"audit_claims"}
    assert result.state["audit_claims"]["sub"] == api.fixture[1]
    assert all(
        value not in result.raw
        for value in (b"synthetic", b"Read one", b"releases", b"token")
    )
    replay = await submit(api, body)
    assert replay.status == 200 and replay.value == {**result.value, "created": False}
    assert totals(api.runtime, api.fixture[1]) == (1, 1, 1)
    assert len(api.fixture[-1]) == 1
    assert api.service.audit.verify_chain(api.fixture[1]) is None


@pytest.mark.parametrize(
    "change", ["absent", "stopping", "handler", "sessions", "service"]
)
async def test_runner_capability_refuses_new_but_never_blocks_receipt(api, change):
    body = research_command(api.service)
    accepted = await submit(api, body)
    assert accepted.status == 201
    if change == "absent":
        api.orch.persistent_assignment_runner = None
    elif change == "stopping":
        api.runner._stopping = True
    elif change == "handler":
        api.runner.one_shot = OneShotLifecycle(api.fixture[0], lambda _: None)
    elif change == "sessions":
        api.runner.one_shot = OneShotLifecycle(object(), run_research_episode)
    else:
        api.runner.service = object()
    api.fixture[0].delete(api.fixture[2])
    api.orch.agent_cards.clear()
    replay = await submit(api, body, bearer=True, cookie=False)
    assert replay.status == 200 and replay.value["id"] == accepted.value["id"]
    refusal = await submit(api, bearer=True, cookie=False)
    assert refusal.status == 503 and refusal.value == {
        "error": "work_submit_unavailable"
    }
    assert totals(api.runtime, api.fixture[1]) == (1, 1, 1)


@pytest.mark.parametrize(
    "change",
    ["missing", "runtime", "catalog", "assignments", "sessions", "audit", "config"],
)
async def test_wrong_production_composition_refuses_without_body_or_acceptance(
    api, change
):
    if change == "missing":
        del api.app.state.orchestrator
    elif change == "runtime":
        api.orch.runtime_composition.plane.runtime = object()
    elif change == "catalog":
        api.orch.runtime_composition.plane.repositories = object()
    else:
        setattr(
            api.orch,
            {
                "assignments": "persistent_assignments",
                "sessions": "web_sessions",
                "audit": "audit_repo",
                "config": "_llm_store",
            }[change],
            object(),
        )
    result = await submit(api)
    assert result.status == 503 and result.received == 0
    no_acceptance(api.runtime, api.fixture[1])


async def test_mounted_application_uses_actual_root_composition(api):
    del api.app.state.orchestrator
    api.app._root_app = SimpleNamespace(state=SimpleNamespace(orchestrator=api.orch))
    assert (await submit(api)).status == 201


@pytest.mark.parametrize(
    "length,status",
    [
        (b"16385", 413),
        (b"-1", 400),
        (b"+1", 400),
        (b"1,2", 400),
        (b" 2", 400),
        (b"999999999999999", 400),
    ],
)
async def test_framing_refused_before_stream_or_auth(api, length, status, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("normal auth should not run")

    monkeypatch.setattr(auth, "verify_production_token", forbidden)
    result = await submit(api, headers=[(b"content-length", length)])
    assert result.status == status and result.received == 0
    no_acceptance(api.runtime, api.fixture[1])


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"content-encoding", b"gzip")],
    ],
)
async def test_ambiguous_length_or_encoding_refuses(api, headers):
    result = await submit(api, headers=headers)
    assert result.status in {400, 415} and result.received == 0


@pytest.mark.parametrize(
    "body,length,status",
    [
        (b"x" * 16385, None, 413),
        (b"x" * 16384, None, 422),
        (b"{}", b"4", 400),
        (b"", b"0", 422),
    ],
    ids=["oversize", "exact-size-invalid-json", "length-mismatch", "empty"],
)
async def test_actual_raw_stream_bound_and_length_equality(api, body, length, status):
    result = await submit(
        api,
        chunks=[body[:100], body[100:]],
        headers=[] if length is None else [(b"content-length", length)],
    )
    assert result.status == status
    assert result.received <= 2
    no_acceptance(api.runtime, api.fixture[1])


async def test_disconnect_discards_partial_intent(api):
    result = await submit(
        api,
        messages=[
            {"type": "http.request", "body": b'{"version":1', "more_body": True},
            {"type": "http.disconnect"},
        ],
    )
    assert result.status == 400 and result.value == {"error": "work_disconnected"}
    no_acceptance(api.runtime, api.fixture[1])


async def test_receive_deadline_does_not_accept(api, monkeypatch):
    monkeypatch.setattr("orchestrator.work_admission_api._BODY_SECONDS", 0.02)

    async def held(_):
        await asyncio.Event().wait()

    result = await submit(api, receive=held)
    assert result.status == 408 and result.value == {"error": "work_body_timeout"}
    no_acceptance(api.runtime, api.fixture[1])


async def test_cancelled_receive_propagates_and_never_accepts(api):
    entered = asyncio.Event()

    async def held(_):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(submit(api, receive=held))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    no_acceptance(api.runtime, api.fixture[1])


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.invalid"},
        {"azp": "foreign"},
        {"realm_access": {"roles": []}},
        {"aud": "astral-mcp"},
        {"act": {"sub": "agent"}},
        {"exp": 1},
    ],
)
async def test_normal_institutional_policy_denies_without_leaking_or_audit_spoof(
    api, changes
):
    state = {"audit_claims": {"sub": "spoofed"}}
    result = await submit(api, bearer=True, claims=changes, state=state)
    assert result.status in {401, 403}
    assert "audit_claims" not in result.state
    assert b"synthetic" not in result.raw and result.received == 0
    no_acceptance(api.runtime, api.fixture[1])


@pytest.mark.parametrize(
    "query,status", [(b"token=secret", 403), (b"runner=true", 422)]
)
async def test_query_cannot_select_credentials_or_capabilities(api, query, status):
    result = await submit(api, query=query)
    assert (
        result.status == status and b"secret" not in result.raw and result.received == 0
    )
    no_acceptance(api.runtime, api.fixture[1])


async def test_bare_bearer_cannot_admit_new_work(api):
    result = await submit(api, bearer=True, cookie=False)
    assert result.status == 403 and result.value == {
        "error": "work_authority_unavailable"
    }
    no_acceptance(api.runtime, api.fixture[1])


async def test_origin_denied_and_explicit_bearer_keeps_existing_origin_contract(api):
    denied = await submit(api, headers=[(b"origin", b"https://other.invalid")])
    assert denied.status == 403 and denied.received == 0
    accepted = await submit(
        api, bearer=True, headers=[(b"origin", b"https://other.invalid")]
    )
    assert accepted.status == 201


async def test_opt_in_config_budget_and_key_are_mandatory(api, monkeypatch):
    low = await submit(api, research_command(api.service, tokens=1))
    assert low.status == 422 and low.value == {
        "error": "work_research_budget_insufficient"
    }
    monkeypatch.delenv("AUDIT_HMAC_SECRET")
    missing = await submit(api)
    assert missing.status == 503 and missing.value == {
        "error": "work_research_profile_unavailable"
    }
    no_acceptance(api.runtime, api.fixture[1])


@pytest.mark.parametrize(
    "failure",
    [
        AssignmentError("private-provider-secret", 403),
        RuntimeError("private-provider-secret"),
    ],
)
async def test_error_details_are_closed_and_verified_attribution_survives(
    api, monkeypatch, failure
):
    async def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr("orchestrator.work_submit.WorkSubmitService.submit", broken)
    result = await submit(api)
    assert result.status == 503 and result.value == {"error": "work_submit_unavailable"}
    assert result.state["audit_claims"]["sub"] == api.fixture[1]
    assert "delegation_subject_token" not in result.state


async def test_composition_replacement_during_body_wait_refuses(api):
    async def replace(_):
        api.app.state.orchestrator = object()
        return {
            "type": "http.request",
            "body": research_command(api.service),
            "more_body": False,
        }

    assert (await submit(api, receive=replace)).status == 503
    no_acceptance(api.runtime, api.fixture[1])


@pytest.mark.parametrize("boundary", ["prepare", "audit"])
async def test_readiness_loss_during_admission_rolls_back_task_receipt_and_audit(
    api, monkeypatch, boundary
):
    if boundary == "prepare":
        original = api.orch._llm_store.capture_user

        async def capture(*args, **kwargs):
            value = await original(*args, **kwargs)
            api.runner._stopping = True
            return value

        monkeypatch.setattr(api.orch._llm_store, "capture_user", capture)
    else:
        original = api.orch.audit_repo.insert_in_transaction

        def append(*args, **kwargs):
            value = original(*args, **kwargs)
            api.runner._stopping = True
            return value

        monkeypatch.setattr(api.orch.audit_repo, "insert_in_transaction", append)
    result = await submit(api)
    assert result.status == 503 and result.value == {"error": "work_submit_unavailable"}
    no_acceptance(api.runtime, api.fixture[1])


async def test_router_is_exported_only_and_non_post_does_not_accept(api):
    assert len(work_admission_router.routes) == 1
    assert work_admission_router.routes[0].methods == {"POST"}
    assert (await submit(api, method="GET")).status == 405
    no_acceptance(api.runtime, api.fixture[1])


@pytest.mark.parametrize("component", ["assignments", "sessions", "audit", "config"])
async def test_runtime_binding_mutation_during_config_capture_refuses(
    api, monkeypatch, component
):
    target = {
        "assignments": (api.service.store, "plane_runtime"),
        "sessions": (api.fixture[0]._sessions, "_runtime"),
        "audit": (api.service.audit._audit, "_runtime"),
        "config": (api.orch._llm_store._repository, "_runtime"),
    }
    original = api.orch._llm_store.capture_user

    async def capture(*args, **kwargs):
        value = await original(*args, **kwargs)
        monkeypatch.setattr(*target[component], object())
        return value

    monkeypatch.setattr(api.orch._llm_store, "capture_user", capture)
    result = await submit(api)
    assert result.status == (403 if component == "assignments" else 503)
    # Restore before fixture cleanup and inspect the original isolated runtime.
    monkeypatch.setattr(*target[component], api.runtime)
    no_acceptance(api.runtime, api.fixture[1])


@pytest.mark.parametrize("boundary", ["auth", "body", "config"])
async def test_swapped_runner_never_admits_after_await(api, monkeypatch, boundary):
    def swap():
        api.orch.persistent_assignment_runner = AssignmentRunner(
            api.orch, api.service.assignments
        )

    if boundary == "auth":
        original = auth.verify_production_token

        async def verify(*args, **kwargs):
            value = await original(*args, **kwargs)
            swap()
            return value

        monkeypatch.setattr(auth, "verify_production_token", verify)
    elif boundary == "config":
        original = api.orch._llm_store.capture_user

        async def capture(*args, **kwargs):
            value = await original(*args, **kwargs)
            swap()
            return value

        monkeypatch.setattr(api.orch._llm_store, "capture_user", capture)

    async def receive(_):
        if boundary == "body":
            swap()
        return {
            "type": "http.request",
            "body": research_command(api.service),
            "more_body": False,
        }

    assert (await submit(api, receive=receive)).status == 503
    no_acceptance(api.runtime, api.fixture[1])


async def test_lost_acceptance_ack_replays_once_without_new_refresh(api, monkeypatch):
    original = api.service.store.transaction

    async def lost(*args, **kwargs):
        value = await original(*args, **kwargs)
        if getattr(value, "created", False):
            raise RuntimeError("synthetic lost acknowledgement")
        return value

    monkeypatch.setattr(api.service.store, "transaction", lost)
    result = await submit(api)
    assert result.status == 200 and result.value["created"] is False
    assert totals(api.runtime, api.fixture[1]) == (1, 1, 1)
    assert len(api.fixture[-1]) == 1


async def test_cancel_after_acceptance_commit_preserves_receipt_and_no_second_action(
    api, monkeypatch
):
    committed = asyncio.Event()
    original = api.service.store.transaction

    async def held(*args, **kwargs):
        value = await original(*args, **kwargs)
        if getattr(value, "created", False):
            committed.set()
            await asyncio.Event().wait()
        return value

    monkeypatch.setattr(api.service.store, "transaction", held)
    body = research_command(api.service)
    task = asyncio.create_task(submit(api, body))
    await asyncio.wait_for(committed.wait(), 10)
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert totals(api.runtime, api.fixture[1]) == (1, 1, 1)
    api.runner._stopping = True
    replay = await submit(api, body, cookie=False, bearer=True)
    assert replay.status == 200 and replay.value["created"] is False
    assert len(api.fixture[-1]) == 1
    with api.runtime.transaction() as tx:
        assert (
            tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment_action")["n"]
            == 0
        )


async def test_outer_audit_middleware_receives_only_verified_attribution(
    api, monkeypatch
):
    from audit import middleware
    from orchestrator import web_auth

    seen = []

    async def record(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(middleware, "get_recorder", lambda: object())
    monkeypatch.setattr(middleware, "record_generic", record)
    api.app.add_middleware(middleware.AuditHTTPMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="https://app.invalid"
    ) as client:
        response = await client.post(
            "/api/work/v1/operations",
            content=research_command(api.service),
            headers={
                "content-type": "application/json",
                "origin": "https://app.invalid",
                "cookie": "astral_session=" + web_auth._sign(api.fixture[2]),
            },
        )
    assert response.status_code == 201
    assert len(seen) == 1 and seen[0]["claims"]["sub"] == api.fixture[1]
    rendered = json.dumps(seen)
    assert not any(
        text in rendered
        for text in ("synthetic", "Read one", "releases", "delegation_subject_token")
    )


async def test_guard_contract_rejects_noncallable_awaitable_and_false_without_leak(api):
    from orchestrator.work_submit import WorkSubmitService
    from tests.test_work_submit_postgres_088 import context

    with pytest.raises(TypeError):
        WorkSubmitService(
            api.service.assignments,
            api.service.audit,
            api.fixture[0],
            new_admission_check=True,
        )
    private = await context(api.fixture, api.runtime)
    coroutines = []

    async def invalid():
        raise AssertionError("must never execute an async readiness check")

    def async_guard():
        value = invalid()
        coroutines.append(value)
        return value

    for check in (async_guard, lambda: False):
        service = WorkSubmitService(
            api.service.assignments,
            api.service.audit,
            api.fixture[0],
            new_admission_check=check,
        )
        with pytest.raises(AssignmentError):
            await service.submit(private, research_command(api.service))
    assert coroutines and coroutines[0].cr_frame is None
    no_acceptance(api.runtime, api.fixture[1])


@pytest.mark.parametrize("state", ["not-started", "finished", "cancelling"])
async def test_composed_but_unsupervised_runner_refuses(api, state):
    if state == "not-started":
        api.orch.persistent_assignment_runner = AssignmentRunner(
            api.orch,
            api.service.assignments,
            one_shot=OneShotLifecycle(api.fixture[0], run_research_episode),
        )
    elif state == "finished":
        await api.runner.stop()
        api.runner._stopping = False
    else:
        api.runner._loop.cancel()
    result = await submit(api)
    assert result.status == 503 and result.value == {"error": "work_submit_unavailable"}
    no_acceptance(api.runtime, api.fixture[1])


async def test_immediately_ready_empty_frames_cannot_starve_receive_deadline(
    api, monkeypatch
):
    monkeypatch.setattr("orchestrator.work_admission_api._BODY_SECONDS", 0)
    result = await submit(api, chunks=[b""] * 20 + [research_command(api.service)])
    assert result.status == 408 and result.received == 1
    no_acceptance(api.runtime, api.fixture[1])


@pytest.mark.parametrize("component", ["assignments", "sessions", "audit", "config"])
async def test_foreign_repository_cannot_hide_behind_current_runtime(
    api, monkeypatch, component
):
    target = {
        "assignments": api.service.store,
        "sessions": api.fixture[0]._sessions,
        "audit": api.service.audit._audit,
        "config": api.orch._llm_store._repository,
    }[component]
    original = target.repository
    monkeypatch.setattr(target, "repository", object())
    result = await submit(api)
    assert result.status == 503 and result.received == 0
    monkeypatch.setattr(target, "repository", original)
    no_acceptance(api.runtime, api.fixture[1])


async def test_receipt_created_during_preflight_bypasses_new_runner_check(
    api, monkeypatch
):
    from tests.test_work_submit_postgres_088 import context

    body = research_command(api.service)
    original = api.orch._llm_store.capture_user
    inserted = []

    async def capture(*args, **kwargs):
        value = await original(*args, **kwargs)
        if not inserted:
            inserted.append(None)
            accepted = await api.service.submit(
                await context(api.fixture, api.runtime), body
            )
            inserted[0] = accepted.record.assignment_id
            api.runner._stopping = True
        return value

    monkeypatch.setattr(api.orch._llm_store, "capture_user", capture)
    result = await submit(api, body)
    assert result.status == 200 and result.value["created"] is False
    assert result.value["id"] == inserted[0] and totals(
        api.runtime, api.fixture[1]
    ) == (1, 1, 1)


@pytest.mark.parametrize(
    "change", [{"state_version": True}, {"owner_id": "foreign-owner"}]
)
async def test_response_cannot_serialize_an_unbound_result(api, monkeypatch, change):
    from dataclasses import replace
    from orchestrator.work_submit import WorkSubmissionResult
    from tests.test_work_submit_postgres_088 import context

    accepted = await api.service.submit(
        await context(api.fixture, api.runtime), research_command(api.service)
    )

    async def invalid(*args, **kwargs):
        return WorkSubmissionResult(replace(accepted.record, **change), True)

    monkeypatch.setattr("orchestrator.work_submit.WorkSubmitService.submit", invalid)
    result = await submit(api)
    assert result.status == 503 and result.value == {"error": "work_submit_unavailable"}
    assert b"foreign-owner" not in result.raw


@pytest.mark.parametrize(
    "message",
    [
        {"type": "websocket.receive"},
        {"type": "http.request", "body": "not raw bytes"},
        {"type": "http.request", "body": b"", "more_body": "false"},
    ],
)
async def test_invalid_asgi_receive_shape_has_no_interpreted_body(api, message):
    result = await submit(api, messages=[message])
    assert result.status == 400 and result.value == {"error": "work_body_invalid"}
    no_acceptance(api.runtime, api.fixture[1])
