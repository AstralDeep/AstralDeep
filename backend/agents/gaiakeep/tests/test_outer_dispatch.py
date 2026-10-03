"""Verifies Gaia's outer dispatch classification, immutable approval identities and single physical attempts. Synthetic callbacks exercise failure logging and cancellation without remote services."""

import asyncio
import copy
import json
import threading
from unittest.mock import AsyncMock

import pytest

from agents.gaiakeep import catalog
from orchestrator import gaiakeep_dispatch as dispatch
from shared.protocol import MCPResponse

UPLOAD = 'gaiakeep_upload_file'
READ = 'gaiakeep_core_whoami'
REPAIR = 'gaiakeep_core_repair'
NATIVE_ID = 'approved_native_request_1234'


def public(name=UPLOAD, **changes):
    arguments = ({'machine_id': 'owned-machine', 'collection_id': 'scratch', 'path': 'file.txt',
                  'data_base64': '', 'request_id': NATIVE_ID}
                 if name == UPLOAD else {'machine_id': 'owned-machine', 'params': {}})
    if name == REPAIR:
        arguments['params'] = {'request_id': NATIVE_ID}
    arguments.update(changes)
    return arguments


@pytest.fixture
def records(monkeypatch):
    events = []
    main_thread = threading.get_ident()

    class Log:
        def record(self, name, verdict, **state):
            assert threading.get_ident() != main_thread
            events.append((name, verdict, state))

    monkeypatch.setattr(dispatch, 'IssueLog', Log)
    return events


def test_timeout_exceeds_inner_exchange_bound():
    assert dispatch.DEFAULT_TIMEOUT == dispatch.FLOW_TIMEOUT + 10


@pytest.mark.parametrize('name', [UPLOAD, REPAIR])
def test_preparation_generates_valid_id_without_input_mutation(name):
    arguments = public(name)
    (arguments if name == UPLOAD else arguments['params']).pop('request_id')
    before = copy.deepcopy(arguments)
    retained = dispatch.prepare_reconciliation(name, arguments)
    assert arguments == before
    assert len(retained['request_id']) == 32 and all(c in '0123456789abcdef' for c in retained['request_id'])
    prepared = copy.deepcopy(arguments)
    (prepared if name == UPLOAD else prepared['params'])['request_id'] = retained['request_id']
    catalog.validate(name, prepared)


@pytest.mark.parametrize('name', [UPLOAD, REPAIR])
def test_preparation_preserves_explicit_approved_id(name):
    arguments = public(name)
    before = copy.deepcopy(arguments)
    retained = dispatch.prepare_reconciliation(name, arguments)
    assert retained['request_id'] == NATIVE_ID and arguments == before


def test_preparation_preserves_other_approved_core_parameters():
    name = REPAIR
    arguments = {'machine_id': 'owned-machine', 'params': {'wait_ms': 12000}}
    catalog.validate(name, arguments)
    before = copy.deepcopy(arguments)
    retained = dispatch.prepare_reconciliation(name, arguments)
    assert arguments == before
    assert set(retained) == {'request_id'}


@pytest.mark.parametrize('name', [READ, 'gaiakeep_connection_info', 'gaiakeep_list_collections', 'gaiakeep_core_profile'])
def test_preparation_does_not_add_unsupported_ids(name):
    arguments = public(name)
    before = copy.deepcopy(arguments)
    assert dispatch.prepare_reconciliation(name, arguments) == {}
    assert arguments == before


def test_deduplication_upload_retains_fields_without_request_id():
    arguments = public(strategy='have')
    arguments.pop('request_id')
    assert dispatch.prepare_reconciliation(UPLOAD, arguments) == {
        'collection_id': 'scratch', 'path': 'file.txt', 'branch': 'main'}


@pytest.mark.parametrize('name', [UPLOAD, REPAIR])
@pytest.mark.parametrize('identifier', ['', 'short', 'bad/native/request', True, 'x' * 65])
def test_preparation_rejects_invalid_explicit_id(name, identifier):
    arguments = public(name)
    (arguments if name == UPLOAD else arguments['params'])['request_id'] = identifier
    with pytest.raises(ValueError):
        dispatch.prepare_reconciliation(name, arguments)


def test_preparation_revalidates_generated_identifier(monkeypatch):
    arguments = public()
    arguments.pop('request_id')
    monkeypatch.setattr(dispatch.uuid, 'uuid4', lambda: type('InvalidID', (), {'hex': 'invalid/id'})())
    with pytest.raises(ValueError):
        dispatch.prepare_reconciliation(UPLOAD, arguments)


