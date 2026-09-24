"""Tests for the human skill command facade (user_skill_catalog.py, user_skills.py) over
real Plane: legacy-bytes cutover, catalog revise/toggle/delete, ownership checks, and
materialization rollback on conflicting writes.
"""

import importlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import user_skills
from persistent_agents.models import AssignmentError
from tests.test_human_request_authority_088 import human as human, selected
from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def catalog(human, tmp_path, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, 'user_skills', True)
    human[2].knowledge_index = SimpleNamespace(knowledge_dir=str(tmp_path))
    module = importlib.import_module('orchestrator.user_skill_catalog')
    return module.UserSkillFacade(human[2], str(tmp_path))


def command(**changes):
    value = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0,
                 name='Weekly format', instructions='Use the exact weekly outline.',
                 applies_to='always', command='weekly', enabled=True, slug='')
    value.update(changes)
    return value


async def test_empty_cutover_create_and_exact_receipt_replay(catalog, human, bound, fixture, runtime):
    caller = await selected(human, bound, fixture)
    intent = command()
    first = await catalog.save(caller=caller, **intent)
    again = await catalog.save(caller=caller, **intent)
    assert first.receipt == again.receipt and again.replayed
    assert again.revision is None
    items = await catalog.list(caller=caller)
    assert len(items) == 1 and items[0].skill_id == intent['skill_id']
    assert items[0].instructions == intent['instructions'] and items[0].revision == 1
    with runtime.transaction() as tx:
        assert runtime.repositories.preferences.skills.get_materialization(tx, owner_id=fixture[1])
        assert tx.fetch_one('SELECT count(*) AS n FROM owner_skill_revision')['n'] == 1
        assert tx.fetch_one("SELECT count(*) AS n FROM audit_events WHERE action_type='user_skill_create'")['n'] == 1
    assert fixture[-1] == []


async def test_exact_legacy_bytes_cutover_then_files_are_recovery_only(catalog, human, bound, fixture, runtime):
    legacy = user_skills.UserSkillStore(catalog.knowledge_dir)
    skill = legacy.save(fixture[1], name='Legacy format', instructions='Keep this exact original format.',
                        applies_to='always', command='legacy')
    path = Path(legacy._dir(fixture[1])) / (skill.slug + '.md')
    raw = path.read_bytes()
    caller = await selected(human, bound, fixture)
    loaded = (await catalog.list(caller=caller))[0]
    assert loaded.instructions == skill.instructions and loaded.updated_at == skill.updated_at
    assert path.read_bytes() == raw
    path.write_text('External recovery edit is never current guidance.')
    assert (await catalog.list(caller=caller))[0] == loaded
    with runtime.transaction() as tx:
        record = runtime.repositories.preferences.skills.get_revision(tx, owner_id=fixture[1],
            skill_id=loaded.skill_id, revision=1)
        assert record.legacy_markdown == raw


@pytest.mark.parametrize('caller', [None, 'owner', SimpleNamespace(owner_id='owner')])
async def test_owner_only_never_authorizes_even_empty_catalog(catalog, caller):
    with pytest.raises(AssignmentError, match='skill_authentication_required'):
        await catalog.list(caller=caller)


