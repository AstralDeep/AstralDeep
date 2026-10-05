"""Builds closed public tool schemas from pinned metadata and shares conservative mutation policy with dispatch."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

MAX_FILE = 8 << 20
MAX_RPC = 1 << 20
PROTECTED = frozenset({'principal', 'audience', 'sig', 'sig_ts', 'sig_nonce', 'sig_params',
                       'user', 'approver', 'client_rpc_id', 'events', 'grant',
                       'reconstruct_token', 'up_stream', 'down_stream', 'ident_key',
                       'ident_id', 'transfer_id'})
MODERN_PROTOTYPE = frozenset({'listfiles', 'createproject', 'projectmember', 'commit',
                             'projectfiles', 'resolve', 'promote', 'approvereconstruct',
                             'reconstructauth'})
METADATA = json.loads(Path(__file__).with_name('capabilities.json').read_text(encoding='utf-8'))
REVISION = METADATA['revision']
ACTIONS = {item['action']: item for item in METADATA['actions']}
FILE_ACTIONS = frozenset({'core.put', 'core.get', 'core.haveopen', 'core.have'})
LOCAL_READ_ACTIONS = frozenset({'connection_info', 'list_collections'})
DATASET_ACTIONS = frozenset({'inspect_dataset', 'upload_dataset', 'download_dataset'})
UPLOAD_ACTIONS = frozenset({'upload', 'upload_dataset'})


def relative_path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value
            or ':' in value or '\x00' in value or value.startswith('/')
            or any(p in {'', '.', '..'} for p in value.split('/'))):
        raise ValueError('A relative collection path is required.')
    return str(PurePosixPath(value))


def owned_fields(action):
    fields = set(PROTECTED)
    if action in {'core.put', 'core.get'}:
        fields.add('xfer')
    return fields


def _schema(fields, required):
    return {'type': 'object', 'properties': fields, 'required': required, 'additionalProperties': False}


def _field(kind):
    if kind in {'int', 'long'}:
        return {'type': 'integer', 'minimum': -(2 ** 63), 'maximum': 2 ** 63 - 1}
    if kind == 'boolean':
        return {'type': 'boolean'}
    return {'type': 'string', 'maxLength': 65536}


def _tools():
    out = {}
    for action, item in ACTIONS.items():
        if action in FILE_ACTIONS:
            continue
        params = {p['name']: dict(_field(p['type']), **({'description': p['description']} if p.get('description') else {}))
                  for p in item['parameters']
                  if p['name'] not in owned_fields(action)}
        required = [p['name'] for p in item['parameters']
                    if p['required'] and p['name'] in params]
        name = 'gaiakeep_' + action.replace('.', '_')
        scope = 'tools:system' if item['system'] else ('tools:read' if item['read'] else 'tools:write')
        out[name] = {'action': action, 'scope': scope,
                     'description': f'Call GaiaKeep {action}; Gaia enforces native roles. '
                                    + ('Requires operator legacy opt-in. ' if item['legacy'] else '')
                                    + ('Requires human approval.' if not item['read'] else 'Reads remote state.'),
                     'input_schema': _schema({'machine_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                                              'params': _schema(params, required)}, ['machine_id', 'params'])}
        if action == 'fetch':
            out[name]['description'] = 'Legacy fetch is unsupported until its bounded receiver is qualified. Use gaiakeep_read_file for versioned files.'
    common = {'machine_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
              'path': {'type': 'string', 'minLength': 1, 'maxLength': 4096}}
    out['gaiakeep_upload_file'] = {'action': 'upload', 'scope': 'tools:write',
        'description': 'Publish a new version containing one file (up to 8 MiB) and move the branch head; '
                       'include base_vid to retain its other files. Requires human approval.',
        'input_schema': _schema(dict(common, collection_id={'type': 'string', 'minLength': 1, 'maxLength': 256},
                                    data_base64={'type': 'string', 'maxLength': 4 * ((MAX_FILE + 2) // 3)},
                                    branch={'type': 'string', 'minLength': 1, 'maxLength': 256},
                                    strategy={'enum': ['ingest', 'have']},
                                    base_vid={'type': 'string', 'minLength': 1, 'maxLength': 256},
                                    request_id={'type': 'string', 'pattern': '^[A-Za-z0-9_-]{16,64}$'},
                                    note={'type': 'string', 'maxLength': 512},
                                    expected_head={'type': 'string', 'minLength': 1, 'maxLength': 256}),
                                ['machine_id', 'path', 'collection_id', 'data_base64'])}
    out['gaiakeep_read_file'] = {'action': 'read', 'scope': 'tools:read',
        'description': 'Read and verify one versioned file (up to 8 MiB), returning base64 bytes and SHA256.',
        'input_schema': _schema(dict(common, vid={'type': 'string', 'minLength': 1, 'maxLength': 256}),
                                ['machine_id', 'path', 'vid'])}
    for action, description in (
        ('connection_info', 'Show your enrolled Gaia principal, tenant and default collection without credentials or paths.'),
        ('list_collections', 'Read one page of collections visible to your enrolled Gaia tenant; pass next back as after.'),
    ):
        fields = ({'after': {'type': 'string', 'maxLength': 4096},
                   'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}} if action == 'list_collections' else {})
        out['gaiakeep_' + action] = {'action': action, 'scope': 'tools:read', 'description': description,
            'input_schema': _schema({'machine_id': common['machine_id'], 'params': _schema(fields, [])},
                                    ['machine_id', 'params'])}
    dataset = {'machine_id': common['machine_id'],
               'dataset_ref': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'}}
    out['gaiakeep_inspect_dataset'] = {'action': 'inspect_dataset', 'scope': 'tools:read',
        'description': 'Inspect a local dataset under your Linux Gaia account workspace; return its bounded content manifest and SHA256 for upload approval. At most 256 files and 1 GiB.',
        'input_schema': _schema(dataset, ['machine_id', 'dataset_ref'])}
    out['gaiakeep_download_dataset'] = {'action': 'download_dataset', 'scope': 'tools:write',
        'description': 'Download and verify a complete version (at most 256 files and 1 GiB) into a new account workspace dataset reference. Existing references are never overwritten. Writes local files and requires human approval.',
        'input_schema': _schema(dict(dataset, vid={'type': 'string', 'minLength': 1, 'maxLength': 256}),
                                ['machine_id', 'dataset_ref', 'vid'])}
    upload_fields = out['gaiakeep_upload_file']['input_schema']['properties']
    out['gaiakeep_upload_dataset'] = {'action': 'upload_dataset', 'scope': 'tools:write',
        'description': 'Publish one atomic version from an inspected local dataset. Bind manifest_sha256 from inspect_dataset into approval; changed bytes are refused. For an existing branch include expected_head; base_vid defaults to that head to preserve other files. Prefix and note can identify a completed run. At most 256 files and 1 GiB; human approval required.',
        'input_schema': _schema(dict(dataset,
                                    **{name: upload_fields[name] for name in ('collection_id', 'branch', 'base_vid', 'expected_head', 'request_id', 'note')},
                                    manifest_sha256={'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
                                    prefix={'type': 'string', 'minLength': 1, 'maxLength': 4096}),
                                ['machine_id', 'dataset_ref', 'collection_id', 'manifest_sha256'])}
    return out


TOOLS = _tools()
READ_TOOLS = frozenset(name for name, item in TOOLS.items()
                       if item['action'] in {'read', 'inspect_dataset'} or item['action'] in LOCAL_READ_ACTIONS
                       or ACTIONS.get(item['action'], {}).get('read', False))


def is_mutation(name):
    return name not in READ_TOOLS


def _bounded_json(value, depth=0):
    if depth > 16:
        raise ValueError('JSON nesting exceeds the allowed depth.')
    if isinstance(value, (dict, list)):
        if len(value) > 10000:
            raise ValueError('JSON contains too many entries.')
        for item in value.values() if isinstance(value, dict) else value:
            _bounded_json(item, depth + 1)


def validate(name, arguments):
    from jsonschema import Draft202012Validator
    if name not in TOOLS:
        raise ValueError('Unknown GaiaKeep tool.')
    try:
        Draft202012Validator(TOOLS[name]['input_schema']).validate(arguments)
        encoded = json.dumps(arguments, allow_nan=False).encode()
    except Exception as exc:
        raise ValueError('Invalid GaiaKeep tool arguments.') from exc
    if len(encoded) > (12 << 20 if name == 'gaiakeep_upload_file' else MAX_RPC):
        raise ValueError('Tool arguments exceed the allowed size.')
    if is_mutation(name) and name != 'gaiakeep_upload_file' and len(encoded) > 10000:
        raise ValueError('Mutation parameters exceed the approval display bound.')
    if name == 'gaiakeep_upload_file' and arguments.get('strategy') == 'have' and arguments.get('base_vid'):
        raise ValueError('Deduplication upload does not support a base version.')
    if name == 'gaiakeep_upload_file' and arguments.get('strategy') == 'have' and arguments.get('request_id'):
        raise ValueError('Deduplication upload does not support a request identifier.')
    if 'note' in arguments and (len(arguments['note'].encode('utf-8')) > 512
                                or any(ord(c) < 32 or ord(c) == 127 for c in arguments['note'])):
        raise ValueError('Version notes allow at most 512 UTF-8 bytes without control characters.')
    params = arguments.get('params', {})
    for key, value in params.items():
        if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
            try:
                _bounded_json(json.loads(value))
            except (ValueError, RecursionError) as exc:
                raise ValueError('Invalid nested JSON parameter.') from exc
        if key in {'path', 'new_path', 'dest_path'}:
            relative_path(value)
        if key == 'request_id' and (not isinstance(value, str) or not 16 <= len(value) <= 64
                                    or any(not (c.isascii() and (c.isalnum() or c in '_-')) for c in value)):
            raise ValueError('Invalid GaiaKeep idempotency request identifier.')
        if key in {'limit', 'wait_ms', 'size', 'length', 'chunk', 'window', 'pages'}:
            maximum = {'limit': 1000, 'wait_ms': 20000, 'pages': 1024}.get(key, MAX_FILE)
            if isinstance(value, bool) or not str(value).isdigit() or not 0 <= int(value) <= maximum:
                raise ValueError('Numeric parameter exceeds the allowed bound.')
    if 'path' in arguments:
        relative_path(arguments['path'])
    if 'prefix' in arguments:
        relative_path(arguments['prefix'])
    return arguments
