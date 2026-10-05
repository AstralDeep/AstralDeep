"""Exercises Gaia's display-only decoded summaries, typed cells and deterministic preview limits against the pinned primitives and renderer."""

import copy
import json

import pytest
from agents.gaiakeep import presentation as ui
from webrender.renderer import render_one


def components(value):
    if isinstance(value, dict):
        if 'type' in value:
            yield value
        for key in ('content', 'children'):
            yield from components(value.get(key))
    elif isinstance(value, list):
        for item in value:
            yield from components(item)


def tables(view):
    return [item for item in components(view) if item['type'] == 'table']


def cells(view):
    return [value for table in tables(view) for row in table['rows'] for value in row]


def notices(view):
    return ' '.join(item['message'] for item in components(view) if item['type'] == 'alert')


def test_native_core_status_decodes_nested_json_and_preserves_typed_peer_rows_and_raw_data():
    payload = {'status': '10', 'core_status': json.dumps(json.dumps({
        'leader_serving': 'false',
        'raft': json.dumps({'role': 'LEADER', 'term': '36', 'commit': '11073', 'applied': '11073',
                           'last': '11076', 'durable': '11076',
                           'followers': {'siteb': {'voter': 'true', 'next': '11077', 'match': '11076'}}}),
        'sites': json.dumps([{'id': 'sitea', 'up': 'true', 'durable': 'false', 'write_mbps': '1.25'}]),
    }))}
    original = copy.deepcopy(payload)
    view = ui.result_components('gaiakeep_core_status', payload)
    assert payload == original
    assert view[0]['content'][0]['label'] == 'Leader not serving'
    assert view[0]['content'][0]['variant'] == 'warning'
    metrics = [item for item in components(view) if item['type'] == 'metric']
    assert [(item['title'], item['value']) for item in metrics] == [
        ('Term', '36'), ('Commit', '11,073'), ('Applied', '11,073'), ('Last', '11,076')]
    assert any(value is True for value in cells(view))
    assert any(value is False for value in cells(view))
    assert any(type(value) is int and value == 11077 for value in cells(view))
    assert any(type(value) is float and value == 1.25 for value in cells(view))
    assert 'sitea' in cells(view) and 'siteb' in cells(view)
    assert all(item['type'] != 'code' for item in components(view))


@pytest.mark.parametrize('key,records,expected', [
    ('jobs', [{'job_id': '001', 'state': 'DONE', 'progress': {'completed': '2', 'total': '4'}}], ['001', 'DONE', 2, 4]),
    ('collections', [{'collection_id': 'c1', 'head': {'vid': '12'}, 'count': '3'}], ['c1', '12', 3]),
    ('versions', [{'vid': '002', 'branch': 'main', 'file_count': '3', 'sealed': 'false'}], ['002', 'main', 3, False]),
    ('files', [{'path': '["metrics"]', 'size': '164', 'sha256': 'a' * 64}], ['["metrics"]', 164, 'a' * 64]),
])
def test_encoded_record_pages_become_useful_typed_tables(key, records, expected):
    result = {'data': json.dumps({key: json.dumps(records), 'count': str(len(records))})}
    original = copy.deepcopy(result)
    view = ui.result_components('gaiakeep_core_' + key, result)
    assert result == original and len(tables(view)) == 1
    for value in expected:
        assert any(type(cell) is type(value) and cell == value for cell in cells(view))
    assert all(isinstance(header, str) for header in tables(view)[0]['headers'])


def test_map_record_identity_never_overwrites_native_names_or_collision_fields():
    payload = {'collections': {'remote-map-id': {'name': 'Original dataset', 'map_key': 'original key',
                                               'map_key_2': 'second key', 'count': '2'}}}
    original = copy.deepcopy(payload)
    view = ui.result_components('gaiakeep_list_collections', payload)
    assert payload == original
    assert {'remote-map-id', 'Original dataset', 'original key', 'second key', 2}.issubset(set(cells(view)))
    assert tables(view)[0]['headers'][0] == 'Map key 3'