async def test_revise_toggle_delete_and_old_replays_never_return_old_content(catalog, human, bound, fixture, runtime):
    caller = await selected(human, bound, fixture)
    original = command()
    first = await catalog.save(caller=caller, **original)
    second = {**original, 'command_id': str(uuid4()), 'expected_revision': 1,
              'slug': first.head.slug, 'instructions': 'Use only the revised exact outline.'}
    await catalog.save(caller=caller, **second)
    toggle = dict(skill_id=first.head.skill_id, command_id=str(uuid4()), expected_revision=2, enabled=False)
    third = await catalog.set_enabled(caller=caller, **toggle)
    assert third.head.revision == 3 and not third.head.enabled
    deletion = dict(skill_id=first.head.skill_id, command_id=str(uuid4()), expected_revision=3)
    fourth = await catalog.delete(caller=caller, **deletion)
    assert fourth.head.deleted_at is not None and await catalog.list(caller=caller) == ()
    for result in (await catalog.save(caller=caller, **original),
                   await catalog.save(caller=caller, **second),
                   await catalog.set_enabled(caller=caller, **toggle),
                   await catalog.delete(caller=caller, **deletion)):
        assert result.replayed and result.revision is None and result.head == fourth.head
    with runtime.transaction() as tx:
        records = runtime.repositories.preferences.skills.history(tx, owner_id=fixture[1], skill_id=first.head.skill_id)
        assert len(records) == 4 and records[-1].definition.instructions == original['instructions']
        assert tx.fetch_one("SELECT count(*) AS n FROM audit_events WHERE action_type LIKE 'user_skill_%'")['n'] == 5
        assert tx.fetch_one("SELECT count(*) AS n FROM audit_events WHERE action_type='user_skill_materialize'")['n'] == 1
    assert 'Use only' not in repr(fourth)


async def test_revision_conflicts_alias_collision_disabled_and_reused_command(catalog, human, bound, fixture):
    caller = await selected(human, bound, fixture)
    intent = command()
    first = await catalog.save(caller=caller, **intent)
    await catalog.set_enabled(caller=caller, skill_id=first.head.skill_id,
        command_id=str(uuid4()), expected_revision=1, enabled=False)
    for bad in (command(name='Different title'),
                {**intent, 'instructions': 'Changed content on the same command identity.'},
                {**intent, 'command_id': str(uuid4()), 'expected_revision': 1, 'slug': first.head.slug}):
        with pytest.raises(AssignmentError, match='skill_conflict'):
            await catalog.save(caller=caller, **bad)
    assert len(await catalog.list(caller=caller)) == 1


async def test_read_caller_cannot_write_and_no_owner_fallback(catalog, human, bound, fixture):
    caller = await selected(human, bound, fixture, method='GET')
    assert await catalog.list(caller=caller) == ()
    with pytest.raises(AssignmentError, match='human_write_required'):
        await catalog.save(caller=caller, **command())


async def test_new_commands_reject_malformed_closed_fields_without_catalog_write(catalog, human, bound, fixture, runtime):
    caller = await selected(human, bound, fixture)
    for delta in ({'name': []}, {'name': 'x'}, {'instructions': 'short'}, {'command': 'help'},
                  {'enabled': 1}, {'expected_revision': False}, {'skill_id': 'not-a-uuid'},
                  {'applies_to': object()}, {'applies_to': ['agent']*9},
                  {'applies_to': ','.join('agent'+str(n) for n in range(9))},
                  {'slug': 'not-existing'}):
        with pytest.raises((AssignmentError, ValueError)):
            await catalog.save(caller=caller, **command(**delta))
    with pytest.raises(AssignmentError, match='skill_invalid'):
        await catalog.set_enabled(caller=caller, skill_id=str(uuid4()), command_id=str(uuid4()),
                                  expected_revision=1, enabled='false')
    with pytest.raises(AssignmentError, match='skill_not_found'):
        await catalog.set_enabled(caller=caller, skill_id=str(uuid4()), command_id=str(uuid4()),
                                  expected_revision=1, enabled=False)
    with runtime.transaction() as tx:
        assert tx.fetch_one('SELECT count(*) AS n FROM owner_skill_head')['n'] == 0


async def test_conflicting_legacy_files_roll_back_marker_and_report_no_content(catalog, human, bound, fixture, runtime):
    legacy = user_skills.UserSkillStore(catalog.knowledge_dir)
    old = legacy.save(fixture[1], name='Legacy format', instructions='Original synthetic instructions.', applies_to='always')
    path = Path(legacy._dir(fixture[1])) / (old.slug + '.md')
    path.write_text(path.read_text().replace('type: user_skill', 'type: hidden_bad_shape'))
    caller = await selected(human, bound, fixture)
    with pytest.raises(AssignmentError, match='skill_materialization_conflict') as error:
        await catalog.list(caller=caller)
    assert 'synthetic' not in str(error.value)
    with runtime.transaction() as tx:
        assert runtime.repositories.preferences.skills.get_materialization(tx, owner_id=fixture[1]) is None
        assert tx.fetch_one('SELECT count(*) AS n FROM owner_skill_head')['n'] == 0


