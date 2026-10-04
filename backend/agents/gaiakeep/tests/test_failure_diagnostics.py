"""Verifies closed native failure labels across exception boundaries and the operational issue mirror. Strict validation, bounded traversal and optional diagnostics preserve the primary failure without exporting private exception data."""

import errno
import hashlib
import json
import logging
from pathlib import Path
import ssl
import sys
from types import ModuleType

import pytest
from agents.gaiakeep import client, issue_log, remote_runtime, transport

PHASES = frozenset({'rpc_open', 'tcp_connect', 'websocket_open', 'rpc_send', 'rpc_receive', 'rpc_decode',
                    'rpc_response', 'stream_open', 'stream_activation', 'stream_send', 'stream_receive',
                    'cleanup', 'sdk_validation', 'account_validation', 'sdk_read', 'sdk_upload',
                    'sdk_control', 'result_validation'})
KINDS = frozenset({'timeout', 'connection', 'tls', 'authentication', 'authorization', 'integrity',
                   'protocol', 'invalid_argument', 'native', 'other'})
SECRET = 'private credential /patient/path?token=never-exported'


def detail(**changes):
    return {'native_phase': 'rpc_receive', 'failure_kind': 'timeout', 'native_status': None} | changes


@pytest.fixture
def sdk_errors(monkeypatch):
    path = Path(__file__).parent / 'fixtures' / 'qualified-sdk' / 'errors.py.txt'
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == remote_runtime.SDK_LOCK['files']['gaiakeep/errors.py']
    errors = ModuleType('gaiakeep.errors')
    exec(compile(raw, str(path), 'exec'), errors.__dict__)
    package = ModuleType('gaiakeep')
    package.__path__ = []
    package.errors = errors
    monkeypatch.setitem(sys.modules, 'gaiakeep', package)
    monkeypatch.setitem(sys.modules, 'gaiakeep.errors', errors)
    return errors


def test_failure_vocabulary_is_closed():
    assert transport.FAILURE_PHASES == PHASES
    assert transport.FAILURE_KINDS == KINDS


@pytest.mark.parametrize('phase', sorted(PHASES))
def test_declared_native_phase_accepts_only_closed_fields(phase):
    value = detail(native_phase=phase, native_status=504)
    assert transport.validate_failure_detail(value) == value


@pytest.mark.parametrize('kind', sorted(KINDS))
def test_declared_kind_is_preserved_without_untrusted_data(kind):
    value = detail(failure_kind=kind)
    assert transport.validate_failure_detail(value) == value


@pytest.mark.parametrize('value', [
    None, True, [], 'rpc_receive', {},
    {'native_phase': 'rpc_receive', 'failure_kind': 'timeout'},
    detail(credential=SECRET), detail(native_phase=SECRET), detail(failure_kind=SECRET),
    detail(native_phase='rpc_receive\n'), detail(failure_kind='timeout\0'),
    detail(native_phase=True), detail(failure_kind=1),
    detail(native_status=True), detail(native_status=False), detail(native_status='504'),
    detail(native_status=-1), detail(native_status=1000), detail(native_status=5.0),
    detail(native_status=float('nan')), detail(native_status={'message': SECRET}),
])
def test_invalid_detail_is_discarded_whole(value):
    assert transport.validate_failure_detail(value) is None


@pytest.mark.parametrize('status', [None, 0, 9, 500, 504, 999])
def test_validation_returns_an_independent_exact_dictionary(status):
    value = detail(native_status=status)
    result = transport.validate_failure_detail(value)
    assert type(result) is dict and result == value and result is not value
    assert set(result) == {'native_phase', 'failure_kind', 'native_status'}
    value['native_phase'] = SECRET
    assert result['native_phase'] == 'rpc_receive'


@pytest.mark.parametrize('exception,kind', [
    (TimeoutError(SECRET), 'timeout'), (ConnectionError(SECRET), 'connection'),
    (OSError(errno.ECONNRESET, SECRET), 'connection'), (OSError(errno.EIO, SECRET), 'other'),
    (ssl.SSLError(SECRET), 'tls'),
    (ssl.SSLCertVerificationError(SECRET), 'tls'), (PermissionError(SECRET), 'authorization'),
    (transport.ProtocolError(SECRET), 'protocol'), (ValueError(SECRET), 'invalid_argument'),
    (RuntimeError(SECRET), 'other'),
])
def test_known_fallback_is_closed_and_does_not_stringify(exception, kind):
    output = transport.failure_detail(exception, 'sdk_read')
    assert output == detail(native_phase='sdk_read', failure_kind=kind)
    assert SECRET not in json.dumps(output)
    assert transport.failure_detail(exception) is None


@pytest.mark.parametrize('phase', [None, '', SECRET, 'rpc_open\r', True, {}, 3])
def test_unknown_fallback_never_creates_a_diagnostic(phase):
    assert transport.failure_detail(RuntimeError(SECRET), phase) is None


