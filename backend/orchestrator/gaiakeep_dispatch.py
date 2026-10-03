"""Preserves Gaia native request identities before approval and classifies one governed physical dispatch without replay. Outer failures mirror fixed metadata through Gaia's IssueLog while MCP-classified failures retain their existing result and log."""

from __future__ import annotations

import asyncio
import uuid

from agents.gaiakeep import catalog
from agents.gaiakeep.issue_log import IssueLog, VERDICTS
from agents.gaiakeep.transport import FLOW_TIMEOUT
from shared.protocol import MCPResponse

DEFAULT_TIMEOUT = FLOW_TIMEOUT + 10
PROTOCOL_REFUSALS = frozenset({-32020, -32021, -32022, -32600, -32601, -32602})


def _supports_request_id(name):
    action = catalog.TOOLS[name]['action']
    return (action == 'upload'
            or (catalog.is_mutation(name) and any(
                field['name'] == 'request_id' for field in catalog.ACTIONS.get(action, {}).get('parameters', []))))


def _reconciliation(name, public):
    if catalog.TOOLS[name]['action'] == 'upload':
        retained = {key: public[key] for key in ('collection_id', 'path')}
        retained['branch'] = public.get('branch', 'main')
        if public.get('strategy', 'ingest') == 'ingest' and 'request_id' in public:
            retained['request_id'] = public['request_id']
        return retained
    if _supports_request_id(name) and 'request_id' in public['params']:
        return {'request_id': public['params']['request_id']}
    return {}


def prepare_reconciliation(name, public):
    catalog.validate(name, public)
    if not catalog.is_mutation(name) or not _supports_request_id(name):
        return {}
    prepared = dict(public)
    if catalog.TOOLS[name]['action'] == 'upload':
        if public.get('strategy', 'ingest') == 'ingest':
            prepared['request_id'] = public.get('request_id') or uuid.uuid4().hex
    else:
        prepared['params'] = dict(public['params'])
        prepared['params']['request_id'] = public['params'].get('request_id') or uuid.uuid4().hex
    catalog.validate(name, prepared)
    return _reconciliation(name, prepared)


async def _record(name, verdict, dispatched, mutation):
    try:
        await asyncio.to_thread(IssueLog().record, name, verdict, dispatched=dispatched, mutation=mutation)
    except Exception:
        return


def _failure(verdict, reconciliation, request_id=None):
    messages = {
        'invalid_argument': 'GaiaKeep requires valid arguments and a retained native request identifier before dispatch.',
        'unavailable': 'The GaiaKeep operation did not return a confirmed result.',
        'unconfirmed': 'The GaiaKeep operation may have taken effect. Check native state before retrying.',
    }
    data = {'verdict': verdict}
    if reconciliation:
        data['reconciliation'] = reconciliation
    return MCPResponse(request_id=request_id if isinstance(request_id, str) else '',
                       error={'code': -32603, 'message': messages[verdict], 'retryable': False, 'data': data})


def _predispatch_verdict(code):
    if type(code) is int and code in PROTOCOL_REFUSALS:
        return 'protocol_error'
    if type(code) is str and (code in {'required_identity_unavailable', 'protected_executor_failed',
                                     'missing_protected_permit', 'invalid_protected_permit',
                                     'invalid_protected_context', 'unexpected_protected_permit'}
                             or code.startswith(('executor_', 'receipt_', 'trust_manifest_'))):
        return 'auth_failed'
    return None


async def invoke(name, arguments, physical_call):
    mutation = catalog.is_mutation(name)
    reconciliation = {}
    try:
        properties = catalog.TOOLS[name]['input_schema']['properties']
        public = {key: value for key, value in arguments.items() if key in properties}
        catalog.validate(name, public)
        reconciliation = _reconciliation(name, public)
        needs_id = (_supports_request_id(name)
                    and (catalog.TOOLS[name]['action'] != 'upload'
                         or public.get('strategy', 'ingest') == 'ingest'))
        if needs_id and 'request_id' not in reconciliation:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        if name in catalog.TOOLS:
            await _record(name, 'invalid_argument', False, mutation)
        return _failure('invalid_argument', reconciliation)
    verdict = 'unconfirmed' if mutation else 'unavailable'
    try:
        response = await physical_call()
    except asyncio.CancelledError:
        await _record(name, verdict, True, mutation)
        raise
    except Exception:
        response = None
    if isinstance(response, MCPResponse):
        if response.error is None:
            return response
        data = response.error.get('data') if isinstance(response.error, dict) else None
        if isinstance(data, dict) and type(data.get('verdict')) is str and data['verdict'] in VERDICTS:
            response.error = {**response.error, 'retryable': False}
            return response
        if isinstance(response.error, dict):
            code = response.error.get('code')
            if (response.error.get('retryable') is False and code != -32603
                    and (code is None or type(code) in {int, str})):
                refusal = _predispatch_verdict(code)
                if refusal is not None:
                    await _record(name, refusal, False, mutation)
                return response
    await _record(name, verdict, True, mutation)
    return _failure(verdict, reconciliation, getattr(response, 'request_id', None))