def test_single_job_progress_and_result_summary_use_existing_primitives():
    result = {'job_id': 'job-42', 'state': 'DONE', 'progress': json.dumps({'completed': '2', 'total': '4'}),
              'result': json.dumps({'file_count': '2', 'total_bytes': '164'})}
    view = ui.result_components('gaiakeep_core_job', result)
    assert view[0]['content'][0]['label'] == 'Done' and view[0]['content'][0]['variant'] == 'success'
    assert any(item['type'] == 'keyvalue' and item.get('title') == 'Progress' for item in components(view))
    assert len([item for item in components(view) if item['type'] == 'metric']) == 2


@pytest.mark.parametrize('state,variant', [('FAILED', 'error'), ('ERROR', 'error'), ('PUBLISHED', 'success'),
                                         ('VERIFIED', 'success'), ('RUNNING', 'info'), ('', 'info'), (None, 'info')])
def test_native_state_badge_never_invents_completion(state, variant):
    view = ui.result_components('gaiakeep_core_job', {'state': state})
    assert view[0]['content'][0]['variant'] == variant


def test_pending_summary_preserves_reconciliation_and_redacts_literal_and_encoded_secrets():
    secret = 'known-secret'
    pem = '-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----'
    result = {'path': '["metrics"]', 'note': pem, 'certificate_verified': True,
              'data_base64': 'binary-private-value', 'certificate_signature': 'private-proof-value',
              'certificate_utf8_base64': 'opaque-certificate-bytes',
              'certificate_public_key': 'opaque-certificate-key', 'proof_base64': 'opaque-proof-bytes',
              'encoded': json.dumps(json.dumps({'password': 'hidden-password', 'service_key': secret,
                                               'message': '\\u006bnown-secret', 'values': [secret]}))}
    reconciliation = {'request_id': 'request_known-secret_identity', 'vid': '123', 'commit_job': 'job-42'}
    original, references = copy.deepcopy(result), copy.deepcopy(reconciliation)
    view = ui.result_components('gaiakeep_upload_dataset', result, pending=True,
                                reconciliation=reconciliation, secrets=(secret,))
    encoded = json.dumps(view)
    assert result == original and reconciliation == references
    assert view[0]['title'] == 'GaiaKeep version awaiting durable copies'
    assert view[0]['content'][0]['variant'] == 'warning'
    assert all(value not in encoded for value in (secret, 'private-material', 'hidden-password',
                                                'binary-private-value', 'private-proof-value',
                                                'opaque-certificate-bytes', 'opaque-certificate-key', 'opaque-proof-bytes'))
    assert '[redacted]' in encoded and 'certificate_verified' not in encoded
    assert 'Certificate verified' in encoded and '123' in encoded and '[\\"metrics\\"]' in encoded


def test_unicode_escaped_secret_is_cleaned_after_decoding_every_layer():
    encoded = json.dumps('{"values":["\\u006bnown-secret"]}')
    view = ui.result_components('gaiakeep_core_job', {'payload': encoded}, secrets=('known-secret',))
    assert 'known-secret' not in json.dumps(view) and '[redacted]' in json.dumps(view)


@pytest.mark.parametrize('value', [{}, [], [{}], [{'password': 'hidden'}], None, '', 0, False, 1.25])
def test_empty_or_scalar_values_have_honest_explicit_views(value):
    view = ui.result_components('gaiakeep_core_job', {'data': value})
    assert view[0]['content'][0]['label'] == 'Response received'
    assert not notices(view) and all(table['headers'] for table in tables(view))
    encoded = json.dumps(view)
    if value in ({}, [{}], [{'password': 'hidden'}]):
        assert 'no displayable fields' in encoded
    if value is None:
        assert 'Not provided' in encoded


def test_mixed_list_values_keep_boolean_numeric_null_and_complex_type_dispositions():
    view = ui.result_components('gaiakeep_core_job', {'values': [True, 7, 1.25, None, 'word', {'a': 1}, [False]]})
    table, = tables(view)
    assert table['headers'] == ['Item', 'Value', 'Type']
    assert table['rows'][:5] == [[1, True, 'boolean'], [2, 7, 'number'], [3, 1.25, 'number'],
                               [4, 'Not provided', 'null'], [5, 'word', 'text']]
    assert 'Nested cells are summarized' in notices(view)


@pytest.mark.parametrize('value', ['{broken', '[1,', '"unterminated', '{"v":NaN}', '{"v":Infinity}',
                                  '1e10000', json.dumps({'x': 'x' * ui.MAX_JSON_TEXT}), '[' * 9 + '0' + ']' * 9],
                         ids=['object', 'list', 'string', 'nan', 'infinity', 'overflow', 'oversize', 'deep'])
