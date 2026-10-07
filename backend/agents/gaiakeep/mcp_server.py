"""Dispatches closed GaiaKeep tools using server-owned identity, encrypted credentials and owner-scoped machines."""

from __future__ import annotations

import json
import os
import uuid
from functools import partial

from astralprims import Alert
from orchestrator import remote_machines
from shared.feature_flags import flags
from shared.protocol import MCPResponse

from agents.gaiakeep import catalog, client
from agents.gaiakeep.issue_log import IssueLog
from agents.gaiakeep.presentation import result_components
from agents.gaiakeep.remote_client import RemoteCore
from agents.gaiakeep.transport import failure_detail

CONTEXT_FIELDS = frozenset({'user_id', 'session_id', '_runtime', '_credentials', '_credentials_stale',
                            '_credentials_encrypted', '_session_llm_credentials', '_session_llm_config',
                            '_delegation_token', '_cap_job_id'})


def bind_owner_context(arguments, user_id):
    arguments['user_id'] = user_id


def _machine_target(plane_source, credential_manager, user_id, reference, mutation):
    try:
        return remote_machines.build_target(plane_source, credential_manager, user_id, reference)
    except remote_machines.MachineNotFound as exc:
        if mutation:
            raise client.AgentError('invalid_argument', 'GaiaKeep changes require the registered machine ID.') from exc
        machine = remote_machines.resolve_machine(plane_source, user_id, reference)
        if machine is None:
            raise client.AgentError('invalid_argument', 'Select a registered machine you own.') from exc
        return remote_machines.build_target(plane_source, credential_manager, user_id, machine['machine_id'])


