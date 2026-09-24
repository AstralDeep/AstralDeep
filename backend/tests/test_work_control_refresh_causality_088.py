"""Tests that an original-session refresh (work_continuation_authority.py,
work_submit_authority.py) can only advance the caller's own exact prior fence, never
adopting a same-issuance rotation from outside the causal exchange.
"""

import asyncio
from dataclasses import asdict, replace

import pytest

from orchestrator import session_authority, web_auth
from orchestrator.work_continuation_authority import refresh_operation_control_authority
from orchestrator.work_submit_authority import authenticate_work_submission_request
from tests.test_operation_session_authority_088 import create_operation
from tests.test_request_session_authority_088 import fixture as fixture
from tests.test_request_session_authority_088 import request
from tests.test_request_session_authority_088 import runtime as runtime
from tests.test_request_session_authority_088 import signing_key as signing_key
from tests.test_work_continuation_authority_088 import control, current


async def selected(fixture, runtime):
    store, owner, sid, token, _ = fixture
    context = await authenticate_work_submission_request(request(sid, headers=[
        (b"authorization", ("Bearer " + token()).encode()),
        (b"content-type", b"application/json"),
    ]), sessions=store, plane_runtime=runtime)
    credential = store.capture_execution_reference(owner_id=owner, session_id=sid).state.credential
    return context, credential


async def resolve(fixture, paused, context, credential):
    return await refresh_operation_control_authority(context=context, original=paused,
        command="resume", sessions=fixture[0], expected_request_credential=credential)


def test_refresh_binds_exact_request_and_old_credential_without_resuming(fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    async def scenario():
        context, credential = await selected(fixture, runtime)
        authority = await resolve(fixture, paused, context, credential)
        assert authority.request_context is context
        assert authority.request_credential == credential
        refreshed = authority.observation.credential
        assert refreshed != credential
        assert refreshed.incarnation_id == credential.incarnation_id
        assert refreshed.session_id == credential.session_id
        assert refreshed.encrypted_state_binding != credential.encrypted_state_binding
        assert credential.encrypted_state_binding not in repr(authority)
        assert current(runtime, paused) == paused
    asyncio.run(scenario())
    assert len(fixture[-1]) == 1


def test_external_same_issuance_rotation_before_capture_cannot_be_adopted(fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    async def scenario():
        context, credential = await selected(fixture, runtime)
        fixture[0].update_tokens(fixture[2], access_token=fixture[3](),
                                 refresh_token="synthetic-concurrent-refresh")
        changed = fixture[0].capture_execution_reference(
            owner_id=fixture[1], session_id=fixture[2]).state.credential
        assert changed.incarnation_id == credential.incarnation_id
        assert changed != credential
        with pytest.raises(session_authority.SessionAuthorityUnavailable):
            await resolve(fixture, paused, context, credential)
    asyncio.run(scenario())
    assert fixture[-1] == [] and current(runtime, paused) == paused


def test_rotation_during_remote_exchange_cannot_return_a_causal_proof(fixture, runtime, monkeypatch):
    paused = control(runtime, create_operation(fixture, runtime))
    async def scenario():
        context, credential = await selected(fixture, runtime)
        async def exchange(*args):
            fixture[-1].append("attempt")
            fixture[0].update_tokens(fixture[2], access_token=fixture[3](),
                                     refresh_token="synthetic-concurrent-refresh")
            return {"access_token": fixture[3](), "refresh_token": "synthetic-late-refresh"}
        monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
        with pytest.raises(session_authority.SessionAuthorityUnavailable):
            await resolve(fixture, paused, context, credential)
    asyncio.run(scenario())
    assert fixture[-1] == ["attempt"] and current(runtime, paused) == paused


@pytest.mark.parametrize("change", ["dict", "owner", "sid", "incarnation", "binding",
                                     "bool_version", "missing_cookie", "cookie_lineage"])
def test_private_expected_fence_is_exact_and_bound_before_exchange(fixture, runtime, change):
    paused = control(runtime, create_operation(fixture, runtime))
    async def scenario():
        context, credential = await selected(fixture, runtime)
        if change == "dict":
            credential = asdict(credential)
        elif change in {"owner", "sid", "incarnation", "binding", "bool_version"}:
            key, value = {
                "owner": ("owner_id", "other-owner"), "sid": ("session_id", "other-sid"),
                "incarnation": ("incarnation_id", "other-incarnation"),
                "binding": ("encrypted_state_binding", "other-binding"),
                "bool_version": ("version", True),
            }[change]
            credential = replace(credential, **{key: value})
        elif change == "missing_cookie":
            context = replace(context, session_id=None)
        else:
            context = replace(context, cookie_session=(credential.session_id, "other-incarnation"))
        with pytest.raises(session_authority.SessionAuthorityUnavailable,
                           match="^session_authority_unavailable$"):
            await resolve(fixture, paused, context, credential)
    asyncio.run(scenario())
    assert fixture[-1] == [] and current(runtime, paused) == paused
