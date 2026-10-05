"""Turns sanitized Gaia results into bounded summaries and typed tables using the pinned primitives. Display decoding never changes the canonical result or its reconciliation and proof fields."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, TypedDict

from astralprims import Alert, Badge, Card, Grid, KeyValue, MetricCard, Primitive, Table, Text

from agents.gaiakeep import client

MAX_JSON_TEXT = 65536
MAX_ENCODED_TEXT = 131072
MAX_DEPTH = 8
MAX_ENCODED_LAYERS = 8
MAX_NODES = 2048
MAX_ROWS = 50
MAX_COLUMNS = 8
MAX_COMPONENTS = 24
MAX_CELL_TEXT = 512
MAX_VISIBLE_TEXT = 65536
_LITERAL = re.compile(r'(^|_)(id|vid|path|key|hash|sha256|digest|name|principal|tenant|role|state|branch|note|label|description|title|message)$')
_HIDDEN = re.compile(r'(^_|token|secret|private|password|passphrase|credential|service_key|data_base64|key_file|'
                     r'(^|_)base64($|_)|certificate_public_key|'
                     r'(^|_)(sig|signature|cert|certificate|proof|proofs)$)', re.IGNORECASE)
_NUMBER = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$')
_REFERENCES = ('collection_id', 'dataset_ref', 'vid', 'job_id', 'commit_job', 'request_id', 'extract_id', 'manifest_sha256', 'sha256')
_METRICS = ('count', 'file_count', 'total_bytes', 'byte_count', 'term', 'commit', 'applied', 'last', 'durable',
            'commit_index', 'applied_index', 'last_applied', 'durable_index')
_COLUMN_PRIORITY = ('collection_id', 'vid', 'job_id', 'id', 'path', 'name', 'state', 'role', 'up', 'durable', 'size', 'count')


@dataclass(frozen=True)
class _Omitted:
    reason: str


Scalar: TypeAlias = str | int | float | bool | None
DisplayValue: TypeAlias = Scalar | _Omitted | list['DisplayValue'] | dict[str, 'DisplayValue']


class ColumnHint(TypedDict):
    key: str
    label: str
    kind: Literal['text', 'number', 'boolean', 'null', 'mixed']


@dataclass
class _Budget:
    nodes: int = MAX_NODES
    encoded_text: int = MAX_ENCODED_TEXT
    components: int = MAX_COMPONENTS
    visible_text: int = MAX_VISIBLE_TEXT
    notices: set[str] = field(default_factory=set)

    def omitted(self, reason: str) -> _Omitted:
        self.notices.add(reason)
        return _Omitted(reason)


def _json_depth(text: str) -> int:
    depth = maximum = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '{[':
            depth += 1
            maximum = max(maximum, depth)
        elif char in '}]':
            depth -= 1
    return maximum


def _decode(value: Any, key: str, budget: _Budget, secrets: tuple[str, ...], depth: int = 0,
            encoded_layers: int = 0) -> DisplayValue:
    if depth > MAX_DEPTH or budget.nodes <= 0:
        return budget.omitted('Some detail exceeds the display depth or entry limit.')
    budget.nodes -= 1
    if isinstance(value, dict):
        output: dict[str, DisplayValue] = {}
        for name, item in value.items():
            if _HIDDEN.search(str(name)):
                continue
            if budget.nodes <= 0:
                budget.omitted('Some detail exceeds the display depth or entry limit.')
                break
            output[str(name)] = _decode(item, str(name), budget, secrets, depth + 1)
        return output
    if isinstance(value, list):
        output_list: list[DisplayValue] = []
        for item in value:
            if budget.nodes <= 0:
                output_list.append(budget.omitted('Some detail exceeds the display depth or entry limit.'))
                break
            output_list.append(_decode(item, key, budget, secrets, depth + 1))
        return output_list
    if isinstance(value, str):
        value = client.clean_result(value, secrets, decode_strings=False)
    if isinstance(value, str) and not _LITERAL.search(key):
        text = value.strip()
        encoded = text != '[redacted]' and (text.startswith(('{', '[', '"'))
                                            or text in {'true', 'false', 'null'} or bool(_NUMBER.fullmatch(text)))
        if encoded:
            size = len(text.encode('utf-8'))
            if (size > MAX_JSON_TEXT or size > budget.encoded_text or _json_depth(text) + depth > MAX_DEPTH
                    or encoded_layers >= MAX_ENCODED_LAYERS):
                return budget.omitted('Some encoded data exceeds the display size or nesting limit.')
            budget.encoded_text -= size
            try:
                decoded = json.loads(text)
                decoded = client.clean_result(decoded, secrets, decode_strings=False)
                return _decode(decoded, key, budget, secrets, depth, encoded_layers + 1)
            except (ValueError, RecursionError, client.AgentError):
                return budget.omitted('Some encoded data could not be safely formatted.')
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return budget.omitted('A non-finite value could not be formatted.')
        return value
    return budget.omitted('An unsupported value could not be formatted.')


def _label(key: str) -> str:
    return re.sub(r'[_\-.]+', ' ', key).strip().capitalize()[:80] or 'Value'


def _cell(value: DisplayValue, budget: _Budget) -> Scalar:
    if isinstance(value, _Omitted):
        value = 'Display omitted'
    if isinstance(value, (dict, list)):
        budget.notices.add('Nested cells are summarized in tables; the canonical result retains their detail.')
        value = f'{len(value)} fields' if isinstance(value, dict) else f'{len(value)} items'
    text = str(value) if value is not None else 'Not provided'
    if len(text) > MAX_CELL_TEXT:
        budget.notices.add('Long values are shortened in this view; the canonical result retains them.')
        text = text[:MAX_CELL_TEXT] + '…'
        value = text
    size = len(text.encode('utf-8'))
    if size > budget.visible_text:
        budget.notices.add('Some values exceed the total display text limit; the canonical result retains them.')
        value = text = 'Display omitted' if budget.visible_text >= len('Display omitted') else ''
        size = len(text)
    budget.visible_text -= size
    return value if value is not None else text


def _kind(values: list[DisplayValue]) -> Literal['text', 'number', 'boolean', 'null', 'mixed']:
    types = {('boolean' if isinstance(value, bool) else 'number' if isinstance(value, (int, float)) else 'text')
             for value in values if value is not None}
    return next(iter(types)) if len(types) == 1 else 'mixed' if types else 'null'


def _columns(records: list[dict[str, DisplayValue]], budget: _Budget, identity_key: str | None) -> list[ColumnHint]:
    keys = set().union(*(record.keys() for record in records))
    ordered = sorted(keys, key=lambda key: (-1 if key == identity_key else _COLUMN_PRIORITY.index(key)
                                           if key in _COLUMN_PRIORITY else len(_COLUMN_PRIORITY), key))
    if len(ordered) > MAX_COLUMNS:
        budget.notices.add('Some columns are omitted from this preview; the canonical result retains them.')
    return [{'key': key, 'label': _label(key), 'kind': _kind([record.get(key) for record in records])}
            for key in ordered[:MAX_COLUMNS]]


def _table(values: list[DisplayValue], title: str, budget: _Budget, identity_key: str | None = None) -> Primitive:
    shown = values[:MAX_ROWS]
    if len(values) > MAX_ROWS:
        budget.notices.add(f'Tables show at most {MAX_ROWS} rows from the returned page.')
    if shown and all(isinstance(item, dict) for item in shown):
        records = []
        for item in shown:
            if isinstance(item, dict):
                record = dict(item)
                for key, value in item.items():
                    if (isinstance(value, dict) and value
                            and all(not isinstance(child, (dict, list)) for child in value.values())
                            and all(f'{key}.{name}' not in item for name in value)):
                        record.pop(key)
                        record.update({f'{key}.{name}': child for name, child in value.items()})
                records.append(record)
        columns = _columns(records, budget, identity_key)
        if not columns:
            return Text(content=f'{title}: no displayable fields.', variant='caption')
        return Table(headers=[column['label'] for column in columns],
                     rows=[[_cell(record.get(column['key']), budget) for column in columns] for record in records],
                     attributes={'title': title})
    return Table(headers=['Item', 'Value', 'Type'],
                 rows=[[_cell(index + 1, budget), _cell(value, budget), _cell(_kind([value]), budget)]
                       for index, value in enumerate(shown)],
                 attributes={'title': title})


def _append(components: list[Primitive], component: Primitive, budget: _Budget) -> None:
    if budget.components > 0:
        components.append(component)
        budget.components -= 1
    else:
        budget.notices.add('Some sections are omitted from this view; the canonical result retains them.')


def _sections(value: DisplayValue, title: str, budget: _Budget) -> list[Primitive]:
    components: list[Primitive] = []
    if isinstance(value, dict):
        if value and all(isinstance(item, dict) and item and all(not isinstance(child, (dict, list)) for child in item.values())
                         for item in value.values()):
            identity_key, suffix = 'map_key', 1
            while any(identity_key in item for item in value.values() if isinstance(item, dict)):
                suffix += 1
                identity_key = f'map_key_{suffix}'
            records = [dict(item, **{identity_key: name}) for name, item in value.items() if isinstance(item, dict)]
            _append(components, _table(records, title, budget, identity_key), budget)
        else:
            scalar = [(key, item) for key, item in value.items() if not isinstance(item, (dict, list))]
            if scalar:
                if len(scalar) > 16:
                    budget.notices.add('Some fields are omitted from this preview; the canonical result retains them.')
                _append(components, KeyValue(title=title, columns=2, items=[
                    {'label': _label(key), 'value': _cell(item, budget)} for key, item in scalar[:16]
                ]), budget)
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    if budget.components <= 0:
                        budget.notices.add('Some sections are omitted from this view; the canonical result retains them.')
                        break
                    components.extend(_sections(item, _label(key), budget))
            if not value:
                _append(components, Text(content=f'{title}: no displayable fields.', variant='caption'), budget)
    elif isinstance(value, list):
        if value:
            _append(components, _table(value, title, budget), budget)
        else:
            _append(components, Text(content=f'{title}: no entries were returned.', variant='caption'), budget)
    elif isinstance(value, _Omitted):
        _append(components, Text(content=f'{title}: display omitted.', variant='caption'), budget)
    else:
        _append(components, KeyValue(title=title, items=[{'label': 'Value', 'value': _cell(value, budget)}]), budget)
    return components


def _summary_objects(value: dict[str, DisplayValue]) -> list[dict[str, DisplayValue]]:
    objects = [value]
    for key in ('core_status', 'data', 'result', 'publication', 'raft'):
        nested = value.get(key)
        if isinstance(nested, dict):
            objects.extend(_summary_objects(nested))
    return objects


def result_components(name: str, result: dict[str, Any], *, pending: bool = False,
                      reconciliation: dict[str, Any] | None = None, secrets: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    budget = _Budget()
    view = _decode(result, '', budget, secrets)
    objects = _summary_objects(view) if isinstance(view, dict) else []
    title = 'GaiaKeep version awaiting durable copies' if pending else 'GaiaKeep ' + _label(name.removeprefix('gaiakeep_')).lower()
    label, variant = ('Awaiting durable copies', 'warning') if pending else ('Response received', 'info')
    if not pending:
        for item in objects:
            if item.get('leader_serving') is False:
                label, variant = 'Leader not serving', 'warning'
                break
            state = item.get('state')
            if isinstance(state, str) and state:
                label = state.replace('_', ' ').capitalize()[:80]
                variant = 'success' if state in {'DONE', 'PUBLISHED', 'VERIFIED'} else 'error' if state in {'FAILED', 'ERROR'} else 'info'
    components: list[Primitive] = [Badge(label=label, variant=variant)]
    metrics: dict[str, Scalar] = {}
    for item in objects:
        for key in _METRICS:
            value = item.get(key)
            if key not in metrics and isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[key] = value
    if metrics:
        components.append(Grid(columns=min(len(metrics), 4), children=[
            MetricCard(title=_label(key), value=str(_cell(f'{value:,}', budget))) for key, value in list(metrics.items())[:4]
        ]))
    if reconciliation:
        references = {key: reconciliation[key] for key in _REFERENCES if key in reconciliation}
        if references:
            components.extend(_sections(_decode(references, '', budget, secrets), 'Reconciliation', budget))
    components.extend(_sections(view, 'Details', budget))
    if budget.notices:
        components.append(Alert(message=' '.join(sorted(budget.notices)), variant='info', title='Display limits'))
    return [Card(title=title, content=components).to_dict()]