class MCPServer:
    def __init__(self, plane_source, credential_manager):
        self.plane_source = plane_source
        self.credential_manager = credential_manager
        self.issue_log = IssueLog()
        self.tools = {name: dict(info, function=partial(self.invoke, name)) for name, info in catalog.TOOLS.items()}

    def get_tool_list(self):
        return [{k: info[k] for k in ('description', 'input_schema', 'scope')} | {'name': name}
                for name, info in self.tools.items()]

    def invoke(self, name, **arguments):
        mutation = catalog.is_mutation(name)
        secrets = []
        dispatched = False
        reconciliation = {}
        try:
            if not flags.is_enabled('gaiakeep') or not flags.is_enabled('cresco'):
                raise client.AgentError('not_configured', 'GaiaKeep is disabled by the operator.')
            public = {k: v for k, v in arguments.items() if k not in CONTEXT_FIELDS}
            catalog.validate(name, public)
            if catalog.TOOLS[name]['action'] == 'fetch':
                raise client.AgentError('unsupported', 'Legacy fetch requires a separately qualified bounded receiver. Use the versioned file read tool.')
            user_id = arguments.get('user_id')
            if not isinstance(user_id, str) or not user_id:
                raise client.AgentError('auth_failed', 'A signed-in owner is required.')
            credentials = {} if os.getenv('GAIAKEEP_CONNECTION_MODE', 'ssh') == 'ssh' else arguments.get('_credentials') or {}
            if (not isinstance(credentials, dict) or arguments.get('_credentials_encrypted')
                    or arguments.get('_credentials_stale')) and os.getenv('GAIAKEEP_CONNECTION_MODE', 'ssh') != 'ssh':
                raise client.AgentError('auth_failed', 'Re-enter your GaiaKeep credentials.')
            secrets.extend(v for k, v in credentials.items() if k != 'GAIAKEEP_PRINCIPAL' and isinstance(v, str))
            target = _machine_target(self.plane_source, self.credential_manager,
                                     user_id, public['machine_id'], mutation)
            secrets.extend([target.secret, target.passphrase])
            with client.open_client(target, credentials) as (core, config):
                secrets.append(config.service_key)
                action = catalog.TOOLS[name]['action']
                if action in catalog.DATASET_ACTIONS and not isinstance(core, RemoteCore):
                    raise client.AgentError('unsupported', 'Local dataset workflows require the qualified Linux SSH account.')
                if action not in {'read', 'upload'} and catalog.ACTIONS.get(action, {}).get('legacy') and not config.allow_legacy:
                    raise client.AgentError('unsupported', 'The operator has not enabled GaiaKeep legacy actions.')
                if action == 'core.legalorder' and public['params'].get('action', 'erase') != 'erase':
                    raise client.AgentError('unsupported', 'GaiaKeep cannot currently sign legal-order shortening.')
                if action in catalog.UPLOAD_ACTIONS:
                    fields = ('collection_id', 'path') if action == 'upload' else ('collection_id', 'dataset_ref', 'manifest_sha256')
                    reconciliation = {k: public[k] for k in fields}
                    reconciliation['branch'] = public.get('branch', 'main')
                    if public.get('strategy', 'ingest') == 'ingest':
                        reconciliation['request_id'] = public.get('request_id') or uuid.uuid4().hex
                elif action in catalog.LOCAL_READ_ACTIONS:
                    params = public['params']
                    if not isinstance(core, RemoteCore):
                        raise client.AgentError('unsupported', 'Gaia account discovery requires the SSH connection mode.')
                elif action != 'read' and action not in catalog.DATASET_ACTIONS:
                    params = client.prepare_params(action, public['params'])
                    if 'request_id' in params:
                        reconciliation['request_id'] = params['request_id']
                dispatched = True
                if isinstance(core, RemoteCore):
                    payload = ({'params': params} if action not in {'read', 'upload'} | catalog.DATASET_ACTIONS
                               else {k: v for k, v in public.items() if k not in {'machine_id', 'request_id'}})
                    result = core.perform(action, payload, reconciliation.get('request_id') if action in catalog.UPLOAD_ACTIONS else None)
                elif action == 'upload':
                    result = client.upload(core, public['collection_id'], public['path'],
                                           public['data_base64'], public.get('branch', 'main'),
                                           public.get('strategy', 'ingest'),
                                           public.get('base_vid'), public.get('expected_head'),
                                           reconciliation.get('request_id'), public.get('note'))
                elif action == 'read':
                    result = client.read(core, public['vid'], public['path'])
                else:
                    result = client.execute(core, action, params, config, credentials)
            if action == 'read':
                data_base64 = result.pop('data_base64')
                result = client.clean_result(result, secrets)
                result['data_base64'] = data_base64
            else:
                result = (client.clean_dataset_result(result, secrets) if action in catalog.DATASET_ACTIONS
                          else client.clean_result(result, secrets))
            serialized = json.dumps({k: v for k, v in result.items() if k != 'data_base64'}, ensure_ascii=False)
            if len(serialized.encode()) > client.MAX_RPC:
                raise client.AgentError('protocol_error', 'The Gaia result exceeds the response bound; request a smaller page.')
            publication = result.get('publication', result)
            pending = isinstance(publication, dict) and (publication.get('commit_job') is not None
                                                        or publication.get('pending') is True)
            if pending:
                native = client.native_reconciliation(publication)
                if 'commit_job' not in native:
                    raise client.AgentError('protocol_error', 'The pending version lacks a valid commit job identifier.')
                reconciliation.update(native)
            components = result_components(name, result, pending=pending, reconciliation=reconciliation,
                                           secrets=tuple(secret for secret in secrets if isinstance(secret, str)))
            data = {'verdict': 'pending' if pending else 'ok', 'result': result}
            if reconciliation:
                data['reconciliation'] = reconciliation
            return {'_data': data, '_ui_components': components}
        except Exception as exc:  # noqa: BLE001
            detail = failure_detail(exc)
            if (dispatched and mutation and isinstance(exc, client.AgentError)
                    and exc.verdict == 'protocol_error'):
                exc = client.AgentError('unconfirmed', 'The operation may have taken effect. Check native state before retrying.')
            verdict, message = client.failure(exc, mutation and dispatched)
            if verdict == 'pending':
                reconciliation.update(client.native_reconciliation(exc))
            self.issue_log.record(name, verdict, dispatched=dispatched, mutation=mutation,
                                  **({'detail': detail} if detail is not None else {}))
            data = {'verdict': verdict}
            if reconciliation:
                data['reconciliation'] = reconciliation
                if 'request_id' in reconciliation:
                    message += f" Native request_id: {reconciliation['request_id']}."
            return {'_data': data,
                    '_ui_components': [Alert(message=message, variant='error').to_dict()], '_error': message}

    def process_request(self, request):
        if request.method == 'tools/list':
            return MCPResponse(request_id=request.request_id, result={'tools': self.get_tool_list()})
        if request.method != 'tools/call':
            return MCPResponse(request_id=request.request_id,
                               error={'code': -32601, 'message': 'Unknown MCP method.', 'retryable': False})
        params = request.params or {}
        name, arguments = params.get('name'), params.get('arguments')
        if name not in self.tools or not isinstance(arguments, dict):
            return MCPResponse(request_id=request.request_id,
                               error={'code': -32602, 'message': 'Invalid GaiaKeep tool request.', 'retryable': False})
        result = self.invoke(name, **arguments)
        if '_error' in result:
            return MCPResponse(request_id=request.request_id,
                               error={'code': -32603, 'message': result['_error'], 'retryable': False,
                                      'data': result['_data']})
        return MCPResponse(request_id=request.request_id, result=result['_data'], ui_components=result['_ui_components'])
