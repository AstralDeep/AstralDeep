"""Checks the operational Gaia failure mirror's closed metadata, bounded retention and file containment. It verifies real thread/process appends plus locking and storage failures without using application identities, databases or a Gaia service."""

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agents.gaiakeep import issue_log


@pytest.fixture
def sink(monkeypatch, tmp_path, caplog):
    path = tmp_path / 'issues'
    monkeypatch.setenv('GAIAKEEP_ISSUE_LOG_DIRECTORY', str(path))
    caplog.set_level(logging.WARNING, logger=issue_log.__name__)
    return path, issue_log.IssueLog()


def record(log, **changes):
    fields = {'tool_name': 'gaiakeep_core_whoami', 'verdict': 'unavailable', 'dispatched': True, 'mutation': False}
    fields.update(changes)
    assert log.record(**fields) is None


def rows(path):
    return [value for value in path.read_text().splitlines() if value.startswith('- ')]


def cold_start_process(path, barrier, results):
    events = []

    class Capture(logging.Handler):
        def emit(self, event):
            events.append(event.gaiakeep_issue)

    issue_log._LOGGER.addHandler(Capture())
    issue_log._LOGGER.setLevel(logging.WARNING)
    original = issue_log._file
    waited = False

    def simultaneous(target, directory, descriptor=None):
        nonlocal waited
        value = original(target, directory, descriptor)
        if not waited and target.name == 'ISSUES.lock' and value is None:
            waited = True
            barrier.wait(timeout=10)
        return value

    issue_log._file = simultaneous
    log = issue_log.IssueLog()
    log._directory = path
    result = log.record('gaiakeep_core_whoami', 'unavailable', dispatched=True, mutation=False)
    results.put({'primary_return_unchanged': result is None, 'first_create_barrier_reached': waited,
                 'ordinary_failures': sum('verdict' in event and 'sink' not in event for event in events),
                 'sink_failures': sum(event.get('sink') == 'unavailable' for event in events)})


def test_golden_utc_metadata_is_identical_in_file_structured_record_and_plain_formatter(sink, caplog):
    path, log = sink
    record(log)
    line = rows(path / 'ISSUES.md')[0]
    event = caplog.records[-1]
    details = event.gaiakeep_issue
    assert set(details) == {'timestamp_utc', 'tool', 'verdict', 'phase', 'mutation'}
    assert details['timestamp_utc'].endswith('Z') and details['phase'] == 'post_dispatch'
    assert details['tool'] == 'gaiakeep_core_whoami' and details['mutation'] is False
    formatted = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s').format(event)
    for value in ('gaiakeep_core_whoami', 'unavailable', 'phase=post_dispatch', 'mutation=false', details['timestamp_utc']):
        assert value in line and value in formatted
    if os.name != 'nt':
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert stat.S_IMODE((path / 'ISSUES.md').stat().st_mode) == 0o640


def test_existing_notes_are_preserved_and_pre_dispatch_mutation_is_explicit(sink):
    path, log = sink
    path.mkdir(mode=0o700)
    notes = b'# Operator notes\nExisting diagnostic note without a newline'
    (path / 'ISSUES.md').write_bytes(notes)
    if os.name != 'nt':
        (path / 'ISSUES.md').chmod(0o640)
    record(log, tool_name='gaiakeep_upload_file', verdict='unconfirmed', dispatched=False, mutation=True)
    content = (path / 'ISSUES.md').read_bytes()
    assert content.startswith(notes + b'\n- ')
    assert b'phase=pre_dispatch' in content and b'mutation=true' in content


def test_disabled_sink_still_emits_visible_closed_metadata(monkeypatch, caplog):
    monkeypatch.delenv('GAIAKEEP_ISSUE_LOG_DIRECTORY', raising=False)
    caplog.set_level(logging.WARNING, logger=issue_log.__name__)
    record(issue_log.IssueLog())
    assert len(caplog.records) == 1 and 'tool=gaiakeep_core_whoami' in caplog.text


@pytest.mark.parametrize('changes', [{'tool_name': 'token=private-secret'}, {'verdict': 'password=private-secret'},
                                   {'dispatched': 'private-secret'}, {'mutation': 'private-secret'}, {'mutation': True}])
def test_invalid_metadata_never_becomes_a_file_or_normal_log_value(sink, caplog, changes):
    path, log = sink
    record(log, **changes)
    assert not path.exists() and 'private-secret' not in caplog.text
    assert caplog.records[-1].gaiakeep_issue == {'sink': 'invalid_event'}