@pytest.mark.asyncio
async def test_success_is_unchanged_and_not_logged(records):
    arguments = public(user_id='owner', _delegation_token='private')
    before = copy.deepcopy(arguments)
    response = MCPResponse(result={'verdict': 'ok', 'result': {'vid': 'native-version'}})
    callback = AsyncMock(return_value=response)
    assert await dispatch.invoke(UPLOAD, arguments, callback) is response
    callback.assert_awaited_once_with()
    assert arguments == before and records == []


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', sorted(dispatch.VERDICTS))
async def test_mcp_classified_failure_preserved_without_duplicate_log(records, verdict):
    response = MCPResponse(error={'message': 'MCP safe error', 'retryable': True,
                                  'data': {'verdict': verdict, 'reconciliation': {'request_id': NATIVE_ID}}})
    callback = AsyncMock(return_value=response)
    returned = await dispatch.invoke(UPLOAD, public(), callback)
    assert returned is response and returned.error['data']['verdict'] == verdict
    assert returned.error['data']['reconciliation']['request_id'] == NATIVE_ID
    assert returned.error['retryable'] is False and records == []
    callback.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize('name,verdict', [(UPLOAD, 'unconfirmed'), (REPAIR, 'unconfirmed'), (READ, 'unavailable')])
@pytest.mark.parametrize('kind', ['none', 'timeout', 'crash', 'unknown_error', 'malformed_error',
                                 'bad_verdict', 'dict_verdict', 'unknown_verdict', 'dict'])
async def test_outer_failures_are_safe_and_attempted_once(records, name, verdict, kind):
    response = {'none': None,
                'unknown_error': MCPResponse(request_id='wire-request', error={'message': 'private raw remote message', 'retryable': True}),
                'bad_verdict': MCPResponse(error={'data': {'verdict': []}}),
                'dict_verdict': MCPResponse(error={'data': {'verdict': {}}}),
                'unknown_verdict': MCPResponse(error={'data': {'verdict': 'unknown'}}),
                'dict': {'raw': 'private raw result'}}.get(kind)
    if kind == 'malformed_error':
        response = MCPResponse(error={})
        response.error = 'private raw error'
    callback = AsyncMock(return_value=response)
    if kind in {'timeout', 'crash'}:
        callback.side_effect = (TimeoutError if kind == 'timeout' else RuntimeError)('private raw exception')
    arguments = public(name)
    before = copy.deepcopy(arguments)
    result = await dispatch.invoke(name, arguments, callback)
    assert result.error['data']['verdict'] == verdict and result.error['retryable'] is False
    if name in {UPLOAD, REPAIR}:
        assert result.error['data']['reconciliation']['request_id'] == NATIVE_ID
    assert records == [(name, verdict, {'dispatched': True, 'mutation': name != READ})]
    assert 'private raw' not in json.dumps(result.error) + str(records)
    assert arguments == before
    callback.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize('code,classification', [(code, 'protocol_error') for code in dispatch.PROTOCOL_REFUSALS]
                         + [('required_identity_unavailable', 'auth_failed'), ('protected_executor_failed', 'auth_failed'),
                            ('missing_protected_permit', 'auth_failed'), ('invalid_protected_permit', 'auth_failed'),
                            ('invalid_protected_context', 'auth_failed'), ('unexpected_protected_permit', 'auth_failed'),
                            ('executor_host_binding_mismatch', 'auth_failed'), ('receipt_replayed', 'auth_failed'),
                            ('trust_manifest_invalid', 'auth_failed')])
async def test_authoritative_pre_mcp_refusal_preserved_and_logged_before_dispatch(records, code, classification):
    response = MCPResponse(error={'code': code, 'message': 'Fixed authorization refusal', 'retryable': False})
    before = copy.deepcopy(response.error)
    callback = AsyncMock(return_value=response)
    assert await dispatch.invoke(UPLOAD, public(), callback) is response
    assert response.error == before
    assert records == [(UPLOAD, classification, {'dispatched': False, 'mutation': True})]
    callback.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [None, 'different_authoritative_refusal'])
async def test_unclassified_nonretryable_refusal_preserved_without_guessed_gaia_state(records, code):
    error = {'message': 'Fixed authoritative refusal', 'retryable': False}
    if code is not None:
        error['code'] = code
    response = MCPResponse(error=error)
    callback = AsyncMock(return_value=response)
    assert await dispatch.invoke(UPLOAD, public(), callback) is response
    assert records == [] and response.error == error
    callback.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [-32603, [], {}])
async def test_generic_shape_failure_or_malformed_code_is_conservatively_unconfirmed(records, code):
    callback = AsyncMock(return_value=MCPResponse(error={'code': code, 'message': 'private shape failure', 'retryable': False}))
    result = await dispatch.invoke(UPLOAD, public(), callback)
    assert result.error['data']['verdict'] == 'unconfirmed'
    assert records == [(UPLOAD, 'unconfirmed', {'dispatched': True, 'mutation': True})]
    assert 'private shape' not in json.dumps(result.error)
    callback.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize('request_id', [None, False, {}, 'wire-request'])