@pytest.mark.parametrize('status', ['500', '504'])
def test_real_sdk_rpc_status_classification_does_not_change_retry_semantics(sdk_errors, status):
    exc = sdk_errors.from_reply('core.head', {'status': status, 'status_desc': SECRET})
    assert isinstance(exc, sdk_errors.RpcTimeout)
    before = (type(exc), exc.retryable, exc.status, exc.action, exc.args)
    assert transport.mark_failure(exc, 'rpc_response') is exc
    assert transport.failure_detail(exc) == detail(native_phase='rpc_response', native_status=int(status))
    assert (type(exc), exc.retryable, exc.status, exc.action, exc.args) == before
    assert SECRET not in json.dumps(transport.failure_detail(exc))


@pytest.mark.parametrize('name,status,kind', [
    ('Unauthenticated', '3', 'authentication'), ('Forbidden', '4', 'authorization'),
    ('PolicyRefused', '7', 'authorization'), ('IntegrityError', None, 'integrity'),
    ('BadRequest', '8', 'protocol'), ('NotFound', '9', 'native'),
    ('InternalError', '5', 'native'), ('TransportError', None, 'connection'),
])
def test_verified_sdk_classes_retain_primary_identity(sdk_errors, name, status, kind):
    exc = getattr(sdk_errors, name)(SECRET, status=status)
    prior = (type(exc), exc.retryable, exc.status, client.failure(exc, mutation=False))
    assert transport.mark_failure(exc, 'sdk_control') is exc
    assert transport.failure_detail(exc) == detail(native_phase='sdk_control', failure_kind=kind,
                                                   native_status=None if status is None else int(status))
    assert (type(exc), exc.retryable, exc.status, client.failure(exc, mutation=False)) == prior


@pytest.mark.parametrize('status,expected', [
    (0, 0), ('0', 0), (500, 500), ('504', 504), ('999', 999),
    (True, None), (-1, None), (1000, None), ('0500', None), ('+500', None),
    (' 500', None), ('500\n', None), (5.0, None), (SECRET, None),
])
def test_canonical_exception_status_is_numeric_or_absent(status, expected):
    exc = TimeoutError(SECRET)
    exc.status = status
    assert transport.mark_failure(exc, 'rpc_response') is exc
    assert transport.failure_detail(exc) == detail(native_phase='rpc_response', native_status=expected)


def test_explicit_stream_status_does_not_change_exception_or_retry_it():
    exc = transport.ProtocolError(SECRET)
    exc.retryable = False
    assert transport.mark_failure(exc, 'stream_activation', native_status=4) is exc
    assert exc.retryable is False
    assert transport.failure_detail(exc) == detail(native_phase='stream_activation', failure_kind='protocol', native_status=4)


def test_first_valid_cause_detail_survives_outer_mark_and_copy():
    inner = TimeoutError(SECRET)
    transport.mark_failure(inner, 'rpc_receive', native_status=504)
    outer = RuntimeError(SECRET)
    outer.__cause__ = inner
    assert transport.mark_failure(outer, 'sdk_read') is outer
    assert transport.failure_detail(outer, 'cleanup') == detail(native_status=504)
    copied = transport.failure_detail(outer)
    copied['native_phase'] = SECRET
    assert transport.failure_detail(outer) == detail(native_status=504)


def test_invalid_attached_detail_does_not_hide_valid_context():
    inner = TimeoutError(SECRET)
    transport.mark_failure(inner, 'websocket_open')
    outer = RuntimeError(SECRET)
    outer._gaiakeep_failure_detail = detail(credential=SECRET)
    outer.__context__ = inner
    assert transport.failure_detail(outer) == detail(native_phase='websocket_open')


def test_existing_outer_detail_precedes_cause_and_invalid_new_phase():
    inner = transport.mark_failure(TimeoutError(SECRET), 'rpc_receive')
    outer = transport.mark_failure(RuntimeError(SECRET), 'sdk_read')
    outer.__cause__ = inner
    assert transport.mark_failure(outer, SECRET) is outer
    assert transport.failure_detail(outer) == detail(native_phase='sdk_read', failure_kind='other')


@pytest.mark.parametrize('depth,found', [(7, True), (8, False)])
def test_exact_eight_exception_traversal_bound(depth, found):
    chain = [RuntimeError(SECRET) for _ in range(depth + 1)]
    for current, following in zip(chain, chain[1:]):
        current.__cause__ = following
    transport.mark_failure(chain[-1], 'rpc_receive')
    expected = detail(failure_kind='other') if found else None
    assert transport.failure_detail(chain[0]) == expected


def test_cause_traversal_is_bounded_and_cycle_safe():
    chain = [RuntimeError(SECRET) for _ in range(64)]
    for current, following in zip(chain, chain[1:]):
        current.__cause__ = following
    transport.mark_failure(chain[-1], 'rpc_receive')
    assert transport.failure_detail(chain[0]) is None
    chain[-1].__cause__ = chain[0]
    assert transport.failure_detail(chain[0]) is None
    assert transport.failure_detail(chain[0], 'sdk_read') == detail(native_phase='sdk_read', failure_kind='other')


