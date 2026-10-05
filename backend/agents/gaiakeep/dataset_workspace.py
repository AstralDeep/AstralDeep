"""Transfers bounded versioned datasets through a private account workspace and verified SDK streams. Approved uploads use digest-bound descriptor snapshots; downloads publish a complete verified directory without replacing existing data."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from contextlib import ExitStack, contextmanager

from agents.gaiakeep import catalog, client

MAX_FILES = 256
MAX_BYTES = 1 << 30
MAX_ENTRIES = 1024
MAX_DEPTH = 16


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('The dataset operation exceeded its deadline.')


def _reference(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise client.AgentError('invalid_argument', 'Choose a dataset reference of at most 64 letters, digits, underscores or hyphens.')
    return value


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_uid, info.st_nlink)


def _directory(parent, name, *, create=False, private=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise client.AgentError('invalid_argument', 'Dataset directory symbolic links are refused.') from exc
        if exc.errno == errno.ENOENT:
            raise client.AgentError('invalid_argument', 'The account workspace or dataset reference does not exist.') from exc
        raise
    try:
        info = os.fstat(descriptor)
        if (info.st_uid != os.geteuid() or info.st_mode & (0o077 if private else 0o022)
                or not stat.S_ISDIR(info.st_mode)):
            raise client.AgentError('invalid_argument', 'Dataset directories must be owned by your account and protected from other writers.')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _workspace(create=False):
    if os.name != 'posix' or not Path('/proc/self/fd').is_dir() or not hasattr(os, 'O_NOFOLLOW'):
        raise client.AgentError('unsupported', 'Local dataset transfers require the qualified Linux SSH account.')
    descriptors = []
    try:
        home = Path.home()
        if home.resolve() != home:
            raise client.AgentError('invalid_argument', 'The dataset account home must not contain symbolic links.')
        descriptors.append(_directory(None, str(home)))
        descriptors.append(_directory(descriptors[-1], '.gaiakeep', private=True))
        descriptors.append(_directory(descriptors[-1], 'astral-workspace', create=create, private=True))
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _path(descriptor, name=''):
    return f'/proc/self/fd/{descriptor}/' + name


def _manifest(files):
    files = sorted(files, key=lambda item: item['path'])
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'files': files, 'file_count': len(files), 'byte_count': sum(item['size'] for item in files),
            'manifest_sha256': hashlib.sha256(encoded).hexdigest()}


def _collect(descriptor, deadline, *, snapshots=None, stack=None):
    files, entries = [], 0

    def walk(directory, prefix='', depth=0):
        nonlocal entries
        before_directory = os.fstat(directory)
        if depth > MAX_DEPTH:
            raise client.AgentError('invalid_argument', 'Dataset nesting exceeds the allowed depth.')
        names = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                _check_deadline(deadline)
                entries += 1
                if entries > MAX_ENTRIES:
                    raise client.AgentError('invalid_argument', 'The dataset contains too many directory entries.')
                names.append(entry.name)
        for name in sorted(names):
            path = catalog.relative_path(prefix + name)
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child = _directory(directory, name)
                try:
                    walk(child, path + '/', depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(before.st_mode):
                if (before.st_uid != os.geteuid() or before.st_nlink != 1 or before.st_mode & 0o022
                        or len(files) >= MAX_FILES or before.st_size > MAX_BYTES
                        or sum(item['size'] for item in files) + before.st_size > MAX_BYTES):
                    raise client.AgentError('invalid_argument', 'Dataset files must be private to your account, within 256 files and 1 GiB.')
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    if _identity(os.fstat(source)) != _identity(before):
                        raise client.AgentError('integrity_error', 'A dataset file changed during inspection.')
                    snapshot = (stack.enter_context(tempfile.TemporaryFile(dir=_path(snapshots)))
                                if snapshots is not None else None)
                    digest, count = hashlib.sha256(), 0
                    while True:
                        _check_deadline(deadline)
                        block = os.read(source, 1 << 20)
                        if not block:
                            break
                        count += len(block)
                        if count > before.st_size:
                            raise client.AgentError('integrity_error', 'A dataset file changed during inspection.')
                        digest.update(block)
                        if snapshot is not None:
                            snapshot.write(block)
                    if (count != before.st_size or _identity(os.fstat(source)) != _identity(before)
                            or _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _identity(before)):
                        raise client.AgentError('integrity_error', 'A dataset file changed during inspection.')
                    if snapshot is not None:
                        snapshot.flush()
                        snapshot.seek(0)
                        os.fchmod(snapshot.fileno(), 0o400)
                    files.append({'path': path, 'size': count, 'sha256': digest.hexdigest(),
                                  **({'_snapshot': _path(snapshot.fileno()).rstrip('/')} if snapshot is not None else {})})
                finally:
                    os.close(source)
            else:
                raise client.AgentError('invalid_argument', 'Dataset symbolic links and special files are refused.')
            if _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _identity(before):
                raise client.AgentError('integrity_error', 'The dataset changed during inspection.')
        if _identity(os.fstat(directory)) != _identity(before_directory):
            raise client.AgentError('integrity_error', 'The dataset changed during inspection.')

    walk(descriptor)
    if not files:
        raise client.AgentError('invalid_argument', 'The dataset has no files.')
    return files


def inspect(dataset_ref, deadline):
    dataset_ref = _reference(dataset_ref)
    with _workspace() as workspace:
        descriptor = _directory(workspace, dataset_ref)
        try:
            before = os.fstat(descriptor)
            files = _collect(descriptor, deadline)
            if _identity(os.stat(dataset_ref, dir_fd=workspace, follow_symlinks=False)) != _identity(before):
                raise client.AgentError('integrity_error', 'The dataset reference changed during inspection.')
            return dict(_manifest(files), dataset_ref=dataset_ref)
        finally:
            os.close(descriptor)


def upload(core, arguments, deadline, on_dispatch=None):
    dataset_ref = _reference(arguments['dataset_ref'])
    with _workspace() as workspace, ExitStack() as stack:
        descriptor = _directory(workspace, dataset_ref)
        try:
            before = os.fstat(descriptor)
            snapshots = _collect(descriptor, deadline, snapshots=workspace, stack=stack)
            manifest = _manifest([{k: v for k, v in item.items() if k != '_snapshot'} for item in snapshots])
            if (manifest['manifest_sha256'] != arguments['manifest_sha256']
                    or _identity(os.stat(dataset_ref, dir_fd=workspace, follow_symlinks=False)) != _identity(before)):
                raise client.AgentError('integrity_error', 'The dataset changed since approval; inspect it and request a new approval.')
            prefix = arguments.get('prefix')
            if prefix:
                prefix = catalog.relative_path(prefix) + '/'
            files = {(prefix or '') + item['path']: item['_snapshot'] for item in snapshots}
            _check_deadline(deadline)
            if on_dispatch is not None:
                on_dispatch()
            result = core.ingest(files, collection_id=arguments['collection_id'], branch=arguments.get('branch', 'main'),
                                 base_vid=arguments.get('base_vid', arguments.get('expected_head')),
                                 expected_head=arguments.get('expected_head'), request_id=arguments['request_id'],
                                 note=arguments.get('note'), mode='1a', legacy_publish='staged')
            publication = client.publication_result(core, result)
            return {'dataset_ref': dataset_ref, 'manifest_sha256': manifest['manifest_sha256'],
                    'file_count': manifest['file_count'], 'byte_count': manifest['byte_count'], 'publication': publication}
        finally:
            os.close(descriptor)


def _version_files(core, vid, deadline):
    files, cursor, pages, total = {}, None, 0, 0
    while True:
        _check_deadline(deadline)
        params = {'vid': vid, 'limit': MAX_FILES + 1}
        if cursor is not None:
            params['after'] = cursor
        reply = client.execute(core, 'core.list', params)
        rows = reply.get('files')
        if not isinstance(rows, dict) or len(rows) > MAX_FILES + 1:
            raise client.AgentError('protocol_error', 'The version listing is malformed or exceeds the dataset bound.')
        for path, value in sorted(rows.items()):
            catalog.relative_path(path)
            size = value.get('size') if isinstance(value, dict) else None
            if (type(size) not in {int, str} or not str(size).isdigit() or len(str(size)) > 19
                    or path in files or cursor is not None and path <= cursor):
                raise client.AgentError('protocol_error', 'The version listing has invalid sizes or pagination.')
            size = int(size)
            total += size
            if len(files) >= MAX_FILES or total > MAX_BYTES:
                raise client.AgentError('invalid_argument', 'The version exceeds 256 files or 1 GiB; transfer a smaller version.')
            files[path] = size
        next_cursor = reply.get('next')
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise client.AgentError('protocol_error', 'The version listing has an invalid cursor.')
        if next_cursor in {None, ''}:
            break
        if not isinstance(next_cursor, str) or not rows or next_cursor != max(rows) or pages >= MAX_FILES:
            raise client.AgentError('protocol_error', 'The version listing did not make bounded progress.')
        cursor, pages = next_cursor, pages + 1
    if not files:
        raise client.AgentError('protocol_error', 'The version must contain a nonempty valid file tree.')
    entries = set(files)
    for path in files:
        parts = path.split('/')
        if len(parts) - 1 > MAX_DEPTH:
            raise client.AgentError('invalid_argument', 'Dataset nesting exceeds the allowed depth.')
        entries.update('/'.join(parts[:i]) for i in range(1, len(parts)))
        if len(entries) > MAX_ENTRIES:
            raise client.AgentError('invalid_argument', 'The dataset contains too many directory entries.')
        if any('/'.join(parts[:i]) in files for i in range(1, len(parts))):
            raise client.AgentError('protocol_error', 'The version contains conflicting file paths.')
    return files


@contextmanager
def _parents(descriptor, path):
    owned = []
    try:
        for part in path.split('/')[:-1]:
            descriptor = _directory(descriptor, part, create=True)
            owned.append(descriptor)
        yield descriptor
    finally:
        for descriptor in reversed(owned):
            os.close(descriptor)


def _publish(workspace, source, destination, descriptor=None):
    identity = _identity(os.fstat(descriptor)) if descriptor is not None else None
    if identity is not None and _identity(os.stat(source, dir_fd=workspace, follow_symlinks=False)) != identity:
        raise client.AgentError('integrity_error', 'The staged dataset changed before publication.')
    function = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if function is None:
        raise client.AgentError('unsupported', 'This Linux account cannot atomically publish a dataset without replacement.')
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(workspace, os.fsencode(source), workspace, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise client.AgentError('invalid_argument', 'The dataset reference already exists; choose a new one.')
        raise OSError(code, 'The dataset directory could not be published.')
    if identity is not None and _identity(os.stat(destination, dir_fd=workspace, follow_symlinks=False)) != _identity(os.fstat(descriptor)):
        raise client.AgentError('integrity_error', 'The staged dataset changed during publication.')
    os.fsync(workspace)


def _same_file(core, vid, path, opened, size):
    from gaiakeep.transfer import same_file

    return same_file(core.transfers, vid, path, opened, size)


def _download_fingerprint(core, parent, path, vid, size, deadline):
    name = path.split('/')[-1]
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_nlink != 1
                or before.st_mode & 0o022 or before.st_size != size):
            raise client.AgentError('integrity_error', 'The downloaded file is outside its approved file boundary.')
        digest, count = hashlib.sha256(), 0
        while True:
            _check_deadline(deadline)
            block = os.read(descriptor, 1 << 20)
            if not block:
                break
            count += len(block)
            if count > size:
                raise client.AgentError('integrity_error', 'The downloaded file changed during verification.')
            digest.update(block)
        if not _same_file(core, vid, path, _path(descriptor).rstrip('/'), size):
            raise client.AgentError('integrity_error', 'The downloaded file does not match its signed version proof.')
        _check_deadline(deadline)
        if (count != size or _identity(os.fstat(descriptor)) != _identity(before)
                or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(before)):
            raise client.AgentError('integrity_error', 'The downloaded file changed during verification.')
        return {'path': path, 'size': size, 'sha256': digest.hexdigest()}
    finally:
        os.close(descriptor)


def download(core, arguments, deadline):
    dataset_ref = _reference(arguments['dataset_ref'])
    with _workspace(create=True) as workspace:
        try:
            os.stat(dataset_ref, dir_fd=workspace, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise client.AgentError('invalid_argument', 'The dataset reference already exists; choose a new one.')
        files = _version_files(core, arguments['vid'], deadline)
        with tempfile.TemporaryDirectory(prefix='.download-', dir=_path(workspace)) as directory:
            name = Path(directory).name
            descriptor = _directory(workspace, name, private=True)
            try:
                proven = []
                for path, size in files.items():
                    _check_deadline(deadline)
                    with _parents(descriptor, path) as parent:
                        reply = core.download(arguments['vid'], path, _path(parent, path.split('/')[-1]), length=size)
                        if not isinstance(reply, dict) or reply.get('verified') is not True or reply.get('bytes') != size:
                            raise client.AgentError('integrity_error', 'The dataset file did not receive a whole-file integrity proof.')
                        proven.append(_download_fingerprint(core, parent, path, arguments['vid'], size, deadline))
                manifest = _manifest(_collect(descriptor, deadline))
                if manifest != _manifest(proven):
                    raise client.AgentError('integrity_error', 'The downloaded dataset differs from its version listing.')
                _check_deadline(deadline)
                _publish(workspace, name, dataset_ref, descriptor)
                if (_manifest(_collect(descriptor, deadline)) != manifest
                        or _identity(os.stat(dataset_ref, dir_fd=workspace, follow_symlinks=False)) != _identity(os.fstat(descriptor))):
                    raise client.AgentError('integrity_error', 'The downloaded dataset changed during publication.')
                return dict(manifest, dataset_ref=dataset_ref, vid=arguments['vid'], verified=True)
            finally:
                os.close(descriptor)
