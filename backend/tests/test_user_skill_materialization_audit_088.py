"""Tests that legacy skill-catalog cutover (user_skill_catalog.py, user_skills.py)
writes exactly one content-free audit event atomically, rolling back the whole
cutover if the audit or a concurrent import fails.
"""

import asyncio
import json
from pathlib import Path
import threading

import pytest

from orchestrator.user_skills import UserSkillStore
from persistent_agents.models import AssignmentError
from tests.test_user_skill_facade_088 import (
    catalog as catalog, human as human, bound as bound, fixture as fixture,
    runtime as runtime, service as service, signing_key as signing_key,
)
from tests.test_human_request_authority_088 import selected
from tests.helpers.session_plane_runtime import get_session_record

pytestmark = pytest.mark.asyncio


def legacy_file(catalog, fixture):
    store = UserSkillStore(catalog.knowledge_dir)
    value = store.save(fixture[1], name='Private legacy title',
        instructions='Private original instructions must never enter audit.',
        applies_to='always', command='privatealias')
    path = Path(store._dir(fixture[1])) / (value.slug+'.md')
    return path, path.read_bytes()


def state(runtime, owner):
    with runtime.transaction() as tx:
        return {name:tx.fetch_one('SELECT count(*) AS n FROM '+name)['n'] for name in (
            'owner_skill_catalog', 'owner_skill_head', 'owner_skill_revision',
            'assignment_guidance_reference', 'assignment_guidance_selection', 'audit_events')}


@pytest.mark.parametrize('count', [0, 1])
async def test_first_materialization_is_audited_once_without_legacy_content(
    catalog, human, bound, fixture, runtime, monkeypatch, count,
):
    captured = legacy_file(catalog, fixture) if count else None
    caller = await selected(human, bound, fixture, method='GET')
    items = await catalog.list(caller=caller)
    events, _ = await asyncio.to_thread(catalog.audit.list_for_user, fixture[1])
    assert len(items) == count and len(events) == 1
    event = events[0]
    assert event.action_type == 'user_skill_materialize'
    with runtime.transaction() as tx:
        marker = catalog.repository.get_materialization(tx, owner_id=fixture[1])
        stored = runtime.repositories.audit.get(tx, chain_id=fixture[1], event_id=event.event_id)
        assert stored.event.chain_id == stored.event.auth_principal == fixture[1]
        assert runtime.repositories.audit.get(tx, chain_id='other-owner', event_id=event.event_id) is None
    assert event.outputs_meta == {'count':count, 'manifest_digest':marker.manifest_digest,
                                  'skill_ids':sorted(item.skill_id for item in items)}
    assert event.inputs_meta == {}
    text = json.dumps(event.model_dump(mode='json'))
    for private in ('Private legacy title','Private original instructions','privatealias','always'):
        assert private not in text
    assert await asyncio.to_thread(catalog.audit.verify_chain, fixture[1]) is None
    def unavailable(*_args, **_kwargs):
        raise AssertionError('an existing marker must not append audit')
    monkeypatch.setattr(catalog.audit, 'insert_in_transaction', unavailable)
    assert await catalog.list(caller=caller) == items
    assert state(runtime, fixture[1])['audit_events'] == 1
    if captured:
        assert captured[0].read_bytes() == captured[1]


@pytest.mark.parametrize('failure', ['before', 'after', 'invalid_result', 'awaitable', 'session', 'composition'])
async def test_materialization_audit_or_final_caller_failure_rolls_back_entire_cutover(
    catalog, human, bound, fixture, runtime, monkeypatch, failure,
):
    path, raw = legacy_file(catalog, fixture)
    caller = await selected(human, bound, fixture)
    old = get_session_record(runtime, fixture[2])
    original = catalog.audit.insert_in_transaction
    before = state(runtime, fixture[1])
    called = []
    async def invalid():
        raise AssertionError('audit must not execute an asynchronous return')
    def fault(event, *, transaction, plane_runtime):
        called.append(event.action_type)
        assert event.action_type == 'user_skill_materialize'
        assert plane_runtime is runtime
        if failure == 'before':
            raise RuntimeError('synthetic audit unavailable')
        result = original(event, transaction=transaction, plane_runtime=plane_runtime)
        if failure == 'after':
            raise RuntimeError('synthetic failure after append')
        if failure == 'invalid_result':
            return object()
        if failure == 'awaitable':
            return invalid()
        if failure == 'session':
            runtime.repositories.history.sessions.delete(transaction, owner_id=fixture[1],
                session_id=fixture[2], expected_incarnation_id=old.incarnation_id)
        else:
            human[2].audit_repo = object()
        return result
    monkeypatch.setattr(catalog.audit, 'insert_in_transaction', fault)
    try:
        with pytest.raises(AssignmentError):
            await catalog.list(caller=caller)
        assert called == ['user_skill_materialize']
        assert state(runtime, fixture[1]) == before
        assert get_session_record(runtime, fixture[2]) == old
        assert path.read_bytes() == raw
    finally:
        human[2].audit_repo = catalog.audit
    monkeypatch.setattr(catalog.audit, 'insert_in_transaction', original)
    assert len(await catalog.list(caller=await selected(human, bound, fixture))) == 1
    assert state(runtime, fixture[1]) == {**before,'owner_skill_catalog':1,
        'owner_skill_head':1,'owner_skill_revision':1,'audit_events':1}


async def test_two_real_facades_serialize_first_import_and_append_only_one_event(
    catalog, human, bound, fixture, runtime, monkeypatch,
):
    from orchestrator.user_skill_catalog import UserSkillFacade
    path, raw = legacy_file(catalog, fixture)
    other = UserSkillFacade(human[2], catalog.knowledge_dir)
    first, second = await selected(human, bound, fixture), await selected(human, bound, fixture)
    entered, release = threading.Event(), threading.Event()
    original = catalog.audit.insert_in_transaction
    def held(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result
    monkeypatch.setattr(catalog.audit, 'insert_in_transaction', held)
    one = asyncio.create_task(catalog.list(caller=first))
    two = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        two = asyncio.create_task(other.list(caller=second))
        await asyncio.sleep(.03)
        assert not two.done()
        release.set()
        values = await asyncio.gather(one, two)
        assert values[0] == values[1] and len(values[0]) == 1
        assert state(runtime, fixture[1])['audit_events'] == 1
        assert path.read_bytes() == raw
    finally:
        release.set()
        await asyncio.gather(*[task for task in (one,two) if task is not None], return_exceptions=True)


async def test_file_conflict_after_materialization_rolls_back_required_audit_too(
    catalog, human, bound, fixture, runtime, monkeypatch,
):
    path, raw = legacy_file(catalog, fixture)
    original = catalog.audit.insert_in_transaction
    def edit_after_append(*args, **kwargs):
        value = original(*args, **kwargs)
        path.write_bytes(raw+b'External edit\n')
        return value
    monkeypatch.setattr(catalog.audit, 'insert_in_transaction', edit_after_append)
    try:
        with pytest.raises(AssignmentError, match='skill_materialization_conflict'):
            await catalog.list(caller=await selected(human, bound, fixture))
        assert not any(state(runtime, fixture[1]).values())
    finally:
        path.write_bytes(raw)