def test_invalid_or_excessive_encoded_json_is_bounded_and_not_reported_as_zero(value):
    result = {'data': value}
    original = copy.deepcopy(result)
    view = ui.result_components('gaiakeep_core_job', result)
    assert result == original and notices(view)
    assert 'Display omitted' in json.dumps(view) or 'display omitted' in json.dumps(view)
    assert len(json.dumps(view).encode()) < 5000
    assert all(item['type'] != 'metric' for item in components(view))


def test_total_encoded_text_and_native_depth_entry_limits_emit_clear_notices():
    result = {'payload_' + str(index): json.dumps(['x' * 33000]) for index in range(5)}
    view = ui.result_components('gaiakeep_core_status', result)
    assert 'display size or nesting limit' in notices(view)
    nested = {'x': 1}
    for _ in range(ui.MAX_DEPTH + 2):
        nested = {'data': nested}
    assert 'display depth or entry limit' in notices(ui.result_components('gaiakeep_core_status', nested))
    assert 'display depth or entry limit' in notices(ui.result_components('gaiakeep_core_jobs', {'jobs': list(range(ui.MAX_NODES + 2))}))
    assert isinstance(ui._decode({'a': 1, 'b': 2}, '', ui._Budget(nodes=2), ()), dict)
    assert isinstance(ui._decode(None, '', ui._Budget(nodes=0), ()), ui._Omitted)
    encoded = 'true'
    for _ in range(ui.MAX_ENCODED_LAYERS):
        encoded = json.dumps(encoded)
    assert 'display size or nesting limit' in notices(ui.result_components('gaiakeep_core_status', {'data': encoded}))


def test_rows_columns_sections_and_aggregate_text_stay_bounded_with_explicit_preview_notice():
    rows = [{f'column_{column}': '界' * 600 for column in range(ui.MAX_COLUMNS + 3)}
            for _ in range(ui.MAX_ROWS + 20)]
    view = ui.result_components('gaiakeep_core_jobs', {'jobs': rows})
    table, = tables(view)
    assert len(table['rows']) == ui.MAX_ROWS and len(table['headers']) == ui.MAX_COLUMNS
    assert 'at most 50 rows' in notices(view) and 'columns are omitted' in notices(view)
    assert 'total display text limit' in notices(view) and 'shortened' in notices(view)
    assert sum(len(str(cell).encode()) for cell in cells(view)) <= ui.MAX_VISIBLE_TEXT
    sections = {f'group_{index}': [index] for index in range(ui.MAX_COMPONENTS + 5)}
    limited = ui.result_components('gaiakeep_core_status', sections)
    assert 'sections are omitted' in notices(limited)
    assert len(limited[0]['content']) <= ui.MAX_COMPONENTS + 2


def test_nonfinite_unsupported_and_omitted_values_fall_back_without_render_exceptions():
    view = ui.result_components('gaiakeep_core_status', {'nan': float('nan'), 'unsupported': object()})
    assert 'non-finite' in notices(view) and 'unsupported' in notices(view)
    assert ui._cell(ui._Omitted('hidden'), ui._Budget()) == 'Display omitted'
    assert ui._cell('bounded', ui._Budget(visible_text=0)) == ''
    assert ui._kind([1, True]) == 'mixed'
    assert ui._label('') == 'Value'
    target = []
    budget = ui._Budget(components=0)
    ui._append(target, ui.Text(content='bounded'), budget)
    assert not target and budget.notices


def test_json_depth_ignores_quoted_braces_and_escaped_quotes():
    assert ui._json_depth(json.dumps({'text': '\\"{[}', 'values': [1]})) == 2


def test_untrusted_json_stays_data_and_real_renderer_escapes_keys_values_and_urls():
    result = {'data': json.dumps([{'type': 'button', 'action': 'remove_everything',
                                 '[click](javascript:alert(1))': '<script>alert(1)</script>',
                                 'value': '<img src=x onerror=alert(1)>'}])}
    view = ui.result_components('gaiakeep_core_jobs', result)
    assert all(item['type'] != 'button' for item in components(view))
    html = render_one(view[0])
    assert '<script>' not in html and '<img src=x' not in html and 'href="javascript:' not in html
    assert '&lt;script&gt;' in html and '&lt;img' in html
