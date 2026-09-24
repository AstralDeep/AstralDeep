"""Tests for orchestrator/turn_guidance_authority.py currentness over auth.py's IAM and
Postgres-backed skills: original guidance stays read-only, retires with its exact
turn, and never adopts a later owner's state after identity loss.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import time
from uuid import uuid4

import pytest

from orchestrator import auth, turn_guidance_authority as module
from persistent_agents.models import AssignmentError
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_human_request_authority_088 import selected
from tests.test_user_skill_facade_088 import (
    bound as bound, catalog as catalog, command, fixture as fixture, human as human,
    runtime as runtime, service as service, signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


async def origin_for(human, bound, fixture):
    caller = await selected(human, bound, fixture)
    return await module.capture_turn_guidance_from_human(caller, expected_orchestrator=human[2])


async def test_original_http_guidance_is_read_only_and_retired_with_exact_turn(
    catalog, human, bound, fixture,
):
    caller = await selected(human, bound, fixture)
    intent = command()
    await catalog.save(caller=caller, **intent)
    origin = await origin_for(human, bound, fixture)
    chat = str(uuid4())
    binding = module.bind_http_guidance(origin, expected_orchestrator=human[2], websocket=None, chat_id=chat)
    try:
        with module.use_turn_guidance(binding, expected_orchestrator=human[2]):
            reader = await module.acquire_turn_guidance_reader(
                expected_orchestrator=human[2], websocket=None, chat_id=chat)
            try:
                items = await catalog.list(caller=reader)
                assert items[0].instructions == intent['instructions']
                assert reader.runtime is caller.runtime and reader.audit_repo is caller.audit_repo
                assert reader.plane_runtime is reader.runtime and reader.owner_id == caller.owner_id
                with pytest.raises(AssignmentError, match='human_write_required'):
                    reader.require_write()
                with pytest.raises(AssignmentError, match='skill_authentication_required'):
                    await catalog.save(caller=reader, **command())
                assert fixture[1] not in repr(reader) and fixture[3]() not in repr(reader)
            finally:
                reader.close()
            with pytest.raises(AssignmentError):
                await reader.verify_delivery()
        assert binding.closed
        with pytest.raises(AssignmentError):
            module.TurnGuidanceReader(binding, expected_orchestrator=human[2])
        assert module.current_turn_guidance(expected_orchestrator=human[2], websocket=None, chat_id=chat) is None
        assert fixture[-1] == []
    finally:
        origin.close()


@pytest.mark.parametrize('change', ['chat', 'socket', 'task', 'context_exit'])
async def test_same_origin_cannot_be_reused_by_an_unrelated_turn(human, bound, fixture, change):
    origin = await origin_for(human, bound, fixture)
    chat, socket = str(uuid4()), object()
    binding = module.bind_http_guidance(origin, expected_orchestrator=human[2], websocket=socket, chat_id=chat)
    async def read():
        return await module.acquire_turn_guidance_reader(expected_orchestrator=human[2],
            websocket=object() if change == 'socket' else socket,
            chat_id=str(uuid4()) if change == 'chat' else chat)
    try:
        with module.use_turn_guidance(binding, expected_orchestrator=human[2]):
            if change != 'context_exit':
                with pytest.raises(AssignmentError):
                    if change == 'task':
                        await asyncio.create_task(read())
                    else:
                        await read()
        if change == 'context_exit':
            with pytest.raises(AssignmentError):
                await read()
    finally:
        origin.close()


@pytest.mark.parametrize('loss', ['origin', 'boundary', 'keycloak_policy', 'credential', 'jwt', 'reader_deadline'])
async def test_original_authentication_loss_never_adopts_current_owner_state(
    catalog, human, bound, fixture, runtime, monkeypatch, loss,
):
    origin = await origin_for(human, bound, fixture)
    chat = str(uuid4())
    if loss == 'jwt':
        origin = replace(origin, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        with pytest.raises(AssignmentError):
            module.bind_http_guidance(origin, expected_orchestrator=human[2], websocket=None, chat_id=chat)
        origin.close()
        return
    binding = module.bind_http_guidance(origin, expected_orchestrator=human[2], websocket=None, chat_id=chat)
    try:
        with module.use_turn_guidance(binding, expected_orchestrator=human[2]):
            reader = await module.acquire_turn_guidance_reader(
                expected_orchestrator=human[2], websocket=None, chat_id=chat)
            if loss == 'origin':
                origin.close()
            elif loss == 'boundary':
                human[1].close()
            elif loss == 'keycloak_policy':
                monkeypatch.setenv('KEYCLOAK_ALLOWED_AZP', 'different-current-client')
            elif loss == 'credential':
                replace_session_record(runtime, get_session_record(runtime, fixture[2]))
            else:
                reader._deadline = time.monotonic() - 1
            with pytest.raises(AssignmentError):
                await catalog.list(caller=reader)
            reader.close()
    finally:
        origin.close()


async def test_session_replacement_during_actual_jwt_await_suppresses_guidance(
    catalog, human, bound, fixture, runtime, monkeypatch,
):
    origin = await origin_for(human, bound, fixture)
    chat = str(uuid4())
    binding = module.bind_http_guidance(origin, expected_orchestrator=human[2], websocket=None, chat_id=chat)
    verify = auth.verify_production_token
    calls = []
    async def replace_after_iam(token):
        value = await verify(token)
        if not calls:
            calls.append(True)
            await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
        return value
    monkeypatch.setattr(auth, 'verify_production_token', replace_after_iam)
    try:
        with module.use_turn_guidance(binding, expected_orchestrator=human[2]):
            with pytest.raises(AssignmentError):
                await module.acquire_turn_guidance_reader(expected_orchestrator=human[2], websocket=None, chat_id=chat)
        with runtime.transaction() as tx:
            assert runtime.repositories.preferences.skills.get_materialization(tx, owner_id=fixture[1]) is None
    finally:
        origin.close()


async def test_read_worker_refuses_awaitables_without_leaking_the_coroutine(human, bound, fixture):
    origin = await origin_for(human, bound, fixture)
    chat = str(uuid4())
    binding = module.bind_http_guidance(origin, expected_orchestrator=human[2], websocket=None, chat_id=chat)
    async def deferred():
        raise AssertionError('must not be awaited')
    coro = deferred()
    try:
        with module.use_turn_guidance(binding, expected_orchestrator=human[2]):
            reader = await module.acquire_turn_guidance_reader(expected_orchestrator=human[2], websocket=None, chat_id=chat)
            with pytest.raises(AssignmentError):
                await reader.transaction(lambda tx, _: coro, expected_orchestrator=human[2])
            assert coro.cr_frame is None
            with pytest.raises(AssignmentError):
                await reader.transaction(None, expected_orchestrator=human[2])
            reader.close()
    finally:
        coro.close()
        origin.close()


@pytest.mark.parametrize('value', [None, 'owner', object()])
async def test_arbitrary_values_never_produce_guidance(value):
    with pytest.raises(AssignmentError):
        await module.capture_turn_guidance_from_human(value, expected_orchestrator=object())
    with pytest.raises(AssignmentError):
        module.TurnGuidanceReader(value, expected_orchestrator=object())