def test_bounded_rotation_keeps_notes_and_only_three_backups(sink, monkeypatch):
    path, log = sink
    monkeypatch.setattr(issue_log, 'MAX_BYTES', 256)
    path.mkdir(mode=0o700)
    initial = b'Existing operator note\n' * 8
    (path / 'ISSUES.md').write_bytes(initial)
    if os.name != 'nt':
        (path / 'ISSUES.md').chmod(0o640)
    record(log)
    assert (path / 'ISSUES.md.1').read_bytes() == initial
    for _ in range(8):
        record(log)
    retained = sorted(path.glob('ISSUES.md*'))
    assert {item.name for item in retained} == {'ISSUES.md', 'ISSUES.md.1', 'ISSUES.md.2', 'ISSUES.md.3'}
    assert all(0 < item.stat().st_size <= 256 for item in retained)


def test_existing_oversized_notes_are_not_destroyed(sink, monkeypatch, caplog):
    path, log = sink
    monkeypatch.setattr(issue_log, 'MAX_BYTES', 128)
    path.mkdir(mode=0o700)
    original = b'operator note' * 20
    (path / 'ISSUES.md').write_bytes(original)
    if os.name != 'nt':
        (path / 'ISSUES.md').chmod(0o640)
    record(log)
    assert (path / 'ISSUES.md').read_bytes() == original
    assert 'sink=unavailable' in caplog.text


def test_real_concurrent_threads_append_complete_rows(sink):
    path, log = sink
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda _value: record(log), range(30)))
    actual = rows(path / 'ISSUES.md')
    assert len(actual) == 30 and all(value.endswith('mutation=false') for value in actual)