def test_nearby_context_detail_is_found_without_examining_private_fields():
    chain = [RuntimeError(SECRET) for _ in range(4)]
    for current, following in zip(chain, chain[1:]):
        current.__context__ = following
    transport.mark_failure(chain[-1], 'stream_receive')
    assert transport.failure_detail(chain[0]) == detail(native_phase='stream_receive', failure_kind='other')


def test_short_cause_cycle_ends_without_recursing_or_inventing_detail():
    first, second = RuntimeError(SECRET), RuntimeError(SECRET)
    first.__cause__, second.__context__ = second, first
    assert transport.failure_detail(first) is None
    assert transport.failure_detail(first, 'sdk_read') == detail(native_phase='sdk_read', failure_kind='other')


def test_hostile_exception_attributes_are_best_effort_and_never_exported():
    class HostileError(Exception):
        def __getattribute__(self, name):
            if name in {'_gaiakeep_failure_detail', 'status', '__cause__', '__context__', 'retryable'}:
                raise RuntimeError(SECRET)
            return super().__getattribute__(name)

        def __setattr__(self, name, value):
            raise RuntimeError(SECRET)

        def __str__(self):
            raise AssertionError('private exception text was inspected')

    exc = HostileError(SECRET)
    assert transport.mark_failure(exc, 'sdk_read') is exc
    assert transport.failure_detail(exc) is None
    assert transport.failure_detail(exc, 'sdk_read') is None


def test_hostile_mapping_is_discarded_before_inspection():
    class HostileDict(dict):
        def __iter__(self):
            raise AssertionError('untrusted diagnostic iterated')

        def get(self, *args):
            raise AssertionError('untrusted diagnostic read')

    assert transport.validate_failure_detail(HostileDict(detail())) is None


def test_agent_error_optional_detail_preserves_verdict_message_and_copy():
    value = detail(native_status=500)
    exc = client.AgentError('unavailable', 'GaiaKeep is currently unavailable.', detail=value)
    value['native_phase'] = SECRET
    assert exc.verdict == 'unavailable' and str(exc) == 'GaiaKeep is currently unavailable.'
    assert client.failure(exc, mutation=False) == ('unavailable', 'GaiaKeep is currently unavailable.')
    assert transport.failure_detail(exc) == detail(native_status=500)
    assert transport.failure_detail(client.AgentError('unconfirmed', 'fixed-safe-message', detail=detail(extra=SECRET))) is None


def test_issue_sink_keeps_legacy_row_for_absent_or_malformed_optional_detail(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv('GAIAKEEP_ISSUE_LOG_DIRECTORY', str(tmp_path / 'issues'))
    caplog.set_level(logging.WARNING, logger=issue_log.__name__)
    sink = issue_log.IssueLog()
    for value in (None, detail(extra=SECRET), detail(native_status='504')):
        assert sink.record('gaiakeep_read_file', 'unavailable', dispatched=True, mutation=False, detail=value) is None
    events = [record.gaiakeep_issue for record in caplog.records if 'verdict' in record.gaiakeep_issue]
    assert len(events) == 3
    assert all(set(event) == {'timestamp_utc', 'tool', 'verdict', 'phase', 'mutation'} for event in events)
    rows = [line for line in (tmp_path / 'issues' / 'ISSUES.md').read_text().splitlines() if line.startswith('- ')]
    assert len(rows) == 3 and all('native_phase' not in line and 'failure_kind' not in line for line in rows)
    assert SECRET not in json.dumps(events) and SECRET not in '\n'.join(rows)


def test_valid_optional_labels_reach_file_and_structured_log_only(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv('GAIAKEEP_ISSUE_LOG_DIRECTORY', str(tmp_path / 'issues'))
    caplog.set_level(logging.WARNING, logger=issue_log.__name__)
    sink = issue_log.IssueLog()
    value = detail(native_status=504)
    assert sink.record('gaiakeep_read_file', 'unavailable', dispatched=True, mutation=False, detail=value) is None
    value['failure_kind'] = SECRET
    event = next(record.gaiakeep_issue for record in caplog.records if 'verdict' in record.gaiakeep_issue)
    assert {name: event[name] for name in ('native_phase', 'failure_kind', 'native_status')} == detail(native_status=504)
    assert event['tool'] == 'gaiakeep_read_file' and event['verdict'] == 'unavailable' and event['mutation'] is False
    row = (tmp_path / 'issues' / 'ISSUES.md').read_text()
    assert 'native_phase=rpc_receive' in row and 'failure_kind=timeout' in row and 'native_status=504' in row
    assert SECRET not in row and SECRET not in json.dumps(event)
