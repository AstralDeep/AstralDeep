"""Exercises confined dataset snapshots, approved content fingerprints and atomic verified local publication. Deterministic filesystem and SDK fixtures cover races, denials and cleanup without contacting storage services."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from agents.gaiakeep import client, dataset_workspace as workspace

POSIX = pytest.mark.skipif(os.name != 'posix' or not Path('/proc/self/fd').is_dir(), reason='Linux descriptor confinement')


@pytest.fixture
def local(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    directory = home / '.gaiakeep' / 'astral-workspace'
    directory.mkdir(parents=True, mode=0o700)
    (home / '.gaiakeep').chmod(0o700)
    directory.chmod(0o700)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    source = directory / 'run42'
    (source / 'nested').mkdir(parents=True, mode=0o700)
    (source / 'metrics.json').write_bytes(b'{"score":1}')
    (source / 'nested' / 'model.bin').write_bytes(b'model-bytes')
    monkeypatch.setattr(workspace, '_same_file',
                        lambda core, vid, path, opened, size: Path(opened).read_bytes() == core.files[path])
    return SimpleNamespace(home=home, root=directory, source=source, deadline=time.monotonic() + 60)


class Core:
    def __init__(self):
        self.files = {'metrics.json': b'{"score":2}', 'nested/model.bin': b'new-model'}
        self.calls, self.uploads, self.downloads = [], [], []
        self.download_hook = None
        self.transfers = self

    def call(self, action, params, timeout=None):
        self.calls.append((action, params))
        return {'files': json.dumps({path: {'size': len(data)} for path, data in self.files.items()})}

    def ingest(self, files, **arguments):
        copied = {path: Path(source).read_bytes() for path, source in files.items()}
        self.uploads.append((copied, arguments, dict(files)))
        return SimpleNamespace(vid='version-42', request_id=arguments['request_id'])

    def download(self, vid, path, destination, length=None):
        self.downloads.append((vid, path, destination, length))
        if self.download_hook is not None:
            return self.download_hook(vid, path, destination, length)
        Path(destination).write_bytes(self.files[path])
        return {'verified': True, 'bytes': len(self.files[path])}


def arguments(local, **changes):
    manifest = workspace.inspect('run42', local.deadline)
    return {'dataset_ref': 'run42', 'collection_id': 'runs', 'manifest_sha256': manifest['manifest_sha256'],
            'request_id': 'request_run42_0000001', **changes}


@pytest.mark.parametrize('value', ['', '../escape', '/absolute', 'a/b', '.hidden', 'a' * 65, None, 1])
def test_reference_is_a_closed_token(value):
    with pytest.raises(client.AgentError):
        workspace._reference(value)


def test_manifest_is_deterministic_and_binds_paths_sizes_and_bytes():
    rows = [{'path': 'b', 'size': 1, 'sha256': 'b' * 64}, {'path': 'a', 'size': 2, 'sha256': 'a' * 64}]
    assert workspace._manifest(rows) == workspace._manifest(list(reversed(rows)))
    first = workspace._manifest(rows)
    assert first['byte_count'] == 3 and first['file_count'] == 2
    for field, value in (('path', 'changed'), ('size', 3), ('sha256', 'c' * 64)):
        changed = [dict(rows[0], **{field: value}), rows[1]]
        assert workspace._manifest(changed)['manifest_sha256'] != first['manifest_sha256']


def test_nonlinux_local_workspace_is_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr(workspace.os, 'name', 'nt')
    with pytest.raises(client.AgentError) as error, workspace._workspace():
        pass
    assert error.value.verdict == 'unsupported'


@POSIX
def test_inspection_and_atomic_augmented_upload_keep_approved_snapshot(local):
    core = Core()
    approved = arguments(local, expected_head='original-version', prefix='runs/42', note='completed training run')
    manifest = workspace.inspect('run42', local.deadline)
    assert [file['path'] for file in manifest['files']] == ['metrics.json', 'nested/model.bin']
    assert manifest['files'][1]['sha256'] == hashlib.sha256(b'model-bytes').hexdigest()
    result = workspace.upload(core, approved, local.deadline,
                              lambda: (local.source / 'nested/model.bin').write_bytes(b'after-approval'))
    copied, sent, paths = core.uploads[0]
    assert copied == {'runs/42/metrics.json': b'{"score":1}', 'runs/42/nested/model.bin': b'model-bytes'}
    assert sent['base_vid'] == sent['expected_head'] == 'original-version'
    assert sent['note'] == 'completed training run' and sent['mode'] == '1a'
    assert result['publication']['vid'] == 'version-42' and result['manifest_sha256'] == approved['manifest_sha256']
    assert all(not Path(path).exists() for path in paths.values())
    assert [path.name for path in local.root.iterdir()] == ['run42']


@POSIX
def test_changed_bytes_require_new_approval_before_remote_io(local):
    core = Core()
    approved = arguments(local)
    (local.source / 'metrics.json').write_bytes(b'changed')
    with pytest.raises(client.AgentError, match='changed since approval'):
        workspace.upload(core, approved, local.deadline)
    assert not core.uploads and not core.calls


@POSIX
def test_source_write_during_snapshot_is_refused(local, monkeypatch):
    read = os.read
    changed = []
    def replacing(descriptor, size):
        data = read(descriptor, size)
        if data and not changed:
            changed.append(True)
            (local.source / 'metrics.json').write_bytes(b'{"score":1}extra')
        return data
    monkeypatch.setattr(os, 'read', replacing)
    with pytest.raises(client.AgentError, match='changed during inspection'):
        workspace.inspect('run42', local.deadline)


@POSIX
@pytest.mark.parametrize('kind', ['reference', 'directory', 'file', 'hardlink', 'fifo', 'writers', 'workspace-mode', 'owner'])
def test_link_special_file_and_account_boundaries_fail_closed(local, tmp_path, monkeypatch, kind):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_bytes(b'secret')
    if kind == 'reference':
        (local.root / 'link').symlink_to(outside, target_is_directory=True)
    elif kind == 'directory':
        (local.source / 'link').symlink_to(outside, target_is_directory=True)
    elif kind == 'file':
        (local.source / 'link').symlink_to(outside / 'secret')
    elif kind == 'hardlink':
        os.link(outside / 'secret', local.source / 'link')
    elif kind == 'fifo':
        os.mkfifo(local.source / 'pipe')
    elif kind == 'writers':
        (local.source / 'metrics.json').chmod(0o666)
    elif kind == 'workspace-mode':
        local.root.chmod(0o755)
    else:
        current = os.geteuid()
        monkeypatch.setattr(os, 'geteuid', lambda: current + 1)
    with pytest.raises(client.AgentError):
        workspace.inspect('link' if kind == 'reference' else 'run42', local.deadline)
    assert (outside / 'secret').read_bytes() == b'secret'


@POSIX
@pytest.mark.parametrize('constant,value', [('MAX_FILES', 1), ('MAX_BYTES', 1), ('MAX_ENTRIES', 1), ('MAX_DEPTH', 0)])
def test_inspection_resource_bounds(local, monkeypatch, constant, value):
    monkeypatch.setattr(workspace, constant, value)
    with pytest.raises(client.AgentError):
        workspace.inspect('run42', local.deadline)


@POSIX
def test_empty_missing_and_expired_datasets_are_refused(local, monkeypatch):
    (local.root / 'empty').mkdir()
    for reference in ('empty', 'missing'):
        with pytest.raises(client.AgentError):
            workspace.inspect(reference, local.deadline)
    monkeypatch.setattr(workspace.time, 'monotonic', lambda: local.deadline)
    with pytest.raises(TimeoutError):
        workspace.inspect('run42', local.deadline)


@POSIX
def test_complete_verified_download_is_published_without_replacing_data(local):
    core = Core()
    result = workspace.download(core, {'dataset_ref': 'download42', 'vid': 'immutable-version'}, local.deadline)
    assert result['verified'] is True and result['vid'] == 'immutable-version' and result['file_count'] == 2
    assert (local.root / 'download42/nested/model.bin').read_bytes() == core.files['nested/model.bin']
    assert all(length == len(core.files[path]) for _, path, _, length in core.downloads)
    assert workspace.inspect('download42', local.deadline)['manifest_sha256'] == result['manifest_sha256']
    calls = len(core.calls)
    with pytest.raises(client.AgentError, match='already exists'):
        workspace.download(core, {'dataset_ref': 'download42', 'vid': 'other'}, local.deadline)
    assert len(core.calls) == calls


@POSIX
@pytest.mark.parametrize('failure', ['unverified', 'wrong-size', 'corrupt-size', 'exception'])
def test_failed_download_removes_staging_and_never_publishes(local, failure):
    core = Core()
    def failed(vid, path, destination, length):
        Path(destination).write_bytes(core.files[path] + (b'changed' if failure == 'corrupt-size' else b''))
        if failure == 'exception':
            raise OSError('private-path-and-data')
        return {'verified': failure != 'unverified', 'bytes': length + (1 if failure == 'wrong-size' else 0)}
    core.download_hook = failed
    with pytest.raises((client.AgentError, OSError)):
        workspace.download(core, {'dataset_ref': 'failed42', 'vid': 'v'}, local.deadline)
    assert [path.name for path in local.root.iterdir()] == ['run42']


@POSIX
def test_destination_created_during_download_is_not_replaced(local):
    core = Core()
    def created(vid, path, destination, length):
        Path(destination).write_bytes(core.files[path])
        (local.root / 'raced42').mkdir(exist_ok=True)
        (local.root / 'raced42/keep').write_bytes(b'keep')
        return {'verified': True, 'bytes': length}
    core.download_hook = created
    with pytest.raises(client.AgentError, match='already exists'):
        workspace.download(core, {'dataset_ref': 'raced42', 'vid': 'v'}, local.deadline)
    assert (local.root / 'raced42/keep').read_bytes() == b'keep'
    assert sorted(path.name for path in local.root.iterdir()) == ['raced42', 'run42']


@POSIX
def test_nested_directory_replacement_cannot_redirect_sdk_write(local, tmp_path):
    core = Core()
    core.files = {'nested/file': b'allowed'}
    outside = tmp_path / 'outside'
    outside.mkdir()
    def raced(vid, path, destination, length):
        stages = [item for item in local.root.iterdir() if item.name.startswith('.download-')]
        stage = stages[0]
        (stage / 'nested').rename(stage / 'old')
        (stage / 'nested').symlink_to(outside, target_is_directory=True)
        Path(destination).write_bytes(b'allowed')
        return {'verified': True, 'bytes': length}
    core.download_hook = raced
    with pytest.raises(client.AgentError):
        workspace.download(core, {'dataset_ref': 'race42', 'vid': 'v'}, local.deadline)
    assert not list(outside.iterdir()) and not (local.root / 'race42').exists()


@POSIX
def test_file_changed_while_later_file_downloads_cannot_be_published(local):
    core = Core()
    def changed(vid, path, destination, length):
        Path(destination).write_bytes(core.files[path])
        if path == 'nested/model.bin':
            stages = [item for item in local.root.iterdir() if item.name.startswith('.download-')]
            (stages[0] / 'metrics.json').write_bytes(b'{"score":9}')
        return {'verified': True, 'bytes': length}
    core.download_hook = changed
    with pytest.raises(client.AgentError, match='differs'):
        workspace.download(core, {'dataset_ref': 'changed42', 'vid': 'v'}, local.deadline)
    assert not (local.root / 'changed42').exists()


@POSIX
def test_false_whole_file_proof_is_refused_even_if_sdk_download_returned_verified(local, monkeypatch):
    monkeypatch.setattr(workspace, '_same_file', lambda *args: False)
    with pytest.raises(client.AgentError, match='signed version proof'):
        workspace.download(Core(), {'dataset_ref': 'bad-proof', 'vid': 'v'}, local.deadline)
    assert not (local.root / 'bad-proof').exists()


@POSIX
def test_download_accepts_proven_empty_files(local):
    core = Core()
    core.files = {'empty': b''}
    result = workspace.download(core, {'dataset_ref': 'empty-file', 'vid': 'v'}, local.deadline)
    assert result['files'] == [{'path': 'empty', 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()}]


@POSIX
def test_inspect_and_download_preserve_json_literal_names(local):
    name = '["metrics"]'
    (local.source / name).write_bytes(b'metrics')
    inspected = workspace.inspect('run42', local.deadline)
    assert name in [item['path'] for item in inspected['files']]
    core = Core()
    core.files = {name: b'metrics'}
    downloaded = workspace.download(core, {'dataset_ref': 'json-name', 'vid': 'v'}, local.deadline)
    assert downloaded['files'][0]['path'] == name
    assert client.clean_dataset_result(downloaded) == downloaded


@pytest.mark.parametrize('reply', [None, {}, {'files': []}, {'files': {'../bad': {'size': 1}}},
                                  {'files': {'a': {'size': True}}}, {'files': {'a': {'size': -1}}},
                                  {'files': {'a': {'size': 'x'}}}, {'files': {'a': {'size': 1}}, 'next': 'other'},
                                  {'files': {'a': {'size': 1}, 'a/b': {'size': 1}}},
                                  *[{'files': {'a': {'size': 1}}, 'next': value} for value in (0, False, [], {})]])
def test_version_manifest_refuses_invalid_paths_sizes_and_cursors(reply):
    if isinstance(reply, dict) and 'files' in reply:
        reply = dict(reply, files=json.dumps(reply['files']))
    core = SimpleNamespace(call=lambda *args, **kwargs: reply)
    with pytest.raises((client.AgentError, client.ProtocolError, ValueError)):
        workspace._version_files(core, 'v', time.monotonic() + 60)


def test_version_pages_are_bounded_and_monotonic(monkeypatch):
    replies = iter([{'files': '{"a":{"size":1}}', 'next': 'a'}, {'files': '{"b":{"size":2}}'}])
    core = SimpleNamespace(call=lambda *args, **kwargs: next(replies))
    assert workspace._version_files(core, 'v', time.monotonic() + 60) == {'a': 1, 'b': 2}
    for constant in ('MAX_FILES', 'MAX_BYTES'):
        with monkeypatch.context() as patch:
            patch.setattr(workspace, constant, 0)
            with pytest.raises(client.AgentError):
                workspace._version_files(Core(), 'v', time.monotonic() + 60)
    core = SimpleNamespace(call=lambda *args, **kwargs: {'files': '{"a":{"size":1}}', 'next': 'a'})
    with pytest.raises(client.AgentError, match='invalid sizes or pagination'):
        workspace._version_files(core, 'v', time.monotonic() + 60)


def test_no_replace_publication_requires_supported_atomic_rename(monkeypatch):
    monkeypatch.setattr(ctypes, 'CDLL', lambda *args, **kwargs: SimpleNamespace())
    with pytest.raises(client.AgentError) as error:
        workspace._publish(0, 'source', 'destination')
    assert error.value.verdict == 'unsupported'
    class FailedRename:
        def __call__(self, *args):
            ctypes.set_errno(errno.EIO)
            return -1
    monkeypatch.setattr(ctypes, 'CDLL', lambda *args, **kwargs: SimpleNamespace(renameat2=FailedRename()))
    with pytest.raises(OSError) as error:
        workspace._publish(0, 'source', 'destination')
    assert error.value.errno == errno.EIO


@POSIX
@pytest.mark.parametrize('constant,value,files', [('MAX_DEPTH', 0, {'nested/file': b'data'}),
                                                ('MAX_ENTRIES', 1, {'nested/file': b'data'})])
def test_download_remote_tree_bounds_are_validated_before_sdk_io(local, monkeypatch, constant, value, files):
    core = Core()
    core.files = files
    monkeypatch.setattr(workspace, constant, value)
    with pytest.raises(client.AgentError):
        workspace.download(core, {'dataset_ref': 'bounded42', 'vid': 'v'}, local.deadline)
    assert not core.downloads and [path.name for path in local.root.iterdir()] == ['run42']


@POSIX
@pytest.mark.parametrize('when', ['before', 'during', 'after'])
def test_staging_namespace_replacement_cannot_return_verified_success(local, monkeypatch, when):
    core = Core()
    publish = workspace._publish
    old = local.root / 'old-stage'
    def replaced(root, source, destination, descriptor):
        stage = local.root / source
        if when == 'before':
            stage.rename(old)
            stage.mkdir()
            (stage / 'replacement').write_bytes(b'not-verified')
        if when == 'during':
            function = ctypes.CDLL(None, use_errno=True).renameat2
            class RacedRename:
                def __call__(self, *args):
                    stage.rename(old)
                    stage.mkdir()
                    (stage / 'replacement').write_bytes(b'not-verified')
                    return function(*args)
            with monkeypatch.context() as patch:
                patch.setattr(ctypes, 'CDLL', lambda *args, **kwargs: SimpleNamespace(renameat2=RacedRename()))
                return publish(root, source, destination, descriptor)
        result = publish(root, source, destination, descriptor)
        if when == 'after':
            (local.root / destination / 'metrics.json').write_bytes(b'changed')
        return result
    monkeypatch.setattr(workspace, '_publish', replaced)
    with pytest.raises(client.AgentError, match='changed'):
        workspace.download(core, {'dataset_ref': 'stage42', 'vid': 'v'}, local.deadline)


@POSIX
def test_qualified_sdk_destination_replacement_does_not_follow_symlinks(tmp_path):
    Transfers = pytest.importorskip('gaiakeep.transfer').Transfers

    data = b'verified-data'
    digest = 'SHA256:' + hashlib.sha256(data).hexdigest()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'keep-secret')
    destination = tmp_path / 'target'
    destination.symlink_to(outside)
    transfer = object.__new__(Transfers)
    transfer.flows, transfer.chunk = 1, 16
    transfer.client = SimpleNamespace(file_info=lambda *args: {'size': len(data), 'file_hash': digest})
    transfer.lane = lambda index: None
    def copied(lane, descriptor, offset, vid, path, start, length, keys):
        os.pwrite(descriptor, data[start:start + length], offset)
        return {'size': len(data), 'file_hash': digest}
    transfer._get_range = copied
    assert transfer.download('v', 'p', destination, length=len(data))['verified'] is True
    assert destination.read_bytes() == data and outside.read_bytes() == b'keep-secret'


@POSIX
@pytest.mark.parametrize('proof', ['hash', 'keyed'])
def test_qualified_sdk_signed_same_file_proof_verifies_pinned_descriptor(tmp_path, proof):
    transfer = pytest.importorskip('gaiakeep.transfer')
    data = b'verified-data'
    source = tmp_path / 'source'
    source.write_bytes(data)
    def reply(action, params):
        assert action == 'core.get' and params['vid'] == 'version-42' and params['path'] == 'file'
        shared = b'fixture-shared-secret'
        row = {'size': len(data)}
        if proof == 'hash':
            row['file_hash'] = 'SHA256:' + hashlib.sha256(data).hexdigest()
        else:
            row['file_alg'] = 'SHA384'
            row['file_mac'] = transfer.gkt.get_digest_mac(transfer.gkt.digest_key(shared, params['xfer']),
                            params['xfer'], 'SHA384', hashlib.sha384(data).hexdigest(), len(data))
        return row, shared
    transfers = SimpleNamespace(chunk=16, lane=lambda index: SimpleNamespace(up_name='up', down_name='down'),
                                keyed_call=reply)
    core = SimpleNamespace(transfers=transfers)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = workspace._path(descriptor).rstrip('/')
        assert workspace._same_file(core, 'version-42', 'file', opened, len(data)) is True
        source.write_bytes(b'different-data')
        assert workspace._same_file(core, 'version-42', 'file', opened, len(data)) is False
    finally:
        os.close(descriptor)