def test_real_process_serialization_preserves_every_complete_row(sink):
    path, log = sink
    record(log)
    program = ('from agents.gaiakeep.issue_log import IssueLog\n'
               'log=IssueLog()\n'
               'for _ in range(6):log.record("gaiakeep_core_whoami","unavailable",dispatched=True,mutation=False)\n')
    environment = dict(os.environ, PYTHONPATH=str(Path(issue_log.__file__).parents[2]))
    children = [subprocess.Popen([sys.executable, '-c', program], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(3)]
    for child in children:
        output, error = child.communicate(timeout=20)
        assert child.returncode == 0 and not output and b'sink failed' not in error
    actual = rows(path / 'ISSUES.md')
    assert len(actual) == 19 and all(value.endswith('mutation=false') for value in actual)


def test_two_real_cold_start_processes_reconcile_initial_lock_creation_without_losing_rows(sink):
    path, _log = sink
    path.mkdir(mode=0o700)
    context = multiprocessing.get_context('spawn')
    barrier, results = context.Barrier(2), context.Queue()
    children = [context.Process(target=cold_start_process, args=(str(path), barrier, results)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        observations = [results.get(timeout=20) for _ in children]
        for child in children:
            child.join(timeout=10)
            assert child.exitcode == 0
        assert observations == [{'primary_return_unchanged': True, 'first_create_barrier_reached': True,
                                 'ordinary_failures': 1, 'sink_failures': 0}] * 2
        actual = rows(path / 'ISSUES.md')
        assert len(actual) == 2 and all(value.endswith('mutation=false') for value in actual)
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize('raw', ['relative', '/tmp/../escape', '/tmp/./escape', '\x00private-secret', '/tmp/\nprivate-secret'])
def test_traversal_and_control_paths_are_refused_without_exposure(sink, caplog, raw):
    path, log = sink
    log._directory = raw
    record(log)
    assert not path.exists() and 'private-secret' not in caplog.text
    assert 'sink=unavailable' in caplog.text


@pytest.mark.parametrize('name', ['ISSUES.md', 'ISSUES.md.1', 'ISSUES.lock'])
def test_nonregular_sink_files_are_refused(sink, name, caplog):
    path, log = sink
    path.mkdir(mode=0o700)
    (path / name).mkdir()
    record(log)
    assert (path / name).is_dir() and 'sink=unavailable' in caplog.text


def test_hard_linked_sink_is_refused_without_altering_target(sink, tmp_path, caplog):
    path, log = sink
    path.mkdir(mode=0o700)
    target = tmp_path / 'outside'
    target.write_bytes(b'unchanged')
    os.link(target, path / 'ISSUES.md')
    record(log)
    assert target.read_bytes() == b'unchanged' and 'sink=unavailable' in caplog.text


def test_reparse_or_symlink_metadata_is_rejected():
    assert issue_log._linked(SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400))
    assert issue_log._linked(SimpleNamespace(st_mode=stat.S_IFLNK))


@pytest.mark.parametrize('kind', ['parent_link', 'parent_writable', 'target_writable'])
def test_unsafe_parent_or_final_directory_is_rejected(sink, monkeypatch, kind):
    path, _log = sink
    path.mkdir(mode=0o700)
    real = Path.lstat
    monkeypatch.setattr(issue_log, '_WINDOWS', False)

    def inspected(target):
        info = real(target)
        if target == (path if kind == 'target_writable' else path.parent):
            return SimpleNamespace(st_mode=stat.S_IFLNK if kind == 'parent_link' else stat.S_IFDIR | 0o777,
                                   st_uid=1003)
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=0, st_dev=info.st_dev, st_ino=info.st_ino)

    monkeypatch.setattr(Path, 'lstat', inspected)
    with pytest.raises(ValueError):
        issue_log._directory(str(path))


@pytest.mark.parametrize('created', [True, False])
def test_only_exclusive_new_validated_files_receive_posix_readable_mode(sink, monkeypatch, created):
    path, _log = sink
    info = SimpleNamespace(st_dev=1, st_ino=2)
    sequence = iter([None if created else info, info])
    calls = []
    monkeypatch.setattr(issue_log, '_WINDOWS', False)
    monkeypatch.setattr(issue_log, '_file', lambda *_args: next(sequence))
    monkeypatch.setattr(issue_log.os, 'open', lambda *args, **kwargs: calls.append((args, kwargs)) or 7)
    monkeypatch.setattr(issue_log.os, 'fstat', lambda *_args: info)
    monkeypatch.setattr(issue_log.os, 'fchmod', lambda *args: calls.append(args), raising=False)
    assert issue_log._open(path / 'ISSUES.md', info) == 7
    assert bool(calls[0][0][1] & os.O_EXCL) is created
    assert calls[1:] == ([(7, 0o640)] if created else [])


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes and restrictive umask')
def test_restrictive_umask_keeps_new_logs_group_readable_without_widening_existing_notes(sink):
    path, log = sink
    path.mkdir(mode=0o2750)
    previous = os.umask(0o077)
    try:
        record(log)
        assert stat.S_IMODE((path / 'ISSUES.md').stat().st_mode) == 0o640
        assert stat.S_IMODE((path / 'ISSUES.lock').stat().st_mode) == 0o640
        assert (path / 'ISSUES.md').stat().st_gid == path.stat().st_gid
        (path / 'ISSUES.md').chmod(0o600)
        record(log)
        assert stat.S_IMODE((path / 'ISSUES.md').stat().st_mode) == 0o600
    finally:
        os.umask(previous)


@pytest.mark.parametrize('identity', [None, SimpleNamespace(st_dev=8, st_ino=9)])
def test_open_identity_changes_close_descriptor_without_writing(sink, monkeypatch, identity):
    path, _log = sink
    before = SimpleNamespace(st_dev=1, st_ino=2)
    sequence = iter([before, identity])
    closed = []
    monkeypatch.setattr(issue_log, '_file', lambda *_args: next(sequence))
    monkeypatch.setattr(issue_log.os, 'open', lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(issue_log.os, 'fstat', lambda *_args: before)
    monkeypatch.setattr(issue_log.os, 'close', closed.append)
    with pytest.raises(ValueError):
        issue_log._open(path / 'ISSUES.md', before)
    assert closed == [7]


def test_valid_exclusive_create_race_preserves_notes_and_never_widens_existing_permissions(sink, monkeypatch):
    path, _log = sink
    path.mkdir(mode=0o700)
    real_open = os.open
    calls = []

    def concurrent_create(target, flags, mode, **options):
        calls.append(flags)
        if len(calls) == 1:
            assert flags & os.O_EXCL
            target.write_bytes(b'concurrent operator notes')
            if os.name != 'nt':
                target.chmod(0o600)
        else:
            assert not flags & (os.O_CREAT | os.O_EXCL)
        return real_open(target, flags, mode, **options)

    monkeypatch.setattr(issue_log.os, 'open', concurrent_create)
    monkeypatch.setattr(issue_log.os, 'fchmod', lambda *_args: pytest.fail('existing raced file chmod'), raising=False)
    descriptor = issue_log._open(path / 'ISSUES.md', path.stat())
    os.close(descriptor)
    assert len(calls) == 2
    assert (path / 'ISSUES.md').read_bytes() == b'concurrent operator notes'
    if os.name != 'nt':
        assert stat.S_IMODE((path / 'ISSUES.md').stat().st_mode) == 0o600


@pytest.mark.parametrize('kind', ['link', 'writable', 'foreign_owner', 'hard_link', 'missing'])
def test_unsafe_exclusive_create_race_is_revalidated_and_refused_before_reopen(sink, monkeypatch, kind):
    path, _log = sink
    path.mkdir(mode=0o700)
    calls = []
    real_file = issue_log._file
    real_lstat = Path.lstat
    monkeypatch.setattr(issue_log, '_WINDOWS', False)
    monkeypatch.setattr(issue_log.os, 'geteuid', lambda: 1003, raising=False)
    target = path / 'ISSUES.md'

    def inspected(value):
        if value == target:
            return SimpleNamespace(st_mode=stat.S_IFLNK if kind == 'link' else stat.S_IFREG | (0o662 if kind == 'writable' else 0o640),
                                   st_nlink=2 if kind == 'hard_link' else 1, st_uid=9999 if kind == 'foreign_owner' else 1003)
        return real_lstat(value)

    def initial_missing(value, directory, descriptor=None):
        if not calls or kind == 'missing':
            return None
        return real_file(value, directory, descriptor)

    def raced_open(*args, **options):
        calls.append((args, options))
        raise FileExistsError

    monkeypatch.setattr(issue_log, '_file', initial_missing)
    monkeypatch.setattr(Path, 'lstat', inspected)
    monkeypatch.setattr(issue_log.os, 'open', raced_open)
    with pytest.raises(ValueError):
        issue_log._open(target, SimpleNamespace(st_uid=1003))
    assert len(calls) == 1


def test_foreign_owned_or_group_writable_files_are_rejected(sink, monkeypatch):
    path, _log = sink
    monkeypatch.setattr(issue_log, '_WINDOWS', False)
    monkeypatch.setattr(issue_log.os, 'geteuid', lambda: 0, raising=False)
    for uid, mode in ((9999, 0o100640), (0, 0o100662)):
        info = SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=1)
        monkeypatch.setattr(Path, 'lstat', lambda _path: info)
        with pytest.raises(ValueError):
            issue_log._file(path / 'ISSUES.md', SimpleNamespace(st_uid=1003))


def test_configured_sink_and_logger_exceptions_never_escape_or_leak(sink, monkeypatch, caplog):
    _path, log = sink
    monkeypatch.setattr(issue_log, '_append', lambda *_args: (_ for _ in ()).throw(OSError('/private-secret/credentials')))
    record(log)
    assert 'private-secret' not in caplog.text and 'tool=gaiakeep_core_whoami' in caplog.text
    assert 'sink=unavailable' in caplog.text
    monkeypatch.setattr(issue_log._LOGGER, 'warning', lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('private-secret')))
    record(log)