async def test_file_change_after_real_materialization_rolls_back_and_retry_is_exact(catalog, human, bound, fixture, runtime, monkeypatch):
    legacy = user_skills.UserSkillStore(catalog.knowledge_dir)
    old = legacy.save(fixture[1], name='Legacy format', instructions='Original synthetic instructions.', applies_to='always')
    path = Path(legacy._dir(fixture[1])) / (old.slug + '.md')
    raw = path.read_bytes()
    original = catalog.repository.materialize_legacy_skills
    def changed(tx, **kwargs):
        result = original(tx, **kwargs)
        path.write_text('Concurrent external editor synthetic change.')
        return result
    monkeypatch.setattr(catalog.repository, 'materialize_legacy_skills', changed)
    caller = await selected(human, bound, fixture)
    with pytest.raises(AssignmentError, match='skill_materialization_conflict'):
        await catalog.list(caller=caller)
    with runtime.transaction() as tx:
        assert runtime.repositories.preferences.skills.get_materialization(tx, owner_id=fixture[1]) is None
        assert tx.fetch_one('SELECT count(*) AS n FROM owner_skill_revision')['n'] == 0
    path.write_bytes(raw)
    monkeypatch.setattr(catalog.repository, 'materialize_legacy_skills', original)
    assert len(await catalog.list(caller=caller)) == 1 and path.read_bytes() == raw


async def test_audit_failure_rolls_back_revision_and_replay_needs_no_new_audit(catalog, human, bound, fixture, runtime, monkeypatch):
    caller = await selected(human, bound, fixture)
    assert await catalog.list(caller=caller) == ()
    intent = command()
    original = catalog.audit.insert_in_transaction
    def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(catalog.audit, 'insert_in_transaction', unavailable)
    with pytest.raises(AssignmentError):
        await catalog.save(caller=caller, **intent)
    with runtime.transaction() as tx:
        assert tx.fetch_one('SELECT count(*) AS n FROM owner_skill_head')['n'] == 0
    monkeypatch.setattr(catalog.audit, 'insert_in_transaction', original)
    accepted = await catalog.save(caller=caller, **intent)
    monkeypatch.setattr(catalog.audit, 'insert_in_transaction', unavailable)
    replay = await catalog.save(caller=caller, **intent)
    assert replay.replayed and replay.receipt == accepted.receipt


async def test_retired_composition_after_real_audit_rolls_back_every_write(catalog, human, bound, fixture, runtime, monkeypatch):
    caller = await selected(human, bound, fixture)
    assert await catalog.list(caller=caller) == ()
    original = catalog.audit.insert_in_transaction
    def retired(*args, **kwargs):
        result = original(*args, **kwargs)
        human[2].audit_repo = object()
        return result
    monkeypatch.setattr(catalog.audit, 'insert_in_transaction', retired)
    try:
        with pytest.raises(AssignmentError, match='skill_unavailable'):
            await catalog.save(caller=caller, **command())
        with runtime.transaction() as tx:
            assert tx.fetch_one('SELECT count(*) AS n FROM owner_skill_head')['n'] == 0
            assert tx.fetch_one("SELECT count(*) AS n FROM audit_events WHERE action_type LIKE 'user_skill_%'")['n'] == 1
            assert tx.fetch_one("SELECT count(*) AS n FROM audit_events WHERE action_type='user_skill_materialize'")['n'] == 1
    finally:
        human[2].audit_repo = catalog.audit


