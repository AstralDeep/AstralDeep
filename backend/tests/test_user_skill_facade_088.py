
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator import user_skills
from persistent_agents.models import AssignmentError

pytestmark = pytest.mark.asyncio


def command(**changes):
    value = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0,
                 name='Weekly format', instructions='Use the exact weekly outline.',
                 applies_to='always', command='weekly', enabled=True, slug='')
    value.update(changes)
    return value


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


async def test_capture_requires_existing_root_and_missing_parent_remains_stable(tmp_path):
    from orchestrator.user_skill_catalog import capture_legacy_files
    with pytest.raises(AssignmentError, match='skill_materialization_conflict'):
        capture_legacy_files(str(tmp_path / 'missing-root'), 'synthetic-owner')
    first = capture_legacy_files(str(tmp_path), 'synthetic-owner')
    assert first.files == () and first == capture_legacy_files(str(tmp_path), 'synthetic-owner')
    (tmp_path / user_skills.SUBDIR).mkdir()
    second = capture_legacy_files(str(tmp_path), 'synthetic-owner')
    assert second.files == () and second != first