def test_lock_failure_is_bounded_and_safe(sink, monkeypatch, caplog):
    path, log = sink
    monkeypatch.setattr(issue_log, '_try_lock', lambda *_args: (_ for _ in ()).throw(OSError('private-secret')))
    moments = iter([0, 1, 3])
    monkeypatch.setattr(issue_log.time, 'monotonic', lambda: next(moments))
    monkeypatch.setattr(issue_log.time, 'sleep', lambda *_args: None)
    record(log)
    assert not (path / 'ISSUES.md').exists() and 'private-secret' not in caplog.text


def test_malformed_lock_file_is_preserved(sink, caplog):
    path, log = sink
    path.mkdir(mode=0o700)
    (path / 'ISSUES.lock').write_bytes(b'operator notes')
    if os.name != 'nt':
        (path / 'ISSUES.lock').chmod(0o640)
    record(log)
    assert (path / 'ISSUES.lock').read_bytes() == b'operator notes' and 'sink=unavailable' in caplog.text


def test_partial_writes_are_completed_and_zero_write_is_safe(sink, monkeypatch, caplog):
    path, log = sink
    real = issue_log.os.write
    monkeypatch.setattr(issue_log.os, 'write', lambda descriptor, data: real(descriptor, data[:7]))
    record(log)
    assert len(rows(path / 'ISSUES.md')) == 1
    monkeypatch.setattr(issue_log.os, 'write', lambda *_args: 0)
    record(log)
    assert len(rows(path / 'ISSUES.md')) == 1 and 'sink=unavailable' in caplog.text


def test_thread_lock_contention_never_changes_primary_outcome(sink, monkeypatch, caplog):
    path, log = sink
    monkeypatch.setattr(issue_log, '_THREAD_LOCK', SimpleNamespace(acquire=lambda **_kwargs: False))
    record(log)
    assert not path.exists() and 'sink=unavailable' in caplog.text


@pytest.mark.parametrize('windows', [True, False])
def test_standard_library_process_lock_and_unlock_mechanisms(monkeypatch, windows):
    calls = []
    monkeypatch.setattr(issue_log, '_WINDOWS', windows)
    monkeypatch.setattr(issue_log.os, 'lseek', lambda *args: calls.append(args))
    if windows:
        monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=lambda *args: calls.append(args)))
    else:
        monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *args: calls.append(args)))
    issue_log._try_lock(9)
    issue_log._unlock(9)
    assert calls == [(9, 0, os.SEEK_SET), (9, 1, 1), (9, 0, os.SEEK_SET), (9, 2, 1)] if windows else calls == [(9, 6), (9, 8)]


def test_no_sensitive_field_can_enter_the_public_record_api(sink):
    _path, log = sink
    with pytest.raises(TypeError):
        log.record('gaiakeep_core_whoami', 'unavailable', dispatched=False, mutation=False,
                   exception_message='private-secret', user_id='private-secret')
    assert 'private-secret' not in json.dumps(vars(log))
