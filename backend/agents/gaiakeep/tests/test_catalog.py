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
    assert len(catalog.ACTIONS) == 101
    assert sum(a.startswith('core.') for a in catalog.ACTIONS) == 99
    assert len(catalog.TOOLS) == 104
    assert catalog.REVISION == 'fbd957521b4b3b828371241465d3ba5438dbdad2'
    assert {name for name, item in catalog.ACTIONS.items() if item['legacy']} == {'getmetrics', 'getcapabilities'}
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


@pytest.mark.parametrize('name', ['gaiakeep_fetch', 'gaiakeep_restore', 'gaiakeep_listnodes',
                                  'gaiakeep_reconstructauth', 'gaiakeep_approvereconstruct'])
def test_retired_prototype_tools_are_absent(name):
    assert name not in catalog.TOOLS
    with pytest.raises(ValueError):
        catalog.validate(name, {'machine_id': 'mine', 'params': {}})


@pytest.mark.parametrize('name,scope', [('gaiakeep_core_membership', 'tools:system'), ('gaiakeep_core_quota', 'tools:system'),
                                        ('gaiakeep_core_redundancy', 'tools:system'), ('gaiakeep_core_deepattest', 'tools:system'),
                                        ('gaiakeep_core_extractattest', 'tools:write')])
def test_new_core_actions_require_approval(name, scope):
    assert catalog.TOOLS[name]['scope'] == scope and catalog.is_mutation(name)


@pytest.mark.parametrize('name,params,valid', [
    ('gaiakeep_core_quota', {'tenant_id': 't', 'op': 'set', 'stored_max': -1}, True),
    ('gaiakeep_core_quota', {'tenant_id': 't', 'stored_max': '-1'}, False),
    ('gaiakeep_core_quota', {'tenant_id': 't', 'weight': True}, False),
    ('gaiakeep_core_extract', {'vid': 'v', 'max_amplification': 2.5}, True),
    ('gaiakeep_core_extract', {'vid': 'v', 'max_amplification': 1e16}, False),
    ('gaiakeep_core_extract', {'vid': 'v', 'max_amplification': '2.5'}, False),
    ('gaiakeep_core_lclist', {'limit': 1000}, True),
    ('gaiakeep_core_lclist', {'limit': 1001}, False),
])
def test_integer_and_number_parameters_are_typed(name, params, valid):
    arguments = {'machine_id': 'mine', 'params': params}
    if valid:
        assert catalog.validate(name, arguments) is arguments
    else:
        with pytest.raises(ValueError):
            catalog.validate(name, arguments)


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


@pytest.mark.parametrize('note', ['x' * 513, '\u00e9' * 257, 'line\nbreak', 'nul\0', 'delete\x7f'])
def test_version_note_utf8_and_control_bounds(note):
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_upload_file', {'machine_id': 'm', 'collection_id': 'c', 'path': 'p',
                                                'data_base64': '', 'note': note})


def test_dataset_tool_boundaries_and_read_catalog():
    for name in ('collections', 'placement', 'jobs', 'principals'):
        assert not catalog.is_mutation('gaiakeep_core_' + name)
    assert not catalog.is_mutation('gaiakeep_inspect_dataset')
    assert catalog.is_mutation('gaiakeep_download_dataset')
    assert catalog.is_mutation('gaiakeep_upload_dataset')
    assert catalog.validate('gaiakeep_list_collections', {'machine_id': 'm', 'params': {'after': 'cursor', 'limit': 20}})
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_inspect_dataset', {'machine_id': 'm', 'dataset_ref': '../escape'})
    with pytest.raises(ValueError):
        catalog.validate('gaiakeep_upload_dataset', {'machine_id': 'm', 'dataset_ref': 'run42', 'collection_id': 'c',
                                                   'manifest_sha256': 'a' * 64, 'prefix': '../escape'})