async def test_replaced_cookie_and_foreign_runtime_refuse(catalog, human, bound, fixture, runtime):
    from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
    caller = await selected(human, bound, fixture)
    replace_session_record(runtime, get_session_record(runtime, fixture[2]))
    with pytest.raises(AssignmentError):
        await catalog.list(caller=caller)
    human[2].runtime_composition = SimpleNamespace(plane=SimpleNamespace(runtime=object(), repositories=runtime.repositories))
    with pytest.raises(AssignmentError, match='skill_unavailable'):
        await catalog.list(caller=caller)


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'hardlink', 'oversize', 'empty', 'foreign_name', 'too_many'])
async def test_bounded_regular_file_capture_refuses_unsafe_entries(tmp_path, kind):
    from orchestrator.user_skill_catalog import capture_legacy_files
    owner = 'synthetic-owner'
    directory = Path(user_skills.owner_dir(str(tmp_path), owner))
    directory.mkdir(parents=True)
    target = directory / 'valid.md'
    if kind == 'symlink':
        target.symlink_to(tmp_path / 'missing')
    elif kind == 'directory':
        target.mkdir()
    elif kind == 'hardlink':
        import os
        (tmp_path / 'source').write_text('synthetic')
        os.link(tmp_path / 'source', target)
    elif kind == 'oversize':
        target.write_bytes(b'x'*32769)
    elif kind == 'empty':
        target.write_bytes(b'')
    elif kind == 'foreign_name':
        (directory / 'unexpected.tmp').write_text('synthetic')
    else:
        for number in range(21):
            (directory / f'skill-{number}.md').write_text('synthetic')
    with pytest.raises(AssignmentError, match='skill_materialization_conflict'):
        capture_legacy_files(str(tmp_path), owner)


async def test_actual_chrome_handlers_use_current_caller_and_revision_payloads(catalog, human, bound, fixture):
    import json
    from html.parser import HTMLParser
    from orchestrator.human_request_authority import bind_human_caller
    from orchestrator.projection_surfaces import authoring
    from orchestrator import skill_packs, slash_commands
    from webrender.chrome.surfaces import _sdui
    human[2]._user_skill_store = catalog
    caller = await selected(human, bound, fixture)
    form = next(c for c in authoring._skill_form_components(None, _sdui) if c.get('submit_action'))
    identity = form['submit_payload']
    fields = dict(skill_name='Standup', skill_command='standup', skill_applies='',
                  skill_instructions='Yesterday / today / blockers, one line each.')
    with bind_human_caller(caller):
        result = await authoring._h_skill_save(human[2], object(), fixture[1], [],
                                              {**identity, 'fields': fields})
        assert 'saved' in result[2]
        skills = await authoring._skills(human[2], fixture[1])
        assert len(skills) == 1 and skills[0].public()['skill_id'] == identity['skill_id']
        skill = skills[0]
        assert '/standup' in authoring._commands_attr(skills)
        row = authoring._skill_row(skill)
        assert 'chrome_user_skill_edit' in row and 'chrome_user_skill_delete' in row
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = {}
            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == 'input' and values.get('type') == 'hidden':
                    self.values[values['name']] = values['value']
        inputs = Inputs()
        inputs.feed(authoring._skill_form(skill))
        assert inputs.values['expected_revision'] == '1'
        assert authoring._skill_request_identity({}, inputs.values)['skill_id'] == skill.skill_id
        assert (await authoring._h_skill_edit(human[2], object(), fixture[1], [],
                                             {'slug': skill.slug}))[1] == {'skill_slug': skill.slug}
        native = authoring._skill_components(skill, _sdui)
        assert 'expected_revision' in json.dumps(native)
        index = SimpleNamespace(get_techniques_for_agent=lambda _: 'Preserved authored pack.')
        digest = skill_packs.build_skill_digest(index, ['agent'], user_skills=skills)
        assert 'Your skill: Standup' in digest and 'Preserved authored pack' in digest
        assert 'one line each' in slash_commands.expand_message('/standup current request', {'standup': skill})
        toggle = {**authoring._skill_identity(skill), 'enabled': False}
        assert 'saved' in (await authoring._h_skill_toggle(human[2], object(), fixture[1], [], toggle))[2]
        disabled = (await authoring._skills(human[2], fixture[1]))[0]
        assert authoring._commands_attr([disabled]) == '[]'
        assert '### Your skill:' not in skill_packs.build_skill_digest(index, ['agent'], user_skills=[disabled])
        assert 'deleted' in (await authoring._h_skill_delete(human[2], object(), fixture[1], [],
                                   authoring._skill_identity(disabled)))[2]
        assert await authoring._skills(human[2], fixture[1]) == []
    with pytest.raises(AssignmentError, match='skill_authentication_required'):
        await authoring._skills(human[2], fixture[1])