async def test_outer_failure_request_id_has_protocol_string_shape(records, request_id):
    response = MCPResponse(error={'message': 'fixed', 'retryable': True})
    response.request_id = request_id
    result = await dispatch.invoke(READ, public(READ), AsyncMock(return_value=response))
    assert result.request_id == (request_id if isinstance(request_id, str) else '')
    result.validate_result_shape()


@pytest.mark.asyncio
async def test_approved_upload_fields_survive_unconfirmed_timeout(records):
    arguments = public(branch='qualification', base_vid='old-version', expected_head='old-head')
    callback = AsyncMock(side_effect=TimeoutError('private'))
    result = await dispatch.invoke(UPLOAD, arguments, callback)
    assert result.error['data']['reconciliation'] == {
        'collection_id': 'scratch', 'path': 'file.txt', 'branch': 'qualification', 'request_id': NATIVE_ID}
    assert records[-1][1] == 'unconfirmed'


@pytest.mark.asyncio
@pytest.mark.parametrize('name', [UPLOAD, REPAIR])
async def test_missing_supported_id_refuses_before_physical_callback(records, monkeypatch, name):
    arguments = public(name)
    (arguments if name == UPLOAD else arguments['params']).pop('request_id')
    before = copy.deepcopy(arguments)
    monkeypatch.setattr(dispatch.uuid, 'uuid4', lambda: pytest.fail('identifier generated after authorization'))
    callback = AsyncMock(return_value=MCPResponse(result={'verdict': 'ok'}))
    result = await dispatch.invoke(name, arguments, callback)
    assert result.error['data']['verdict'] == 'invalid_argument' and result.error['retryable'] is False
    assert 'request_id' not in result.error['data'].get('reconciliation', {})
    assert records == [(name, 'invalid_argument', {'dispatched': False, 'mutation': True})]
    assert arguments == before
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutation_without_native_id_support_is_one_conservative_attempt(records):
    name = 'gaiakeep_core_profile'
    assert catalog.is_mutation(name)
    callback = AsyncMock(side_effect=TimeoutError())
    result = await dispatch.invoke(name, public(name), callback)
    assert result.error['data'] == {'verdict': 'unconfirmed'}
    assert records == [(name, 'unconfirmed', {'dispatched': True, 'mutation': True})]
    callback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_deduplication_upload_attempts_once_without_fabricated_id(records):
    arguments = public(strategy='have')
    arguments.pop('request_id')
    callback = AsyncMock(return_value=None)
    result = await dispatch.invoke(UPLOAD, arguments, callback)
    assert 'request_id' not in result.error['data']['reconciliation']
    callback.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [None, {'machine_id': 'owned-machine', 'params': {'private': 'value'}},
                                     {'machine_id': 'owned-machine', 'params': {'request_id': 'invalid'}}])
async def test_invalid_arguments_fail_closed_without_physical_call(records, arguments):
    callback = AsyncMock(return_value=None)
    result = await dispatch.invoke(READ, arguments, callback)
    assert result.error['data']['verdict'] == 'invalid_argument' and result.error['retryable'] is False
    assert records == [(READ, 'invalid_argument', {'dispatched': False, 'mutation': False})]
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_tool_is_not_dispatched_or_logged_as_valid_event(records):
    callback = AsyncMock(return_value=None)
    result = await dispatch.invoke('unknown-private-tool', {}, callback)
    assert result.error['data']['verdict'] == 'invalid_argument' and records == []
    assert 'unknown-private-tool' not in json.dumps(result.error)
    callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('name,verdict', [(READ, 'unavailable'), (UPLOAD, 'unconfirmed')])
async def test_cancellation_logged_and_propagated_without_retry(records, name, verdict):
    callback = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await dispatch.invoke(name, public(name), callback)
    assert records == [(name, verdict, {'dispatched': True, 'mutation': name == UPLOAD})]
    callback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_issue_sink_failure_cannot_change_unconfirmed_outcome(monkeypatch):
    class Log:
        def record(self, *args, **kwargs):
            raise OSError('private issue log directory')

    monkeypatch.setattr(dispatch, 'IssueLog', Log)
    callback = AsyncMock(side_effect=TimeoutError('private SSH credential'))
    result = await dispatch.invoke(UPLOAD, public(), callback)
    assert result.error['data']['verdict'] == 'unconfirmed' and result.error['retryable'] is False
    assert 'private' not in json.dumps(result.error)
    callback.assert_awaited_once_with()
