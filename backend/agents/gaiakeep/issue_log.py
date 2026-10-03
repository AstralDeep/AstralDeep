"""Mirrors fixed GaiaKeep failure metadata to ordinary operational logging and an optional operator-owned rotating Markdown file. MCP dispatch supplies only catalog names and classifications; AstralPlane remains the authority for identity, auditing and durable application state."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import stat
import threading
import time

from agents.gaiakeep import catalog
from agents.gaiakeep.transport import validate_failure_detail

MAX_BYTES = 1 << 20
BACKUPS = 3
LOCK_TIMEOUT = 2.0
VERDICTS = frozenset({'not_configured', 'unsupported', 'auth_failed', 'invalid_argument',
                     'protocol_error', 'integrity_error', 'upstream_denied', 'unavailable', 'unconfirmed'})
_LOGGER = logging.getLogger(__name__)
_THREAD_LOCK = threading.Lock()
_WINDOWS = os.name == 'nt'
_FILE_NAMES = ('ISSUES.md', *(f'ISSUES.md.{index}' for index in range(1, BACKUPS + 1)))


def _warning(message, **metadata):
    try:
        labels = ' | '.join(f"{key}={str(metadata[key]).lower() if type(metadata[key]) is bool else metadata[key]}"
                            for key in ('timestamp_utc', 'tool', 'verdict', 'phase', 'mutation',
                                        'native_phase', 'failure_kind', 'native_status', 'sink')
                            if key in metadata)
        _LOGGER.warning('%s | %s', message, labels, extra={'gaiakeep_issue': metadata})
    except Exception:
        return


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0)
                                            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _identity(info):
    return info.st_dev, info.st_ino


def _directory(raw):
    if (type(raw) is not str or not raw or len(raw) > 4096
            or any(ord(value) < 32 or ord(value) == 127 for value in raw)
            or any(part in {'.', '..'} for part in re.split(r'[/\\]', raw))):
        raise ValueError
    path = Path(raw)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError
    for parent in reversed(path.parents):
        info = parent.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError
        if (not _WINDOWS and info.st_mode & 0o022
                and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)):
            raise ValueError
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (_linked(info) or not stat.S_ISDIR(info.st_mode)
            or (not _WINDOWS and info.st_mode & 0o022)):
        raise ValueError
    return path, info


def _file(path, directory, descriptor=None):
    try:
        info = (path.lstat() if descriptor is None
                else os.stat(path.name, dir_fd=descriptor, follow_symlinks=False))
    except FileNotFoundError:
        return None
    if (_linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (not _WINDOWS and (info.st_mode & 0o022
                                 or info.st_uid not in {directory.st_uid, os.geteuid()}))):
        raise ValueError
    return info


def _open(path, directory, directory_descriptor=None):
    before = _file(path, directory, directory_descriptor)
    flags = (os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    created = before is None
    if created:
        flags |= os.O_CREAT | os.O_EXCL
    target = path if directory_descriptor is None else path.name
    options = {} if directory_descriptor is None else {'dir_fd': directory_descriptor}
    try:
        descriptor = os.open(target, flags, 0o640, **options)
    except FileExistsError:
        if not created:
            raise
        before = _file(path, directory, directory_descriptor)
        if before is None:
            raise ValueError from None
        created = False
        descriptor = os.open(target, flags & ~(os.O_CREAT | os.O_EXCL), 0o640, **options)
    try:
        actual = os.fstat(descriptor)
        current = _file(path, directory, directory_descriptor)
        if (current is None or _identity(actual) != _identity(current)
                or (before is not None and _identity(before) != _identity(actual))):
            raise ValueError
        if created and not _WINDOWS:
            os.fchmod(descriptor, 0o640)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _try_lock(descriptor):
    if _WINDOWS:
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor):
    if _WINDOWS:
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def _locked(path, directory, directory_descriptor=None):
    descriptor = _open(path / 'ISSUES.lock', directory, directory_descriptor)
    acquired = False
    try:
        size = os.fstat(descriptor).st_size
        if size == 0:
            os.write(descriptor, b'\0')
        elif size != 1:
            raise ValueError
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                _try_lock(descriptor)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError from None
                time.sleep(0.01)
        lock = _file(path / 'ISSUES.lock', directory, directory_descriptor)
        if lock is None or _identity(lock) != _identity(os.fstat(descriptor)):
            raise ValueError
        yield
    finally:
        try:
            if acquired:
                _unlock(descriptor)
        finally:
            os.close(descriptor)


def _append_locked(path, directory, line, directory_descriptor):
    with _locked(path, directory, directory_descriptor):
        if _identity(path.lstat()) != _identity(directory) or path.resolve() != path:
            raise ValueError
        files = {name: _file(path / name, directory, directory_descriptor) for name in _FILE_NAMES}
        if any(info is not None and info.st_size > MAX_BYTES for info in files.values()):
            raise ValueError
        current = files['ISSUES.md']
        if current is not None and current.st_size + len(line) > MAX_BYTES:
            for index in range(BACKUPS, 0, -1):
                source = 'ISSUES.md' if index == 1 else f'ISSUES.md.{index - 1}'
                if files[source] is not None:
                    if directory_descriptor is None:
                        os.replace(path / source, path / f'ISSUES.md.{index}')
                    else:
                        os.replace(source, f'ISSUES.md.{index}', src_dir_fd=directory_descriptor,
                                   dst_dir_fd=directory_descriptor)
        descriptor = _open(path / 'ISSUES.md', directory, directory_descriptor)
        try:
            if _identity(path.lstat()) != _identity(directory):
                raise ValueError
            os.lseek(descriptor, 0, os.SEEK_END)
            remaining = memoryview(line)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.fsync(directory_descriptor)


def _append(raw, line):
    path, directory = _directory(raw)
    if _WINDOWS:
        _append_locked(path, directory, line, None)
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(descriptor)) != _identity(directory):
            raise ValueError
        _append_locked(path, directory, line, descriptor)
    finally:
        os.close(descriptor)


class IssueLog:
    def __init__(self):
        self._directory = os.getenv('GAIAKEEP_ISSUE_LOG_DIRECTORY')

    def record(self, tool_name, verdict, *, dispatched, mutation, detail=None):
        if (type(tool_name) is not str or tool_name not in catalog.TOOLS
                or type(verdict) is not str or verdict not in VERDICTS
                or type(dispatched) is not bool or type(mutation) is not bool
                or mutation != catalog.is_mutation(tool_name)):
            _warning('GaiaKeep issue log rejected invalid event metadata.', sink='invalid_event')
            return
        timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        metadata = {'timestamp_utc': timestamp, 'tool': tool_name, 'verdict': verdict,
                    'phase': 'post_dispatch' if dispatched else 'pre_dispatch', 'mutation': mutation}
        detail = validate_failure_detail(detail)
        if detail is not None:
            metadata.update(detail)
        _warning('GaiaKeep operation failed.', **metadata)
        if not self._directory:
            return
        try:
            suffix = (f" | native_phase={detail['native_phase']} | failure_kind={detail['failure_kind']}"
                      f" | native_status={detail['native_status'] if detail['native_status'] is not None else 'none'}"
                      if detail is not None else '')
            line = (f"\n- {timestamp} | tool={tool_name} | verdict={verdict} | phase={metadata['phase']} "
                    f"| mutation={'true' if mutation else 'false'}{suffix}\n").encode('ascii')
            if not _THREAD_LOCK.acquire(timeout=LOCK_TIMEOUT):
                raise TimeoutError
            try:
                _append(self._directory, line)
            finally:
                _THREAD_LOCK.release()
        except Exception:
            _warning('GaiaKeep issue log sink failed.', **metadata, sink='unavailable')
