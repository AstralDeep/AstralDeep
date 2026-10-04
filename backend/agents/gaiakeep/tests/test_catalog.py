"""Checks the pinned public catalog and rejects identity, routing and oversized tool inputs before dispatch."""

import pytest
from agents.gaiakeep import catalog


@pytest.mark.parametrize('change', [{'request_id': 'short'}, {'request_id': 'x' * 65},
                                  {'strategy': 'have', 'request_id': 'stable-request-id'}])
def test_upload_id_is_bounded_and_matches_supported_ingest_strategy(change):
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_upload_file', dict(machine_id='mine', path='p', collection_id='c',
                                                     data_base64='aA==', **change))


def test_complete_closed_catalog():
    assert len(catalog.ACTIONS) == 111
    assert sum(a.startswith('core.') for a in catalog.ACTIONS) == 90
    assert len(catalog.TOOLS) == 111
    assert catalog.FILE_ACTIONS == {'core.put', 'core.get', 'core.haveopen', 'core.have'}
    for info in catalog.TOOLS.values():
        schema = info['input_schema']
        assert schema['additionalProperties'] is False
        assert info['scope'] in {'tools:read', 'tools:write', 'tools:system'}
        if 'params' in schema['properties']:
            assert schema['properties']['params']['additionalProperties'] is False


@pytest.mark.parametrize('field', ['principal', 'audience', 'sig', 'user', 'dst_plugin', 'client_rpc_id'])
def test_protected_input(field):
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_core_whoami', {'machine_id': 'mine', 'params': {field: 'spoof'}})


@pytest.mark.parametrize('args', [
    {}, {'machine_id': 'mine', 'params': {'collection_id': 5}},
    {'machine_id': 'mine', 'params': {'collection_id': 'x', 'extra': True}},
    {'machine_id': 'mine', 'params': {'collection_id': 'x' * 70000}},
    {'machine_id': 'mine', 'params': {}, 'confirmed': True},
])
def test_invalid_parameters(args):
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_core_head', args)


def test_conservative_classification():
    assert not catalog.is_mutation('gaiakeep_core_whoami')
    assert catalog.is_mutation('gaiakeep_core_destroykey')
    assert catalog.is_mutation('gaiakeep_core_profile')
    assert catalog.is_mutation('unknown')
    assert catalog.is_mutation('gaiakeep_upload_file')
    assert not catalog.is_mutation('gaiakeep_read_file')


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'C:/secret', 'a\\b', 'a/../b', '', 'a\x00b'])
def test_paths(path):
    with pytest.raises(ValueError):
        catalog.relative_path(path)


def test_valid_bounded_nested_json_and_id():
    args = {'machine_id': 'mine', 'params': {'collection_id': 'c', 'sources': '[{"vid":"v"}]', 'request_id': 'a' * 32}}
    assert catalog.validate('gaiakeep_core_compose', args) == args


@pytest.mark.parametrize('value', ['{bad', '[' * 18 + '0' + ']' * 18, '[0,' * 10001 + '0' + ']' * 10001,
                                 '{"items":[' + ','.join(['0'] * 10001) + ']}'], ids=['syntax', 'depth', 'extreme-depth', 'entries'])
def test_bad_nested_json(value):
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_core_compose', {'machine_id': 'mine', 'params': {'collection_id': 'c', 'sources': value}})


@pytest.mark.parametrize('field,value', [('limit', '1001'), ('limit', '-1'), ('limit', 'abc')])
def test_numeric_bounds(field, value):
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_core_list', {'machine_id': 'mine', 'params': {'vid': 'v', field: value}})


@pytest.mark.parametrize('value', ['small', 'x' * 65, 'x' * 16 + '/'])
def test_bad_idempotency_identifier(value):
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_core_gc', {'machine_id': 'mine', 'params': {'request_id': value}})


def test_mutation_review_bound():
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_core_compose', {'machine_id': 'mine', 'params': {'collection_id': 'c', 'sources': 'x' * 11000}})


def test_unknown_and_large_read_arguments():
    with pytest.raises(ValueError):
        catalog.validate('unknown', {})
    catalog._bounded_json({'valid': [1, 2]})
    with pytest.raises(ValueError):
        catalog._bounded_json([0] * 10001)


def test_have_upload_target_guard_refused():
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_upload_file', {'machine_id': 'mine', 'path': 'file', 'collection_id': 'c',
                                               'data_base64': '', 'strategy': 'have', 'base_vid': 'v'})


def test_have_upload_head_guard_is_admitted():
    args = {'machine_id': 'mine', 'path': 'file', 'collection_id': 'c',
            'data_base64': '', 'strategy': 'have', 'expected_head': 'v'}
    assert catalog.validate('gaiakeep_upload_file', args) == args