async def test_real_commands_http_uses_same_catalog_and_normal_iam(catalog, human, bound, fixture):
    import httpx
    from orchestrator.api import chrome_router
    caller = await selected(human, bound, fixture)
    await catalog.save(caller=caller, **command())
    human[2]._user_skill_store = catalog
    bound[1].include_router(chrome_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bound[1]), base_url='https://app.invalid') as client:
        response = await client.get('/api/chrome/commands', headers={'Authorization': 'Bearer '+fixture[3]()})
        assert response.status_code == 200
        values = response.json()['commands']
        assert {'name': '/weekly', 'desc': 'Weekly format', 'mine': True} in values
        assert any(value['name'] == '/help' and not value['mine'] for value in values)
        assert (await client.get('/api/chrome/commands')).status_code in {401,403}


async def test_skill_flag_off_is_inert_for_facade_and_shared_views(human, monkeypatch):
    from shared.feature_flags import flags
    from orchestrator.projection_surfaces import authoring
    monkeypatch.setitem(flags._flags, 'user_skills', False)
    assert user_skills.store_for(human[2]) is None
    assert await authoring._skills(human[2], 'owner') == []
    for handler in (authoring._h_skill_save, authoring._h_skill_toggle,
                    authoring._h_skill_delete, authoring._h_skill_edit):
        assert 'not enabled' in (await handler(human[2], object(), 'owner', [], {}))[2]


@pytest.mark.parametrize('payload', [None, {}, {'skill_id':'bad','command_id':str(uuid4()),'expected_revision':0},
    {'skill_id':str(uuid4()),'command_id':str(uuid4()),'expected_revision':True},
    {'skill_id':str(uuid4()),'command_id':str(uuid4()),'expected_revision':'01'}])
async def test_shared_skill_write_identity_refuses_missing_or_noncanonical(payload):
    from orchestrator.projection_surfaces import authoring
    with pytest.raises(AssignmentError, match='skill_invalid'):
        authoring._skill_request_identity(payload)


async def test_collected_form_identity_cannot_conflict_with_rendered_payload():
    from orchestrator.projection_surfaces import authoring
    payload = {'skill_id':str(uuid4()),'command_id':str(uuid4()),'expected_revision':1}
    with pytest.raises(AssignmentError, match='skill_invalid'):
        authoring._skill_request_identity(payload, {'skill_id':str(uuid4())})


async def test_serialized_chat_safe_cleanup_preserves_required_read_refusal():
    from unittest.mock import AsyncMock
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.user_skill_catalog import SkillCatalogError
    error = SkillCatalogError('skill_lookup_unavailable', 503)
    orch = SimpleNamespace(_chat_locks={}, _workspace_locks={}, _chat_recorders={},
        handle_chat_message=AsyncMock(side_effect=error), _safe_send=AsyncMock(), send_ui_render=AsyncMock())
    with pytest.raises(SkillCatalogError) as caught:
        await Orchestrator._serialized_chat(orch, object(), 'synthetic', 'synthetic-chat', None,
                                            user_id='synthetic-owner')
    assert caught.value is error and orch._safe_send.await_count == orch.send_ui_render.await_count == 1
    assert orch._chat_recorders == {}


async def test_revision_changed_during_delivery_wait_cannot_return_stale_guidance(
    catalog, human, bound, fixture, runtime, monkeypatch,
):
    from orchestrator.human_request_authority import CurrentHumanCaller
    from astralplane.repositories.guidance_models import SkillCommand
    writer = await selected(human, bound, fixture)
    first = await catalog.save(caller=writer, **command())
    reader = await selected(human, bound, fixture, method='GET', cookie=False)
    original = CurrentHumanCaller.verify_delivery
    async def retire_after_verification(self):
        await original(self)
        if self is reader:
            with runtime.transaction() as tx:
                catalog.repository.apply_change(tx, command=SkillCommand(fixture[1], first.head.skill_id,
                    str(uuid4()), 'delete', 1))
    monkeypatch.setattr(CurrentHumanCaller, 'verify_delivery', retire_after_verification)
    with pytest.raises(AssignmentError, match='skill_conflict'):
        await catalog.list(caller=reader)


async def test_disappearing_observed_owner_directory_is_not_empty_materialization(
    catalog, human, bound, fixture, runtime, monkeypatch,
):
    import os
    legacy = user_skills.UserSkillStore(catalog.knowledge_dir)
    legacy.save(fixture[1], name='Legacy format', instructions='Original synthetic instructions.', applies_to='always')
    directory = Path(legacy._dir(fixture[1]))
    moved = directory.with_name(directory.name + '-moved')
    original = os.open
    def renamed(path, flags, *args, **kwargs):
        if path == directory.name and directory.exists():
            directory.rename(moved)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', renamed)
    try:
        with pytest.raises(AssignmentError, match='skill_materialization_conflict'):
            await catalog.list(caller=await selected(human, bound, fixture))
        with runtime.transaction() as tx:
            assert catalog.repository.get_materialization(tx, owner_id=fixture[1]) is None
    finally:
        if moved.exists():
            moved.rename(directory)


async def test_store_factory_cannot_reuse_a_legacy_writer_or_replaced_runtime(
    catalog, human, bound, fixture, monkeypatch, tmp_path,
):
    from orchestrator import knowledge_synthesis
    legacy = user_skills.UserSkillStore(str(tmp_path))
    human[2]._user_skill_store = legacy
    actual = user_skills.store_for(human[2])
    assert type(actual) is type(catalog) and actual is user_skills.store_for(human[2])
    assert actual is not legacy
    caller = await selected(human, bound, fixture)
    await actual.save(caller=caller, **command())
    assert 'Use the exact' not in repr((await actual.list(caller=caller))[0])
    human[2]._user_skill_store = None
    human[2].knowledge_index = None
    monkeypatch.setattr(knowledge_synthesis, 'DEFAULT_KNOWLEDGE_DIR', str(tmp_path))
    assert user_skills.store_for(human[2]).knowledge_dir == str(tmp_path)
    with pytest.raises(AssignmentError, match='skill_unavailable'):
        user_skills.store_for(SimpleNamespace())


async def test_capture_requires_existing_root_and_missing_parent_remains_stable(tmp_path):
    from orchestrator.user_skill_catalog import capture_legacy_files
    with pytest.raises(AssignmentError, match='skill_materialization_conflict'):
        capture_legacy_files(str(tmp_path / 'missing-root'), 'synthetic-owner')
    first = capture_legacy_files(str(tmp_path), 'synthetic-owner')
    assert first.files == () and first == capture_legacy_files(str(tmp_path), 'synthetic-owner')
    (tmp_path / user_skills.SUBDIR).mkdir()
    second = capture_legacy_files(str(tmp_path), 'synthetic-owner')
    assert second.files == () and second != first


async def test_maximum_legacy_catalog_is_preserved_and_rejects_twenty_first(catalog, human, bound, fixture):
    legacy = user_skills.UserSkillStore(catalog.knowledge_dir)
    for index in range(20):
        legacy.save(fixture[1], name=f'Legacy skill {index:02}', instructions='Exact bounded synthetic standing guidance.',
                    applies_to='always', command=f'legacy{index}')
    caller = await selected(human, bound, fixture)
    assert len(await catalog.list(caller=caller)) == 20
    with pytest.raises(AssignmentError, match='skill_conflict'):
        await catalog.save(caller=caller, **command())
    assert len(list(Path(legacy._dir(fixture[1])).iterdir())) == 20
