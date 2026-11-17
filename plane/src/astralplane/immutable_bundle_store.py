"""Filesystem transaction layer for publishing immutable generated bundles: stage,
flush, validate, then atomically rename into a revision namespace. Used by
repositories/generated_agent_publications.py; database transitions stay with the
caller.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from astralplane.errors import PlaneError

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None  # type: ignore[assignment]

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover
    _msvcrt = None  # type: ignore[assignment]

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_WIN32_ERROR_ACCESS_DENIED = 5
_WIN32_ERROR_SHARING_VIOLATION = 32
_WIN32_ERROR_FILE_EXISTS = 80
_WIN32_ERROR_ALREADY_EXISTS = 183
_WIN32_TRANSIENT_MOVE_ERRORS = frozenset(
    {_WIN32_ERROR_ACCESS_DENIED, _WIN32_ERROR_SHARING_VIOLATION}
)
_WIN32_COLLISION_MOVE_ERRORS = frozenset(
    {_WIN32_ERROR_FILE_EXISTS, _WIN32_ERROR_ALREADY_EXISTS}
)
_WIN32_MOVE_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32)
_MOVEFILE_WRITE_THROUGH = 0x8
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_POSIX_AT_FDCWD = -100
_POSIX_RENAME_NOREPLACE = 1

_DirectoryIdentity = tuple[int, int]


@dataclass(frozen=True)
class _PinnedDirectoryChain:
    target: Path
    identity: _DirectoryIdentity
    entries: tuple[tuple[Path, _DirectoryIdentity], ...]
    descriptors: tuple[int, ...] = field(default=(), repr=False)
    handles: tuple[int, ...] = field(default=(), repr=False)

    @property
    def descriptor(self) -> int | None:
        return self.descriptors[-1] if self.descriptors else None

    @property
    def parent_descriptor(self) -> int | None:
        if len(self.descriptors) < 2:
            return None
        return self.descriptors[-2]


@dataclass(frozen=True)
class _PathEntryState:
    exists: bool
    identity: _DirectoryIdentity | None = None
    is_directory: bool = False
    is_reparse: bool = False


class ArtifactPublicationError(PlaneError):
    default_code = "immutable_bundle_publication_failed"


class ArtifactCollisionError(ArtifactPublicationError):
    default_code = "immutable_bundle_collision"


class ArtifactIntegrityError(ArtifactPublicationError):
    default_code = "immutable_bundle_integrity_failed"


class ArtifactPublicationRevokedError(ArtifactPublicationError):
    default_code = "immutable_bundle_publication_revoked"


class ArtifactReconciliationError(ArtifactPublicationError):
    default_code = "immutable_bundle_reconciliation_failed"

    def __init__(
        self,
        message: str,
        *,
        conflict_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.conflict_snapshot = (
            None if conflict_snapshot is None else dict(conflict_snapshot)
        )


def _check_publication_not_revoked(
    cancellation_event: threading.Event | None,
) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise ArtifactPublicationRevokedError(
            "artifact publication authority was revoked"
        )


def _move_file_ex_write_through(source: Path, destination: Path) -> int:
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    if move_file(
        str(source),
        str(destination),
        _MOVEFILE_WRITE_THROUGH,
    ):
        return 0
    return int(ctypes.get_last_error())


def _win32_error(error_code: int) -> OSError:
    import ctypes

    return ctypes.WinError(error_code)


def _sleep_before_win32_move_retry(delay_seconds: float) -> None:
    time.sleep(delay_seconds)


def _open_win32_directory(
    path: Path,
    *,
    share_delete: bool,
) -> tuple[int, int, int, int]:
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_share_read = 0x1
    file_share_write = 0x2
    file_share_delete = 0x4 if share_delete else 0
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        0,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle_value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        file_id = (
            int(information.nFileIndexHigh) << 32
        ) | int(information.nFileIndexLow)
        return (
            int(handle),
            int(information.dwFileAttributes),
            int(information.dwVolumeSerialNumber),
            file_id,
        )
    except BaseException:
        close_handle(handle)
        raise


def _close_win32_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _win32_directory_information(path: Path) -> tuple[int, int, int]:
    handle, attributes, volume_serial, file_id = _open_win32_directory(
        path,
        share_delete=True,
    )
    try:
        return attributes, volume_serial, file_id
    finally:
        _close_win32_handle(handle)


def _directory_identity(path: Path) -> _DirectoryIdentity:
    if os.name == "nt":
        try:
            file_attributes, volume_serial, file_id = (
                _win32_directory_information(path)
            )
        except FileNotFoundError as exc:
            raise ArtifactPublicationError(
                "artifact staging directory disappeared before durable replace"
            ) from exc
        if file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ArtifactPublicationError(
                "artifact staging directory is a reparse point"
            )
        if not file_attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise ArtifactPublicationError(
                "artifact staging path is not a directory"
            )
        return volume_serial, file_id

    try:
        status = path.lstat()
    except FileNotFoundError as exc:
        raise ArtifactPublicationError(
            "artifact staging directory disappeared before durable replace"
        ) from exc
    file_attributes = int(getattr(status, "st_file_attributes", 0))
    if stat.S_ISLNK(status.st_mode) or (
        file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ArtifactPublicationError(
            "artifact staging directory is a symbolic link or reparse point"
        )
    if not stat.S_ISDIR(status.st_mode):
        raise ArtifactPublicationError(
            "artifact staging path is not a directory"
        )
    return int(status.st_dev), int(status.st_ino)


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _path_entry_state(
    path: Path,
    *,
    parent_descriptor: int | None = None,
) -> _PathEntryState:
    if os.name == "nt" and parent_descriptor is None:
        try:
            handle, attributes, volume_serial, file_id = (
                _open_win32_directory(path, share_delete=True)
            )
        except FileNotFoundError:
            return _PathEntryState(exists=False)
        try:
            return _PathEntryState(
                exists=True,
                identity=(volume_serial, file_id),
                is_directory=bool(attributes & _FILE_ATTRIBUTE_DIRECTORY),
                is_reparse=bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT),
            )
        finally:
            _close_win32_handle(handle)
    try:
        if parent_descriptor is None:
            status = path.lstat()
        else:
            status = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
    except FileNotFoundError:
        return _PathEntryState(exists=False)
    file_attributes = int(getattr(status, "st_file_attributes", 0))
    is_directory = stat.S_ISDIR(status.st_mode)
    is_reparse = stat.S_ISLNK(status.st_mode) or bool(
        file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    )
    identity = (int(status.st_dev), int(status.st_ino))
    return _PathEntryState(
        exists=True,
        identity=identity,
        is_directory=is_directory,
        is_reparse=is_reparse,
    )


def _path_is_reparse_or_symlink(path: Path) -> bool:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(status.st_mode) or bool(
        int(getattr(status, "st_file_attributes", 0))
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _absolute_without_link_resolution(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    if not absolute.is_absolute() or not absolute.anchor:
        raise ArtifactPublicationError("artifact path is not absolute")
    return absolute


def _identity_from_status(status: os.stat_result) -> _DirectoryIdentity:
    file_attributes = int(getattr(status, "st_file_attributes", 0))
    if stat.S_ISLNK(status.st_mode) or (
        file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ArtifactPublicationError(
            "artifact directory is a symbolic link or reparse point"
        )
    if not stat.S_ISDIR(status.st_mode):
        raise ArtifactPublicationError("artifact path is not a directory")
    return int(status.st_dev), int(status.st_ino)


@contextmanager
def _pin_directory_chain(
    path: Path,
    *,
    create: bool,
    require_leaf_new: bool = False,
    windows_leaf_share_delete: bool = False,
) -> Iterator[_PinnedDirectoryChain]:
    absolute = _absolute_without_link_resolution(path)
    anchor = Path(absolute.anchor)
    relative_parts = absolute.relative_to(anchor).parts
    paths = [anchor]
    current = anchor
    for part in relative_parts:
        current = current / part
        paths.append(current)

    if os.name == "nt":
        handles: list[int] = []
        entries: list[tuple[Path, _DirectoryIdentity]] = []
        try:
            for index, current_path in enumerate(paths):
                is_leaf = index == len(paths) - 1
                created = False
                if index > 0 and not _path_entry_exists(current_path):
                    if not create:
                        raise ArtifactPublicationError(
                            "artifact directory chain disappeared"
                        )
                    try:
                        current_path.mkdir(mode=0o700)
                        created = True
                    except FileExistsError:
                        pass
                if require_leaf_new and is_leaf and not created:
                    raise ArtifactCollisionError(
                        "artifact staging path already exists"
                    )
                handle, attributes, volume_serial, file_id = (
                    _open_win32_directory(
                        current_path,
                        share_delete=(is_leaf and windows_leaf_share_delete),
                    )
                )
                if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    _close_win32_handle(handle)
                    raise ArtifactPublicationError(
                        "artifact directory chain contains a reparse point"
                    )
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
                    _close_win32_handle(handle)
                    raise ArtifactPublicationError(
                        "artifact directory chain contains a non-directory"
                    )
                handles.append(handle)
                entries.append((current_path, (volume_serial, file_id)))
            yield _PinnedDirectoryChain(
                target=absolute,
                identity=entries[-1][1],
                entries=tuple(entries),
                handles=tuple(handles),
            )
        finally:
            for handle in reversed(handles):
                _close_win32_handle(handle)
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    entries = []
    try:
        anchor_descriptor = os.open(anchor, flags)
        descriptors.append(anchor_descriptor)
        entries.append(
            (anchor, _identity_from_status(os.fstat(anchor_descriptor)))
        )
        parent_descriptor = anchor_descriptor
        for index, (part, current_path) in enumerate(
            zip(relative_parts, paths[1:], strict=True),
            start=1,
        ):
            is_leaf = index == len(paths) - 1
            created = False
            try:
                descriptor = os.open(part, flags, dir_fd=parent_descriptor)
            except FileNotFoundError:
                if not create:
                    raise ArtifactPublicationError(
                        "artifact directory chain disappeared"
                    ) from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent_descriptor)
                    created = True
                except FileExistsError:
                    pass
                descriptor = os.open(part, flags, dir_fd=parent_descriptor)
            except OSError as exc:
                raise ArtifactPublicationError(
                    "artifact directory chain is not trustworthy"
                ) from exc
            if require_leaf_new and is_leaf and not created:
                os.close(descriptor)
                raise ArtifactCollisionError(
                    "artifact staging path already exists"
                )
            descriptors.append(descriptor)
            entries.append(
                (current_path, _identity_from_status(os.fstat(descriptor)))
            )
            if created:
                os.fsync(descriptor)
                os.fsync(parent_descriptor)
            parent_descriptor = descriptor
        yield _PinnedDirectoryChain(
            target=absolute,
            identity=entries[-1][1],
            entries=tuple(entries),
            descriptors=tuple(descriptors),
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _revalidate_pinned_chain(
    pinned: _PinnedDirectoryChain,
    *,
    include_leaf_path: bool = True,
) -> None:
    entries = pinned.entries if include_leaf_path else pinned.entries[:-1]
    for index, (path, expected_identity) in enumerate(entries):
        if _directory_identity(path) != expected_identity:
            raise ArtifactIntegrityError(
                "artifact directory chain identity changed"
            )
        if pinned.descriptors and (
            _identity_from_status(os.fstat(pinned.descriptors[index]))
            != expected_identity
        ):
            raise ArtifactIntegrityError("held artifact directory identity changed")


def _move_posix_no_replace(
    source: Path,
    destination: Path,
    *,
    source_parent_descriptor: int | None = None,
    destination_parent_descriptor: int | None = None,
) -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ArtifactPublicationError(
            "atomic no-replace directory rename is unavailable"
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    source_fd = (
        _POSIX_AT_FDCWD
        if source_parent_descriptor is None
        else source_parent_descriptor
    )
    destination_fd = (
        _POSIX_AT_FDCWD
        if destination_parent_descriptor is None
        else destination_parent_descriptor
    )
    source_name = source if source_parent_descriptor is None else Path(source.name)
    destination_name = (
        destination
        if destination_parent_descriptor is None
        else Path(destination.name)
    )
    result = renameat2(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        _POSIX_RENAME_NOREPLACE,
    )
    if result == 0:
        return 0
    return int(ctypes.get_errno())


def _validate_replace_paths(
    source: Path,
    destination: Path,
    *,
    expected_source_identity: _DirectoryIdentity,
    expected_destination_parent_identity: _DirectoryIdentity,
) -> None:
    if _directory_identity(source) != expected_source_identity:
        raise ArtifactIntegrityError(
            "artifact staging directory identity changed before durable replace"
        )
    if (
        _directory_identity(destination.parent)
        != expected_destination_parent_identity
    ):
        raise ArtifactIntegrityError(
            "artifact revision parent identity changed before durable replace"
        )


@dataclass(frozen=True, slots=True)
class ImmutableBundleContract:
    file_names: tuple[str, ...]
    manifest_filename: str = "manifest.json"
    scope_identity_field: str = "scope_id"
    revision_identity_field: str = "revision_id"
    required_text_metadata_fields: tuple[str, ...] = ()
    nonempty_text_metadata_fields: tuple[str, ...] = ()
    max_file_bytes: int = 2 * 1024 * 1024
    max_manifest_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if (
            not isinstance(self.file_names, tuple)
            or not self.file_names
            or len(set(self.file_names)) != len(self.file_names)
        ):
            raise ValueError("bundle file names must be non-empty and unique")
        if not isinstance(self.required_text_metadata_fields, tuple) or not isinstance(
            self.nonempty_text_metadata_fields,
            tuple,
        ):
            raise ValueError("required metadata fields must be immutable tuples")
        if not set(self.nonempty_text_metadata_fields) <= set(
            self.required_text_metadata_fields
        ):
            raise ValueError("non-empty metadata fields must also be required text")
        for filename in (*self.file_names, self.manifest_filename):
            if (
                not isinstance(filename, str)
                or _SAFE_FILE_NAME.fullmatch(filename) is None
                or PurePosixPath(filename).name != filename
                or filename in {".", ".."}
            ):
                raise ValueError("bundle file names must be safe path components")
        if self.manifest_filename in self.file_names:
            raise ValueError("manifest filename must not overlap executable files")
        metadata_fields = (
            self.scope_identity_field,
            self.revision_identity_field,
            "runtime_contract_version",
            "required_runtime_lock_sha256",
            *self.required_text_metadata_fields,
        )
        if len(set(metadata_fields)) != len(metadata_fields):
            raise ValueError("manifest field names must be unique")
        reserved_core_fields = {
            "manifest_version",
            "digest_algorithm",
            "bundle_sha256",
            "files",
        }
        if set(metadata_fields) & reserved_core_fields:
            raise ValueError(
                "manifest identity and metadata fields overlap reserved core fields"
            )
        for field_name in metadata_fields:
            if not isinstance(field_name, str) or _SAFE_FILE_NAME.fullmatch(field_name) is None:
                raise ValueError("manifest identity field names are invalid")
        for value, name in (
            (self.max_file_bytes, "max_file_bytes"),
            (self.max_manifest_bytes, "max_manifest_bytes"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class BundlePublicationKey:
    scope_id: str
    staging_id: str
    source_revision: int
    publication_id: str
    revision_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", _safe_scope_id(self.scope_id))
        object.__setattr__(self, "staging_id", _uuid_text(self.staging_id, "staging_id"))
        object.__setattr__(
            self,
            "publication_id",
            _uuid_text(self.publication_id, "publication_id"),
        )
        object.__setattr__(self, "revision_id", _uuid_text(self.revision_id, "revision_id"))
        if type(self.source_revision) is not int or self.source_revision < 0:
            raise ValueError("source_revision must be non-negative")


@dataclass(frozen=True, slots=True)
class BundlePublicationPaths:
    staging_relative_path: str
    revision_relative_path: str
    quarantine_relative_path: str


def paths_for(key: BundlePublicationKey) -> BundlePublicationPaths:
    if not isinstance(key, BundlePublicationKey):
        raise TypeError("publication key is required")
    return BundlePublicationPaths(
        staging_relative_path=PurePosixPath(
            "staging",
            key.staging_id,
            str(key.source_revision),
            key.publication_id,
        ).as_posix(),
        revision_relative_path=PurePosixPath(
            "revisions",
            key.scope_id,
            key.revision_id,
        ).as_posix(),
        quarantine_relative_path=PurePosixPath(
            "quarantine",
            key.publication_id,
        ).as_posix(),
    )


@dataclass(frozen=True, slots=True)
class BundlePublicationReceipt:
    paths: BundlePublicationPaths
    publication_key: BundlePublicationKey
    storage_identity: _DirectoryIdentity
    bundle_sha256: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class StagedBundleReceipt:
    paths: BundlePublicationPaths
    publication_key: BundlePublicationKey
    storage_identity: _DirectoryIdentity
    bundle_sha256: str
    manifest_sha256: str
    runtime_metadata: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PublishedBundle:
    bundle_relative_path: str
    bundle_sha256: str
    manifest_sha256: str
    files: Mapping[str, str]
    manifest: Mapping[str, Any]
    manifest_json: str
    runtime_metadata: Mapping[str, Any]
    storage_identity: _DirectoryIdentity = field(repr=False, compare=False)
    receipt: BundlePublicationReceipt | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def manifest_dict(self) -> dict[str, Any]:
        return json.loads(self.manifest_json)


class BundleRecoveryDisposition(StrEnum):
    FINAL_VALID = "final_valid"
    STAGING_PROMOTED = "staging_promoted"
    ABSENT = "absent"
    PARTIAL = "partial"
    FOREIGN = "foreign"
    COLLISION = "collision"


@dataclass(frozen=True, slots=True)
class BundleRecoveryResult:
    disposition: BundleRecoveryDisposition
    published: PublishedBundle | None = None
    observed_identity: _DirectoryIdentity | None = field(
        default=None,
        repr=False,
    )
    quarantined: bool = False
    detail: str = ""


def _uuid_text(value: Any, field_name: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
    if parsed.version != 4:
        raise ValueError(f"{field_name} must be a UUID4")
    return str(parsed)


def _safe_scope_id(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_SCOPE_ID.fullmatch(value) is None:
        raise ValueError("scope_id is not a safe path component")
    if value in {".", ".."}:
        raise ValueError("scope_id is not a safe path component")
    return value


def canonical_bundle_digest(
    files: Mapping[str, str],
    contract: ImmutableBundleContract,
) -> str:
    if not isinstance(contract, ImmutableBundleContract):
        raise TypeError("immutable bundle contract is required")
    canonical = json.dumps(
        {name: files[name] for name in contract.file_names},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("runtime manifest object keys must be text")
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _manifest_core_fields(contract: ImmutableBundleContract) -> frozenset[str]:
    return frozenset(
        {
            "manifest_version",
            "digest_algorithm",
            "bundle_sha256",
            "files",
            contract.scope_identity_field,
            contract.revision_identity_field,
        }
    )


def runtime_metadata_for_manifest(
    contract: ImmutableBundleContract,
    manifest: Mapping[str, Any] | str,
) -> Mapping[str, Any]:
    if not isinstance(contract, ImmutableBundleContract):
        raise TypeError("immutable bundle contract is required")
    if isinstance(manifest, str):
        manifest_bytes = manifest.encode("utf-8")
        if len(manifest_bytes) > contract.max_manifest_bytes:
            raise ValueError("runtime manifest exceeds its configured size limit")
        try:
            plain = json.loads(manifest)
        except json.JSONDecodeError as exc:
            raise ValueError("runtime manifest is invalid JSON") from exc
        if not isinstance(plain, dict):
            raise ValueError("runtime manifest must be an object")
        canonical = json.dumps(
            plain,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
        if manifest != canonical:
            raise ValueError("runtime manifest JSON is not canonical")
    elif isinstance(manifest, Mapping):
        try:
            plain = _thaw_json(manifest)
            canonical = json.dumps(
                plain,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime manifest is not bounded JSON") from exc
        if len(canonical.encode("utf-8")) > contract.max_manifest_bytes:
            raise ValueError("runtime manifest exceeds its configured size limit")
    else:
        raise TypeError("runtime manifest must be a mapping or canonical JSON")
    if not isinstance(plain, dict):
        raise ValueError("runtime manifest must be an object")

    metadata_names = (
        "runtime_contract_version",
        "required_runtime_lock_sha256",
        *contract.required_text_metadata_fields,
    )
    metadata_fields = frozenset(metadata_names)
    required_fields = _manifest_core_fields(contract) | metadata_fields
    if set(plain) != required_fields:
        raise ValueError("runtime manifest v2 fields are invalid")
    manifest_version = plain["manifest_version"]
    if type(manifest_version) is not int or manifest_version != 2:
        raise ValueError("runtime manifest version is not v2")
    _safe_scope_id(plain[contract.scope_identity_field])
    _uuid_text(
        plain[contract.revision_identity_field],
        contract.revision_identity_field,
    )
    if plain["digest_algorithm"] != "sha256":
        raise ValueError("runtime manifest digest algorithm is unsupported")
    bundle_digest = plain["bundle_sha256"]
    if not isinstance(bundle_digest, str) or _SHA256.fullmatch(bundle_digest) is None:
        raise ValueError("runtime manifest bundle digest is invalid")
    runtime_version = plain["runtime_contract_version"]
    if type(runtime_version) is not int or runtime_version < 1:
        raise ValueError("runtime contract version is invalid")
    runtime_lock = plain["required_runtime_lock_sha256"]
    if not isinstance(runtime_lock, str) or _SHA256.fullmatch(runtime_lock) is None:
        raise ValueError("required runtime lock digest is invalid")
    for field_name in contract.required_text_metadata_fields:
        value = plain[field_name]
        if not isinstance(value, str):
            raise ValueError("required runtime metadata must be text")
        if field_name in contract.nonempty_text_metadata_fields and not value:
            raise ValueError("required runtime metadata must be non-empty")

    manifest_files = plain["files"]
    if not isinstance(manifest_files, list) or tuple(
        item.get("name") if isinstance(item, dict) else None
        for item in manifest_files
    ) != contract.file_names:
        raise ValueError("runtime manifest file inventory is invalid")
    for item in manifest_files:
        if set(item) != {"name", "sha256", "size_bytes"}:
            raise ValueError("runtime manifest file record shape is invalid")
        if (
            not isinstance(item["sha256"], str)
            or _SHA256.fullmatch(item["sha256"]) is None
            or type(item["size_bytes"]) is not int
            or item["size_bytes"] < 0
            or item["size_bytes"] > contract.max_file_bytes
        ):
            raise ValueError("runtime manifest file metadata is invalid")

    return MappingProxyType(
        {
            name: _freeze_json(plain[name])
            for name in metadata_names
        }
    )


@dataclass(frozen=True, slots=True)
class FinalizedBundle:
    contract: ImmutableBundleContract
    files: Mapping[str, str]
    bundle_sha256: str
    manifest: Mapping[str, Any]
    manifest_json: str
    manifest_sha256: str = field(init=False)
    scope_id: str = field(init=False)
    revision_id: str = field(init=False)
    runtime_metadata: Mapping[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.contract, ImmutableBundleContract):
            raise TypeError("immutable bundle contract is required")
        if not isinstance(self.files, Mapping):
            raise TypeError("files must be a mapping")
        if set(self.files) != set(self.contract.file_names):
            raise ValueError("bundle file inventory does not match its contract")
        ordered_files: dict[str, str] = {}
        for filename in self.contract.file_names:
            content = self.files[filename]
            if not isinstance(content, str):
                raise TypeError(f"{filename} must be UTF-8 text")
            raw = content.encode("utf-8")
            if len(raw) > self.contract.max_file_bytes:
                raise ValueError("bundle file exceeds its configured size limit")
            ordered_files[filename] = content

        if (
            not isinstance(self.bundle_sha256, str)
            or _SHA256.fullmatch(self.bundle_sha256) is None
            or canonical_bundle_digest(ordered_files, self.contract)
            != self.bundle_sha256
        ):
            raise ValueError("bundle_sha256 does not identify the canonical files")
        if not isinstance(self.manifest_json, str):
            raise TypeError("manifest_json must be canonical JSON text")
        manifest_bytes = self.manifest_json.encode("utf-8")
        if len(manifest_bytes) > self.contract.max_manifest_bytes:
            raise ValueError("runtime manifest exceeds its configured size limit")
        try:
            parsed_manifest = json.loads(self.manifest_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime manifest is invalid JSON") from exc
        if not isinstance(parsed_manifest, dict):
            raise ValueError("runtime manifest must be an object")
        canonical_json = json.dumps(
            parsed_manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
        if self.manifest_json != canonical_json:
            raise ValueError("runtime manifest JSON is not canonical")
        if not isinstance(self.manifest, Mapping) or _thaw_json(self.manifest) != parsed_manifest:
            raise ValueError("manifest mapping does not match canonical manifest JSON")

        runtime_metadata = runtime_metadata_for_manifest(
            self.contract,
            parsed_manifest,
        )
        scope_id = _safe_scope_id(
            parsed_manifest[self.contract.scope_identity_field]
        )
        revision_id = _uuid_text(
            parsed_manifest[self.contract.revision_identity_field],
            self.contract.revision_identity_field,
        )
        if parsed_manifest["bundle_sha256"] != self.bundle_sha256:
            raise ValueError("runtime manifest bundle digest does not match files")
        manifest_files = parsed_manifest["files"]
        for item in manifest_files:
            filename = item["name"]
            raw = ordered_files[filename].encode("utf-8")
            if (
                item.get("sha256") != hashlib.sha256(raw).hexdigest()
                or type(item.get("size_bytes")) is not int
                or item.get("size_bytes") != len(raw)
            ):
                raise ValueError("runtime manifest file metadata does not match files")

        object.__setattr__(self, "files", MappingProxyType(ordered_files))
        object.__setattr__(self, "manifest", _freeze_json(parsed_manifest))
        object.__setattr__(
            self,
            "manifest_sha256",
            hashlib.sha256(manifest_bytes).hexdigest(),
        )
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(
            self,
            "runtime_metadata",
            runtime_metadata,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> FinalizedBundle:
        if not isinstance(value, Mapping):
            raise TypeError("bundle value must be a mapping")
        required = {
            "contract",
            "files",
            "bundle_sha256",
            "manifest",
            "manifest_json",
        }
        if set(value) != required:
            raise ValueError("bundle mapping fields are invalid")
        return cls(
            contract=value["contract"],
            files=value["files"],
            bundle_sha256=value["bundle_sha256"],
            manifest=value["manifest"],
            manifest_json=value["manifest_json"],
        )

    def manifest_dict(self) -> dict[str, Any]:
        return json.loads(self.manifest_json)


class ImmutableBundleStore:
    def __init__(
        self,
        root: os.PathLike[str] | str,
        *,
        contract: ImmutableBundleContract,
    ) -> None:
        if not isinstance(contract, ImmutableBundleContract):
            raise TypeError("immutable bundle contract is required")
        self._contract = contract
        self._root = _absolute_without_link_resolution(Path(root))
        self._staging_root = self._root / "staging"
        self._revision_root = self._root / "revisions"
        self._quarantine_root = self._root / "quarantine"
        with _pin_directory_chain(self._root, create=True) as pinned:
            self._root_identity = pinned.identity
        with self._pin_store_directory(self._staging_root, create=True) as pinned:
            self._staging_root_identity = pinned.identity
        with self._pin_store_directory(self._revision_root, create=True) as pinned:
            self._revision_root_identity = pinned.identity
        with self._pin_store_directory(self._quarantine_root, create=True) as pinned:
            self._quarantine_root_identity = pinned.identity

    @property
    def root(self) -> Path:
        return self._root

    @contextmanager
    def _pin_store_directory(
        self,
        path: Path,
        *,
        create: bool = False,
        require_leaf_new: bool = False,
        windows_leaf_share_delete: bool = False,
    ) -> Iterator[_PinnedDirectoryChain]:
        absolute = _absolute_without_link_resolution(path)
        try:
            absolute.relative_to(self._root)
        except ValueError as exc:
            raise ArtifactPublicationError(
                "artifact path escaped the configured root"
            ) from exc
        with _pin_directory_chain(
            absolute,
            create=create,
            require_leaf_new=require_leaf_new,
            windows_leaf_share_delete=windows_leaf_share_delete,
        ) as pinned:
            identities = dict(pinned.entries)
            if identities.get(self._root) != self._root_identity:
                raise ArtifactIntegrityError(
                    "configured artifact root identity changed"
                )
            expected_roots = (
                (self._staging_root, getattr(self, "_staging_root_identity", None)),
                (self._revision_root, getattr(self, "_revision_root_identity", None)),
                (self._quarantine_root, getattr(self, "_quarantine_root_identity", None)),
            )
            for expected_path, expected_identity in expected_roots:
                if (
                    expected_identity is not None
                    and expected_path in identities
                    and identities[expected_path] != expected_identity
                ):
                    raise ArtifactIntegrityError(
                        "artifact namespace root identity changed"
                    )
            yield pinned

    def _ensure_directory(self, path: Path) -> None:
        try:
            with self._pin_store_directory(path, create=True):
                pass
        except ArtifactPublicationError as exc:
            raise ArtifactPublicationError(
                "artifact directory is not trustworthy"
            ) from exc

    @staticmethod
    def _fsync_directory(
        path: Path,
        *,
        descriptor: int | None = None,
    ) -> None:
        if os.name == "nt":
            # No POSIX directory fd on Windows; uses MoveFileExW instead
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        owned_descriptor = descriptor is None
        if descriptor is None:
            descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            if owned_descriptor:
                os.close(descriptor)

    @staticmethod
    def _durable_replace(
        source: Path,
        destination: Path,
        *,
        retry_check: Callable[[], None] | None = None,
        expected_source_identity: _DirectoryIdentity | None = None,
        expected_destination_parent_identity: _DirectoryIdentity | None = None,
        cancellation_event: threading.Event | None = None,
        mismatch_handler: Callable[[_DirectoryIdentity], bool] | None = None,
        source_parent_descriptor: int | None = None,
        destination_parent_descriptor: int | None = None,
        equivalent_destination_check: Callable[[], bool] | None = None,
    ) -> str:
        source_identity = (
            _directory_identity(source)
            if expected_source_identity is None
            else expected_source_identity
        )
        destination_parent_identity = (
            _directory_identity(destination.parent)
            if expected_destination_parent_identity is None
            else expected_destination_parent_identity
        )
        if os.name != "nt":
            while True:
                _check_publication_not_revoked(cancellation_event)
                if retry_check is not None:
                    retry_check()
                _validate_replace_paths(
                    source,
                    destination,
                    expected_source_identity=source_identity,
                    expected_destination_parent_identity=(
                        destination_parent_identity
                    ),
                )
                _check_publication_not_revoked(cancellation_event)
                error_code = _move_posix_no_replace(
                    source,
                    destination,
                    source_parent_descriptor=source_parent_descriptor,
                    destination_parent_descriptor=(
                        destination_parent_descriptor
                    ),
                )
                outcome = ImmutableBundleStore._reconcile_native_move(
                    source,
                    destination,
                    native_error_code=error_code,
                    expected_source_identity=source_identity,
                    source_parent_descriptor=source_parent_descriptor,
                    destination_parent_descriptor=(
                        destination_parent_descriptor
                    ),
                    collision_errors={errno.EEXIST, errno.ENOTEMPTY},
                    equivalent_destination_check=(
                        equivalent_destination_check
                    ),
                    mismatch_handler=mismatch_handler,
                )
                if outcome is not None:
                    return outcome
                if error_code == errno.EINTR:
                    continue
                if error_code in {
                    errno.ENOSYS,
                    errno.EINVAL,
                    getattr(errno, "EOPNOTSUPP", errno.ENOSYS),
                }:
                    raise ArtifactPublicationError(
                        "atomic no-replace directory rename is unavailable"
                    )
                if error_code == errno.EXDEV:
                    raise ArtifactPublicationError(
                        "artifact staging and revision roots are on different filesystems"
                    )
                raise OSError(
                    error_code,
                    os.strerror(error_code),
                    os.fspath(source),
                    os.fspath(destination),
                )
        return ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
            retry_check=retry_check,
            expected_source_identity=source_identity,
            expected_destination_parent_identity=destination_parent_identity,
            cancellation_event=cancellation_event,
            mismatch_handler=mismatch_handler,
            equivalent_destination_check=equivalent_destination_check,
        )

    @staticmethod
    def _durable_replace_windows(
        source: Path,
        destination: Path,
        *,
        retry_check: Callable[[], None] | None = None,
        expected_source_identity: _DirectoryIdentity | None = None,
        expected_destination_parent_identity: _DirectoryIdentity | None = None,
        cancellation_event: threading.Event | None = None,
        mismatch_handler: Callable[[_DirectoryIdentity], bool] | None = None,
        equivalent_destination_check: Callable[[], bool] | None = None,
    ) -> str:
        source_identity = (
            _directory_identity(source)
            if expected_source_identity is None
            else expected_source_identity
        )
        destination_parent_identity = (
            _directory_identity(destination.parent)
            if expected_destination_parent_identity is None
            else expected_destination_parent_identity
        )
        for attempt in range(len(_WIN32_MOVE_RETRY_DELAYS_SECONDS) + 1):
            _check_publication_not_revoked(cancellation_event)
            if retry_check is not None:
                retry_check()
            _validate_replace_paths(
                source,
                destination,
                expected_source_identity=source_identity,
                expected_destination_parent_identity=(
                    destination_parent_identity
                ),
            )
            _check_publication_not_revoked(cancellation_event)

            error_code = _move_file_ex_write_through(source, destination)
            outcome = ImmutableBundleStore._reconcile_native_move(
                source,
                destination,
                native_error_code=error_code,
                expected_source_identity=source_identity,
                collision_errors=_WIN32_COLLISION_MOVE_ERRORS,
                equivalent_destination_check=equivalent_destination_check,
                mismatch_handler=mismatch_handler,
            )
            if outcome is not None:
                return outcome
            if (
                error_code not in _WIN32_TRANSIENT_MOVE_ERRORS
                or attempt == len(_WIN32_MOVE_RETRY_DELAYS_SECONDS)
            ):
                raise _win32_error(error_code)
            delay = _WIN32_MOVE_RETRY_DELAYS_SECONDS[attempt]
            if cancellation_event is None:
                _sleep_before_win32_move_retry(delay)
            elif cancellation_event.wait(delay):
                _check_publication_not_revoked(cancellation_event)
        raise ArtifactPublicationError("Windows durable replace retry bound was invalid")

    @staticmethod
    def _reconcile_native_move(
        source: Path,
        destination: Path,
        *,
        native_error_code: int,
        expected_source_identity: _DirectoryIdentity,
        collision_errors: frozenset[int] | set[int],
        source_parent_descriptor: int | None = None,
        destination_parent_descriptor: int | None = None,
        equivalent_destination_check: Callable[[], bool] | None,
        mismatch_handler: Callable[[_DirectoryIdentity], bool] | None,
    ) -> str | None:
        source_state = _path_entry_state(
            source,
            parent_descriptor=source_parent_descriptor,
        )
        destination_state = _path_entry_state(
            destination,
            parent_descriptor=destination_parent_descriptor,
        )
        source_is_expected = (
            source_state.identity == expected_source_identity
            and source_state.is_directory
            and not source_state.is_reparse
        )
        if (
            not source_state.exists
            and destination_state.identity == expected_source_identity
            and destination_state.is_directory
            and not destination_state.is_reparse
        ):
            return "committed"
        if (
            native_error_code in collision_errors
            and source_is_expected
            and destination_state.exists
            and equivalent_destination_check is not None
            and equivalent_destination_check()
        ):
            return "idempotent"
        if source_is_expected:
            if not destination_state.exists:
                if native_error_code == 0:
                    raise ArtifactIntegrityError(
                        "native move reported success without committing source"
                    )
                return None
            raise ArtifactCollisionError(
                "immutable revision path appeared during durable replace"
            )

        integrity_error = ArtifactIntegrityError(
            "native move produced an untrusted publication state"
        )
        if not destination_state.exists or destination_state.identity is None:
            raise integrity_error
        if mismatch_handler is None:
            raise integrity_error from ArtifactPublicationError(
                "untrusted destination has no exact quarantine handler"
            )
        try:
            recovered = mismatch_handler(destination_state.identity)
        except BaseException as cleanup_error:
            raise integrity_error from cleanup_error
        if not recovered:
            raise integrity_error from ArtifactPublicationError(
                "mismatched publication could not be quarantined exactly"
            )
        raise integrity_error

    @staticmethod
    def _fault(
        fault_hook: Callable[[str], None] | None, boundary: str
    ) -> None:
        if fault_hook is not None:
            fault_hook(boundary)

    @contextmanager
    def _publication_lock(self) -> Iterator[None]:
        with self._pin_store_directory(self._root) as root_pin:
            if _fcntl is not None:
                descriptor = root_pin.descriptor
                if descriptor is None:  # pragma: no cover
                    raise ArtifactPublicationError(
                        "POSIX artifact root descriptor is unavailable"
                    )
                _fcntl.flock(descriptor, _fcntl.LOCK_EX)
                try:
                    _revalidate_pinned_chain(root_pin)
                    yield
                finally:
                    _fcntl.flock(descriptor, _fcntl.LOCK_UN)
                return

            if _msvcrt is None:  # pragma: no cover
                raise ArtifactPublicationError("platform file locking is unavailable")
            lock_path = self._root / ".publication.lock"
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                opened = os.fstat(descriptor)
                opened_identity = (int(opened.st_dev), int(opened.st_ino))
                path_status = lock_path.lstat()
                if (
                    not stat.S_ISREG(path_status.st_mode)
                    or _path_is_reparse_or_symlink(lock_path)
                    or (int(path_status.st_dev), int(path_status.st_ino))
                    != opened_identity
                ):
                    raise ArtifactPublicationError(
                        "artifact publication lock identity is untrusted"
                    )
                os.lseek(descriptor, 0, os.SEEK_SET)
                _msvcrt.locking(descriptor, _msvcrt.LK_LOCK, 1)
                try:
                    current = lock_path.lstat()
                    if (int(current.st_dev), int(current.st_ino)) != opened_identity:
                        raise ArtifactIntegrityError(
                            "artifact publication lock identity changed"
                        )
                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"\0")
                        os.fsync(descriptor)
                        os.lseek(descriptor, 0, os.SEEK_SET)
                    _revalidate_pinned_chain(root_pin)
                    yield
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
            finally:
                os.close(descriptor)

    @staticmethod
    def _write_durable_file(
        path: Path,
        content: bytes,
        *,
        directory_descriptor: int | None = None,
    ) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        open_path = path if directory_descriptor is None else path.name
        descriptor = os.open(
            open_path,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            view = memoryview(content)
            while view:
                try:
                    written = os.write(descriptor, view)
                except InterruptedError:
                    continue
                if written <= 0:  # pragma: no cover
                    raise OSError("short artifact write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _remove_stale_staging(
        self,
        path: Path,
        *,
        fsync_parent: bool = True,
    ) -> None:
        if not _path_entry_exists(path):
            return
        try:
            with self._pin_store_directory(
                path,
                windows_leaf_share_delete=True,
            ) as pinned:
                if pinned.descriptor is None:
                    shutil.rmtree(path)
                else:
                    parent_descriptor = pinned.parent_descriptor
                    if parent_descriptor is None:  # pragma: no cover
                        raise ArtifactPublicationError(
                            "staging parent descriptor is unavailable"
                        )
                    shutil.rmtree(path.name, dir_fd=parent_descriptor)
                    if fsync_parent:
                        os.fsync(parent_descriptor)
        except ArtifactPublicationError as exc:
            raise ArtifactPublicationError(
                "staging path is not a trustworthy directory"
            ) from exc

    def _relocate_exact_entry(
        self,
        source_path: Path,
        destination_path: Path,
        *,
        expected_identity: _DirectoryIdentity,
        retry_check: Callable[[], None] | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> None:
        with self._pin_store_directory(source_path.parent) as source_parent_pin:
            source_state = _path_entry_state(
                source_path,
                parent_descriptor=source_parent_pin.descriptor,
            )
            if source_state.identity != expected_identity:
                raise ArtifactIntegrityError(
                    "exact quarantine source identity changed"
                )
            with self._pin_store_directory(
                destination_path.parent,
                create=True,
            ) as destination_parent_pin:
                if _path_entry_state(
                    destination_path,
                    parent_descriptor=destination_parent_pin.descriptor,
                ).exists:
                    raise ArtifactCollisionError(
                        "exact quarantine destination already exists"
                    )

                def revalidate() -> None:
                    _revalidate_pinned_chain(source_parent_pin)
                    _revalidate_pinned_chain(destination_parent_pin)
                    current = _path_entry_state(
                        source_path,
                        parent_descriptor=source_parent_pin.descriptor,
                    )
                    if current != source_state:
                        raise ArtifactIntegrityError(
                            "exact quarantine source identity changed"
                        )

                attempt = 0
                while True:
                    _check_publication_not_revoked(cancellation_event)
                    if retry_check is not None:
                        retry_check()
                    revalidate()
                    _check_publication_not_revoked(cancellation_event)
                    if os.name == "nt":
                        error_code = _move_file_ex_write_through(
                            source_path,
                            destination_path,
                        )
                    else:
                        error_code = _move_posix_no_replace(
                            source_path,
                            destination_path,
                            source_parent_descriptor=(
                                source_parent_pin.descriptor
                            ),
                            destination_parent_descriptor=(
                                destination_parent_pin.descriptor
                            ),
                        )
                    current_source = _path_entry_state(
                        source_path,
                        parent_descriptor=source_parent_pin.descriptor,
                    )
                    current_destination = _path_entry_state(
                        destination_path,
                        parent_descriptor=destination_parent_pin.descriptor,
                    )
                    if (
                        not current_source.exists
                        and current_destination.identity == expected_identity
                        and current_destination.is_directory
                        == source_state.is_directory
                        and current_destination.is_reparse
                        == source_state.is_reparse
                    ):
                        break
                    if current_source == source_state:
                        if current_destination.exists:
                            raise ArtifactCollisionError(
                                "exact quarantine destination appeared"
                            )
                        if error_code == 0:
                            raise ArtifactIntegrityError(
                                "native quarantine move reported success "
                                "without committing source"
                            )
                        if os.name == "nt":
                            if (
                                error_code in _WIN32_TRANSIENT_MOVE_ERRORS
                                and attempt
                                < len(_WIN32_MOVE_RETRY_DELAYS_SECONDS)
                            ):
                                delay = _WIN32_MOVE_RETRY_DELAYS_SECONDS[
                                    attempt
                                ]
                                attempt += 1
                                if cancellation_event is None:
                                    _sleep_before_win32_move_retry(delay)
                                elif cancellation_event.wait(delay):
                                    _check_publication_not_revoked(
                                        cancellation_event
                                    )
                                continue
                            raise _win32_error(error_code)
                        if error_code == errno.EINTR:
                            continue
                        if error_code in {
                            errno.ENOSYS,
                            errno.EINVAL,
                            getattr(errno, "EOPNOTSUPP", errno.ENOSYS),
                        }:
                            raise ArtifactPublicationError(
                                "atomic no-replace directory rename is unavailable"
                            )
                        if error_code == errno.EXDEV:
                            raise ArtifactPublicationError(
                                "artifact roots are on different filesystems"
                            )
                        raise OSError(
                            error_code,
                            os.strerror(error_code),
                            os.fspath(source_path),
                            os.fspath(destination_path),
                        )
                    raise ArtifactIntegrityError(
                        "exact quarantine native move produced an untrusted state"
                    )

                if source_state.is_directory and not source_state.is_reparse:
                    with self._pin_store_directory(
                        destination_path,
                    ) as destination_pin:
                        if destination_pin.identity != expected_identity:
                            raise ArtifactIntegrityError(
                                "exact quarantine destination identity changed"
                            )
                        self._fsync_directory(
                            destination_path,
                            descriptor=destination_pin.descriptor,
                        )
                # Destination fsync must land before the source-parent fsync
                self._fsync_directory(
                    destination_path.parent,
                    descriptor=destination_parent_pin.descriptor,
                )
                self._fsync_directory(
                    source_path.parent,
                    descriptor=source_parent_pin.descriptor,
                )
                final_state = _path_entry_state(
                    destination_path,
                    parent_descriptor=destination_parent_pin.descriptor,
                )
                if final_state != source_state:
                    raise ArtifactIntegrityError(
                        "exact quarantine destination identity changed"
                    )

    def _relocate_exact_directory(
        self,
        source_path: Path,
        destination_path: Path,
        *,
        expected_identity: _DirectoryIdentity,
        allow_mismatch_restore: bool = True,
    ) -> None:
        with self._pin_store_directory(
            source_path,
            windows_leaf_share_delete=True,
        ) as source_pin:
            if source_pin.identity != expected_identity:
                raise ArtifactIntegrityError(
                    "exact relocation source identity changed"
                )
            with self._pin_store_directory(
                destination_path.parent,
                create=True,
            ) as destination_parent_pin:
                if _path_entry_exists(destination_path):
                    raise ArtifactCollisionError(
                        "exact relocation destination already exists"
                    )

                def retry_check() -> None:
                    _revalidate_pinned_chain(source_pin)
                    _revalidate_pinned_chain(destination_parent_pin)

                mismatch_handler = None
                if allow_mismatch_restore:

                    def restore_mismatch(
                        observed_identity: _DirectoryIdentity,
                    ) -> bool:
                        try:
                            self._relocate_exact_directory(
                                destination_path,
                                source_path,
                                expected_identity=observed_identity,
                                allow_mismatch_restore=False,
                            )
                        except ArtifactPublicationError:
                            return False
                        return True

                    mismatch_handler = restore_mismatch

                outcome = self._durable_replace(
                    source_path,
                    destination_path,
                    retry_check=retry_check,
                    expected_source_identity=expected_identity,
                    expected_destination_parent_identity=(
                        destination_parent_pin.identity
                    ),
                    mismatch_handler=mismatch_handler,
                    source_parent_descriptor=source_pin.parent_descriptor,
                    destination_parent_descriptor=(
                        destination_parent_pin.descriptor
                    ),
                )
                if outcome != "committed":
                    raise ArtifactIntegrityError(
                        "exact relocation did not commit its source"
                    )
                self._fsync_directory(
                    destination_path,
                    descriptor=source_pin.descriptor,
                )
                self._fsync_directory(
                    destination_path.parent,
                    descriptor=destination_parent_pin.descriptor,
                )
                self._fsync_directory(
                    source_path.parent,
                    descriptor=source_pin.parent_descriptor,
                )
                destination_state = _path_entry_state(
                    destination_path,
                    parent_descriptor=destination_parent_pin.descriptor,
                )
                if destination_state.identity != expected_identity:
                    raise ArtifactIntegrityError(
                        "exact relocation destination identity changed"
                    )

    def _quarantine_failed_revision(
        self,
        revision_path: Path,
        quarantine_path: Path,
        *,
        expected_identity: _DirectoryIdentity,
    ) -> bool:
        try:
            self._relocate_exact_directory(
                revision_path,
                quarantine_path,
                expected_identity=expected_identity,
            )
        except (ArtifactPublicationError, OSError):
            return False
        return True

    def _redurable_existing_revision(
        self,
        revision_path: Path,
        *,
        expected_identity: _DirectoryIdentity,
    ) -> None:
        with self._pin_store_directory(revision_path) as revision_pin:
            if revision_pin.identity != expected_identity:
                raise ArtifactIntegrityError(
                    "existing revision identity changed during recovery"
                )
            self._fsync_directory(
                revision_path,
                descriptor=revision_pin.descriptor,
            )
            self._fsync_directory(
                revision_path.parent,
                descriptor=revision_pin.parent_descriptor,
            )

    def _fsync_existing_directory(self, path: Path) -> None:
        if _path_entry_exists(path):
            with self._pin_store_directory(
                path,
            ) as pinned:
                self._fsync_directory(
                    path,
                    descriptor=pinned.descriptor,
                )

    @staticmethod
    def _with_receipt(
        artifact: PublishedBundle,
        *,
        key: BundlePublicationKey,
        publication_paths: BundlePublicationPaths,
    ) -> PublishedBundle:
        return replace(
            artifact,
            receipt=BundlePublicationReceipt(
                paths=publication_paths,
                publication_key=key,
                storage_identity=artifact.storage_identity,
                bundle_sha256=artifact.bundle_sha256,
                manifest_sha256=artifact.manifest_sha256,
            ),
        )

    def _validated_recovery_metadata(
        self,
        value: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError("expected_runtime_metadata must be a mapping")
        try:
            plain = _thaw_json(value)
            encoded = json.dumps(
                plain,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("expected runtime metadata is not bounded JSON") from exc
        if not isinstance(plain, dict):
            raise TypeError("expected_runtime_metadata must be an object")
        expected_fields = {
            "runtime_contract_version",
            "required_runtime_lock_sha256",
            *self._contract.required_text_metadata_fields,
        }
        if set(plain) != expected_fields:
            raise ValueError("expected runtime metadata fields are invalid")
        runtime_version = plain["runtime_contract_version"]
        if type(runtime_version) is not int or runtime_version < 1:
            raise ValueError("expected runtime contract version is invalid")
        runtime_lock = plain["required_runtime_lock_sha256"]
        if not isinstance(runtime_lock, str) or _SHA256.fullmatch(runtime_lock) is None:
            raise ValueError("expected runtime lock digest is invalid")
        for field_name in self._contract.required_text_metadata_fields:
            field_value = plain[field_name]
            if not isinstance(field_value, str):
                raise ValueError("expected runtime metadata must be text")
            if (
                field_name in self._contract.nonempty_text_metadata_fields
                and not field_value
            ):
                raise ValueError("expected runtime metadata must be non-empty")
        if len(encoded) > self._contract.max_manifest_bytes:
            raise ValueError("expected runtime metadata exceeds its size limit")
        return MappingProxyType(
            {name: _freeze_json(item) for name, item in plain.items()}
        )

    @staticmethod
    def _metadata_matches(
        artifact: PublishedBundle,
        expected_runtime_metadata: Mapping[str, Any],
    ) -> bool:
        return _thaw_json(artifact.runtime_metadata) == _thaw_json(
            expected_runtime_metadata
        )

    def _independently_valid_bundle(
        self,
        path: Path,
        *,
        relative_path: str,
        expected_identity: _DirectoryIdentity,
    ) -> PublishedBundle | None:
        try:
            with self._pin_store_directory(
                path,
                windows_leaf_share_delete=True,
            ) as candidate_pin:
                if candidate_pin.identity != expected_identity:
                    raise ArtifactIntegrityError(
                        "candidate bundle identity changed during inspection"
                    )
                return self._load_path(
                    path,
                    relative_path=relative_path,
                    expected_digest=None,
                    expected_manifest_digest=None,
                    pinned=candidate_pin,
                )
        except ArtifactIntegrityError:
            return None

    def _stage_locked(
        self,
        finalized: FinalizedBundle,
        *,
        key: BundlePublicationKey,
        publication_paths: BundlePublicationPaths,
        replace_existing: bool,
        before_stage_checked: bool = False,
        fence_check: Callable[[str], None] | None,
        cancellation_event: threading.Event | None,
        fault_hook: Callable[[str], None] | None,
    ) -> StagedBundleReceipt:
        relative_path = publication_paths.revision_relative_path
        staging_path = self._root.joinpath(
            *PurePosixPath(publication_paths.staging_relative_path).parts
        )
        manifest_bytes = finalized.manifest_json.encode("utf-8")

        if not before_stage_checked:
            if fence_check is not None:
                fence_check("before_stage")
            _check_publication_not_revoked(cancellation_event)
            self._fault(fault_hook, "before_stage")

        if _path_entry_exists(staging_path):
            state = _path_entry_state(staging_path)
            if (
                state.identity is None
                or not state.is_directory
                or state.is_reparse
            ):
                raise ArtifactCollisionError(
                    "artifact staging path is occupied by a foreign entry"
                )
            try:
                with self._pin_store_directory(
                    staging_path,
                    windows_leaf_share_delete=True,
                ) as existing_pin:
                    if existing_pin.identity != state.identity:
                        raise ArtifactIntegrityError(
                            "artifact staging identity changed during replay"
                        )
                    existing = self._load_path(
                        staging_path,
                        relative_path=relative_path,
                        expected_digest=finalized.bundle_sha256,
                        expected_manifest_digest=finalized.manifest_sha256,
                        pinned=existing_pin,
                    )
                    if not self._metadata_matches(
                        existing,
                        finalized.runtime_metadata,
                    ):
                        raise ArtifactCollisionError(
                            "artifact staging runtime metadata differs"
                        )
                    self._fsync_directory(
                        staging_path,
                        descriptor=existing_pin.descriptor,
                    )
                    self._fsync_directory(
                        staging_path.parent,
                        descriptor=existing_pin.parent_descriptor,
                    )
                    identity = existing_pin.identity
            except ArtifactIntegrityError as expected_error:
                if self._independently_valid_bundle(
                    staging_path,
                    relative_path=relative_path,
                    expected_identity=state.identity,
                ) is not None:
                    raise ArtifactCollisionError(
                        "artifact staging contains another valid bundle"
                    ) from expected_error
                if not replace_existing:
                    raise
            else:
                return StagedBundleReceipt(
                    paths=publication_paths,
                    publication_key=key,
                    storage_identity=identity,
                    bundle_sha256=existing.bundle_sha256,
                    manifest_sha256=existing.manifest_sha256,
                    runtime_metadata=existing.runtime_metadata,
                )

        if replace_existing:
            self._remove_stale_staging(staging_path)
        with self._pin_store_directory(
            staging_path,
            create=True,
            require_leaf_new=True,
        ) as staging_write_pin:
            self._fault(fault_hook, "after_staging_directory")

            for filename in self._contract.file_names:
                _check_publication_not_revoked(cancellation_event)
                content = finalized.files[filename].encode("utf-8")
                if len(content) > self._contract.max_file_bytes:
                    raise ArtifactPublicationError(
                        "bundle file exceeds size limit"
                    )
                self._write_durable_file(
                    staging_path / filename,
                    content,
                    directory_descriptor=staging_write_pin.descriptor,
                )
                self._fault(fault_hook, f"after_file:{filename}")
            _check_publication_not_revoked(cancellation_event)
            if len(manifest_bytes) > self._contract.max_manifest_bytes:
                raise ArtifactPublicationError(
                    "runtime manifest exceeds size limit"
                )
            self._write_durable_file(
                staging_path / self._contract.manifest_filename,
                manifest_bytes,
                directory_descriptor=staging_write_pin.descriptor,
            )
            self._fault(fault_hook, "after_file:manifest.json")
            self._fsync_directory(
                staging_path,
                descriptor=staging_write_pin.descriptor,
            )
            self._fsync_directory(
                staging_path.parent,
                descriptor=staging_write_pin.parent_descriptor,
            )
            self._fault(fault_hook, "after_staging_fsync")

            staged = self._load_path(
                staging_path,
                relative_path=relative_path,
                expected_digest=finalized.bundle_sha256,
                expected_manifest_digest=finalized.manifest_sha256,
                pinned=staging_write_pin,
            )
            _revalidate_pinned_chain(staging_write_pin)
            identity = staging_write_pin.identity
        return StagedBundleReceipt(
            paths=publication_paths,
            publication_key=key,
            storage_identity=identity,
            bundle_sha256=staged.bundle_sha256,
            manifest_sha256=staged.manifest_sha256,
            runtime_metadata=staged.runtime_metadata,
        )

    def _promote_validated_staging(
        self,
        *,
        staging_path: Path,
        revision_path: Path,
        quarantine_path: Path,
        relative_path: str,
        staging_identity: _DirectoryIdentity,
        expected_digest: str,
        expected_manifest_digest: str,
        expected_runtime_metadata: Mapping[str, Any],
        fence_check: Callable[[str], None] | None,
        cancellation_event: threading.Event | None,
        fault_hook: Callable[[str], None] | None,
    ) -> PublishedBundle:
        self._fault(fault_hook, "after_validate")
        if fence_check is not None:
            fence_check("before_replace")
        self._fault(fault_hook, "before_replace")

        with self._pin_store_directory(
            staging_path,
            windows_leaf_share_delete=True,
        ) as source_pin, self._pin_store_directory(
            revision_path.parent,
            create=True,
        ) as destination_parent_pin:
            if source_pin.identity != staging_identity:
                raise ArtifactIntegrityError(
                    "validated staging identity changed before promotion"
                )
            if _path_entry_exists(revision_path):
                existing = self.load(
                    relative_path,
                    expected_digest=expected_digest,
                    expected_manifest_digest=expected_manifest_digest,
                )
                if not self._metadata_matches(existing, expected_runtime_metadata):
                    raise ArtifactCollisionError(
                        "existing revision runtime metadata differs"
                    )
                self._redurable_existing_revision(
                    revision_path,
                    expected_identity=existing.storage_identity,
                )
                self._remove_stale_staging(
                    staging_path,
                    fsync_parent=False,
                )
                self._fsync_existing_directory(staging_path.parent)
                return existing

            def retry_check() -> None:
                _revalidate_pinned_chain(source_pin)
                _revalidate_pinned_chain(destination_parent_pin)
                current = self._load_path(
                    staging_path,
                    relative_path=relative_path,
                    expected_digest=expected_digest,
                    expected_manifest_digest=expected_manifest_digest,
                    pinned=source_pin,
                )
                if not self._metadata_matches(
                    current,
                    expected_runtime_metadata,
                ):
                    raise ArtifactIntegrityError(
                        "validated staging runtime metadata changed"
                    )
                if fence_check is not None:
                    fence_check("before_replace")

            def equivalent_destination() -> bool:
                try:
                    existing_destination = self.load(
                        relative_path,
                        expected_digest=expected_digest,
                        expected_manifest_digest=expected_manifest_digest,
                    )
                except ArtifactPublicationError:
                    return False
                return self._metadata_matches(
                    existing_destination,
                    expected_runtime_metadata,
                )

            def quarantine_mismatch(
                observed_identity: _DirectoryIdentity,
            ) -> bool:
                self._relocate_exact_entry(
                    revision_path,
                    quarantine_path,
                    expected_identity=observed_identity,
                )
                return True

            move_outcome = self._durable_replace(
                staging_path,
                revision_path,
                retry_check=retry_check,
                expected_source_identity=staging_identity,
                expected_destination_parent_identity=(
                    destination_parent_pin.identity
                ),
                cancellation_event=cancellation_event,
                mismatch_handler=quarantine_mismatch,
                source_parent_descriptor=source_pin.parent_descriptor,
                destination_parent_descriptor=(
                    destination_parent_pin.descriptor
                ),
                equivalent_destination_check=equivalent_destination,
            )
            if move_outcome == "idempotent":
                existing = self.load(
                    relative_path,
                    expected_digest=expected_digest,
                    expected_manifest_digest=expected_manifest_digest,
                )
                if not self._metadata_matches(existing, expected_runtime_metadata):
                    raise ArtifactCollisionError(
                        "equivalent revision runtime metadata differs"
                    )
                self._redurable_existing_revision(
                    revision_path,
                    expected_identity=existing.storage_identity,
                )
            else:
                self._fault(fault_hook, "after_replace")
                try:
                    self._fsync_directory(
                        revision_path,
                        descriptor=source_pin.descriptor,
                    )
                    self._fsync_directory(
                        revision_path.parent,
                        descriptor=destination_parent_pin.descriptor,
                    )
                    self._fsync_directory(
                        staging_path.parent,
                        descriptor=source_pin.parent_descriptor,
                    )
                except Exception as durability_error:
                    try:
                        self._relocate_exact_directory(
                            revision_path,
                            quarantine_path,
                            expected_identity=staging_identity,
                        )
                    except BaseException as cleanup_error:
                        raise durability_error from cleanup_error
                    raise
                self._fault(fault_hook, "after_revision_fsync")
                existing = None

        if move_outcome == "idempotent":
            self._remove_stale_staging(
                staging_path,
                fsync_parent=False,
            )
            self._fsync_existing_directory(staging_path.parent)
            if existing is None:  # pragma: no cover
                raise ArtifactIntegrityError(
                    "idempotent publication did not identify a revision"
                )
            return existing

        # Re-verifies here so post-move corruption isn't hidden
        try:
            published = self.load(
                relative_path,
                expected_digest=expected_digest,
                expected_manifest_digest=expected_manifest_digest,
            )
            if published.storage_identity != staging_identity:
                raise ArtifactIntegrityError(
                    "published artifact identity differs from staged object"
                )
            if not self._metadata_matches(published, expected_runtime_metadata):
                raise ArtifactIntegrityError(
                    "published runtime metadata differs from validated staging"
                )
        except Exception as verification_error:
            try:
                self._relocate_exact_directory(
                    revision_path,
                    quarantine_path,
                    expected_identity=staging_identity,
                )
            except BaseException as cleanup_error:
                raise verification_error from cleanup_error
            raise
        return published

    def publish(
        self,
        finalized: FinalizedBundle,
        *,
        key: BundlePublicationKey,
        fence_check: Callable[[str], None] | None = None,
        cancellation_event: threading.Event | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> PublishedBundle:
        with self._publication_lock():
            return self._publish_locked(
                finalized,
                key=key,
                fence_check=fence_check,
                cancellation_event=cancellation_event,
                fault_hook=fault_hook,
            )

    def stage(
        self,
        finalized: FinalizedBundle,
        *,
        key: BundlePublicationKey,
        fence_check: Callable[[str], None] | None = None,
        cancellation_event: threading.Event | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> StagedBundleReceipt:
        if not isinstance(finalized, FinalizedBundle):
            raise TypeError("finalized must be FinalizedBundle")
        if not isinstance(key, BundlePublicationKey):
            raise TypeError("publication key is required")
        if finalized.contract != self._contract:
            raise ArtifactIntegrityError(
                "finalized bundle contract does not match store"
            )
        if finalized.scope_id != key.scope_id:
            raise ArtifactIntegrityError(
                "finalized manifest scope does not match path"
            )
        if finalized.revision_id != key.revision_id:
            raise ArtifactIntegrityError(
                "finalized manifest revision does not match path"
            )
        publication_paths = paths_for(key)
        revision_path = self._root.joinpath(
            *PurePosixPath(publication_paths.revision_relative_path).parts
        )
        with self._publication_lock():
            if _path_entry_exists(revision_path):
                raise ArtifactCollisionError(
                    "immutable revision already exists; recover it instead"
                )
            return self._stage_locked(
                finalized,
                key=key,
                publication_paths=publication_paths,
                replace_existing=False,
                fence_check=fence_check,
                cancellation_event=cancellation_event,
                fault_hook=fault_hook,
            )

    def promote_staged(
        self,
        receipt: StagedBundleReceipt,
        *,
        fence_check: Callable[[str], None] | None = None,
        cancellation_event: threading.Event | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> PublishedBundle:
        expected_paths, expected_metadata = self._validated_staged_receipt(
            receipt
        )
        relative_path = expected_paths.revision_relative_path
        staging_path = self._root.joinpath(
            *PurePosixPath(expected_paths.staging_relative_path).parts
        )
        revision_path = self._root.joinpath(
            *PurePosixPath(relative_path).parts
        )
        quarantine_path = self._root.joinpath(
            *PurePosixPath(expected_paths.quarantine_relative_path).parts
        )

        with self._publication_lock():
            _check_publication_not_revoked(cancellation_event)
            staging_state = _path_entry_state(staging_path)
            if not staging_state.exists:
                revision_state = _path_entry_state(revision_path)
                if not revision_state.exists:
                    raise ArtifactIntegrityError(
                        "staged bundle disappeared before promotion"
                    )
                if not revision_state.is_directory or revision_state.is_reparse:
                    raise ArtifactCollisionError(
                        "immutable revision path is occupied by a foreign entry"
                    )
                existing = self.load(
                    relative_path,
                    expected_digest=receipt.bundle_sha256,
                    expected_manifest_digest=receipt.manifest_sha256,
                )
                if not self._metadata_matches(existing, expected_metadata):
                    raise ArtifactCollisionError(
                        "immutable revision runtime metadata differs"
                    )
                self._redurable_existing_revision(
                    revision_path,
                    expected_identity=existing.storage_identity,
                )
                return self._with_receipt(
                    existing,
                    key=receipt.publication_key,
                    publication_paths=expected_paths,
                )
            if (
                staging_state.identity != receipt.storage_identity
                or not staging_state.is_directory
                or staging_state.is_reparse
            ):
                raise ArtifactIntegrityError(
                    "staged bundle receipt no longer identifies its entry"
                )
            with self._pin_store_directory(
                staging_path,
                windows_leaf_share_delete=True,
            ) as staging_pin:
                if staging_pin.identity != receipt.storage_identity:
                    raise ArtifactIntegrityError(
                        "staged bundle identity changed during promotion"
                    )
                staged = self._load_path(
                    staging_path,
                    relative_path=relative_path,
                    expected_digest=receipt.bundle_sha256,
                    expected_manifest_digest=receipt.manifest_sha256,
                    pinned=staging_pin,
                )
                if not self._metadata_matches(staged, expected_metadata):
                    raise ArtifactIntegrityError(
                        "staged bundle runtime metadata differs from its receipt"
                    )
                _revalidate_pinned_chain(staging_pin)
            published = self._promote_validated_staging(
                staging_path=staging_path,
                revision_path=revision_path,
                quarantine_path=quarantine_path,
                relative_path=relative_path,
                staging_identity=receipt.storage_identity,
                expected_digest=receipt.bundle_sha256,
                expected_manifest_digest=receipt.manifest_sha256,
                expected_runtime_metadata=expected_metadata,
                fence_check=fence_check,
                cancellation_event=cancellation_event,
                fault_hook=fault_hook,
            )
            return self._with_receipt(
                published,
                key=receipt.publication_key,
                publication_paths=expected_paths,
            )

    def _validated_staged_receipt(
        self,
        receipt: StagedBundleReceipt,
    ) -> tuple[BundlePublicationPaths, Mapping[str, Any]]:
        if not isinstance(receipt, StagedBundleReceipt):
            raise TypeError("staged bundle receipt is required")
        if not isinstance(receipt.publication_key, BundlePublicationKey):
            raise ArtifactIntegrityError("staged bundle receipt key is invalid")
        expected_paths = paths_for(receipt.publication_key)
        if receipt.paths != expected_paths:
            raise ArtifactIntegrityError("staged bundle receipt paths are invalid")
        if _SHA256.fullmatch(receipt.bundle_sha256 or "") is None:
            raise ArtifactIntegrityError("staged bundle receipt digest is invalid")
        if _SHA256.fullmatch(receipt.manifest_sha256 or "") is None:
            raise ArtifactIntegrityError(
                "staged bundle receipt manifest digest is invalid"
            )
        expected_metadata = self._validated_recovery_metadata(
            receipt.runtime_metadata
        )
        return expected_paths, expected_metadata

    def quarantine_staged(self, receipt: StagedBundleReceipt) -> None:
        expected_paths, expected_metadata = self._validated_staged_receipt(
            receipt
        )
        relative_path = expected_paths.revision_relative_path
        staging_path = self._root.joinpath(
            *PurePosixPath(expected_paths.staging_relative_path).parts
        )
        revision_path = self._root.joinpath(
            *PurePosixPath(relative_path).parts
        )
        quarantine_path = self._root.joinpath(
            *PurePosixPath(expected_paths.quarantine_relative_path).parts
        )

        with self._publication_lock():
            staging_state = _path_entry_state(staging_path)
            quarantine_state = _path_entry_state(quarantine_path)
            if not staging_state.exists:
                if (
                    quarantine_state.identity == receipt.storage_identity
                    and quarantine_state.is_directory
                    and not quarantine_state.is_reparse
                ):
                    with self._pin_store_directory(
                        quarantine_path,
                    ) as quarantine_pin, self._pin_store_directory(
                        staging_path.parent,
                    ) as staging_parent_pin:
                        if quarantine_pin.identity != receipt.storage_identity:
                            raise ArtifactReconciliationError(
                                "quarantined staging identity changed"
                            )
                        self._fsync_directory(
                            quarantine_path,
                            descriptor=quarantine_pin.descriptor,
                        )
                        self._fsync_directory(
                            quarantine_path.parent,
                            descriptor=quarantine_pin.parent_descriptor,
                        )
                        self._fsync_directory(
                            staging_path.parent,
                            descriptor=staging_parent_pin.descriptor,
                        )
                    return
                revision_state = _path_entry_state(revision_path)
                if revision_state.identity == receipt.storage_identity:
                    raise ArtifactReconciliationError(
                        "staged receipt was already promoted"
                    )
                raise ArtifactReconciliationError(
                    "staged receipt no longer identifies a live entry"
                )
            if (
                staging_state.identity != receipt.storage_identity
                or not staging_state.is_directory
                or staging_state.is_reparse
            ):
                raise ArtifactReconciliationError(
                    "live staging entry differs from its receipt"
                )
            if quarantine_state.exists:
                raise ArtifactReconciliationError(
                    "staging quarantine path is occupied"
                )
            with self._pin_store_directory(
                staging_path,
                windows_leaf_share_delete=True,
            ) as staging_pin:
                if staging_pin.identity != receipt.storage_identity:
                    raise ArtifactReconciliationError(
                        "staging identity changed before quarantine"
                    )
                try:
                    staged = self._load_path(
                        staging_path,
                        relative_path=relative_path,
                        expected_digest=receipt.bundle_sha256,
                        expected_manifest_digest=receipt.manifest_sha256,
                        pinned=staging_pin,
                    )
                except ArtifactIntegrityError as exc:
                    raise ArtifactReconciliationError(
                        "staging bytes differ from their receipt"
                    ) from exc
                if not self._metadata_matches(staged, expected_metadata):
                    raise ArtifactReconciliationError(
                        "staging metadata differs from its receipt"
                    )
                _revalidate_pinned_chain(staging_pin)
            self._relocate_exact_directory(
                staging_path,
                quarantine_path,
                expected_identity=receipt.storage_identity,
            )

    def recover(
        self,
        *,
        key: BundlePublicationKey,
        expected_bundle_sha256: str,
        expected_manifest_sha256: str,
        expected_runtime_metadata: Mapping[str, Any],
        fence_check: Callable[[str], None] | None = None,
        cancellation_event: threading.Event | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> BundleRecoveryResult:
        if not isinstance(key, BundlePublicationKey):
            raise TypeError("publication key is required")
        if _SHA256.fullmatch(expected_bundle_sha256 or "") is None:
            raise ValueError("expected_bundle_sha256 must be lowercase SHA-256")
        if _SHA256.fullmatch(expected_manifest_sha256 or "") is None:
            raise ValueError(
                "expected_manifest_sha256 must be lowercase SHA-256"
            )
        metadata = self._validated_recovery_metadata(expected_runtime_metadata)
        publication_paths = paths_for(key)
        with self._publication_lock():
            return self._recover_locked(
                key=key,
                publication_paths=publication_paths,
                expected_bundle_sha256=expected_bundle_sha256,
                expected_manifest_sha256=expected_manifest_sha256,
                expected_runtime_metadata=metadata,
                fence_check=fence_check,
                cancellation_event=cancellation_event,
                fault_hook=fault_hook,
            )

    def _recover_locked(
        self,
        *,
        key: BundlePublicationKey,
        publication_paths: BundlePublicationPaths,
        expected_bundle_sha256: str,
        expected_manifest_sha256: str,
        expected_runtime_metadata: Mapping[str, Any],
        fence_check: Callable[[str], None] | None,
        cancellation_event: threading.Event | None,
        fault_hook: Callable[[str], None] | None,
    ) -> BundleRecoveryResult:
        relative_path = publication_paths.revision_relative_path
        revision_path = self._root.joinpath(*PurePosixPath(relative_path).parts)
        staging_path = self._root.joinpath(
            *PurePosixPath(publication_paths.staging_relative_path).parts
        )
        quarantine_path = self._root.joinpath(
            *PurePosixPath(publication_paths.quarantine_relative_path).parts
        )

        _check_publication_not_revoked(cancellation_event)
        if fence_check is not None:
            fence_check("before_recovery")
        self._fault(fault_hook, "before_recovery")

        def recovery_quarantine_check() -> None:
            if fence_check is not None:
                fence_check("before_recovery_quarantine")

        revision_state = _path_entry_state(revision_path)
        if revision_state.exists:
            if not revision_state.is_directory or revision_state.is_reparse:
                return BundleRecoveryResult(
                    disposition=BundleRecoveryDisposition.FOREIGN,
                    observed_identity=revision_state.identity,
                    detail="immutable revision path contains a foreign entry",
                )
            try:
                existing = self.load(
                    relative_path,
                    expected_digest=expected_bundle_sha256,
                    expected_manifest_digest=expected_manifest_sha256,
                )
            except ArtifactIntegrityError:
                return BundleRecoveryResult(
                    disposition=BundleRecoveryDisposition.COLLISION,
                    observed_identity=revision_state.identity,
                    detail="immutable revision directory differs from journal evidence",
                )
            if not self._metadata_matches(existing, expected_runtime_metadata):
                return BundleRecoveryResult(
                    disposition=BundleRecoveryDisposition.COLLISION,
                    observed_identity=existing.storage_identity,
                    detail="immutable revision metadata differs from journal evidence",
                )
            _check_publication_not_revoked(cancellation_event)
            self._redurable_existing_revision(
                revision_path,
                expected_identity=existing.storage_identity,
            )
            published = self._with_receipt(
                existing,
                key=key,
                publication_paths=publication_paths,
            )
            return BundleRecoveryResult(
                disposition=BundleRecoveryDisposition.FINAL_VALID,
                published=published,
                observed_identity=existing.storage_identity,
                detail="exact durable immutable revision is present",
            )

        staging_state = _path_entry_state(staging_path)
        quarantine_state = _path_entry_state(quarantine_path)
        if not staging_state.exists:
            if not quarantine_state.exists:
                return BundleRecoveryResult(
                    disposition=BundleRecoveryDisposition.ABSENT,
                    detail="journaled publication has no filesystem entry",
                )
            if (
                not quarantine_state.is_directory
                or quarantine_state.is_reparse
            ):
                return BundleRecoveryResult(
                    disposition=BundleRecoveryDisposition.FOREIGN,
                    observed_identity=quarantine_state.identity,
                    quarantined=True,
                    detail="publication quarantine path contains a foreign entry",
                )
            try:
                with self._pin_store_directory(
                    quarantine_path,
                ) as quarantine_pin:
                    quarantined_bundle = self._load_path(
                        quarantine_path,
                        relative_path=relative_path,
                        expected_digest=expected_bundle_sha256,
                        expected_manifest_digest=expected_manifest_sha256,
                        pinned=quarantine_pin,
                    )
                    if not self._metadata_matches(
                        quarantined_bundle,
                        expected_runtime_metadata,
                    ):
                        raise ArtifactIntegrityError(
                            "quarantined runtime metadata differs"
                        )
                    self._fsync_directory(
                        quarantine_path,
                        descriptor=quarantine_pin.descriptor,
                    )
                    self._fsync_directory(
                        quarantine_path.parent,
                        descriptor=quarantine_pin.parent_descriptor,
                    )
            except ArtifactIntegrityError:
                if self._independently_valid_bundle(
                    quarantine_path,
                    relative_path=relative_path,
                    expected_identity=quarantine_state.identity,
                ) is not None:
                    return BundleRecoveryResult(
                        disposition=BundleRecoveryDisposition.COLLISION,
                        observed_identity=quarantine_state.identity,
                        quarantined=True,
                        detail="quarantine contains another valid bundle",
                    )
                return BundleRecoveryResult(
                    disposition=BundleRecoveryDisposition.PARTIAL,
                    observed_identity=quarantine_state.identity,
                    quarantined=True,
                    detail="partial journaled bytes are already quarantined",
                )
            return BundleRecoveryResult(
                disposition=BundleRecoveryDisposition.PARTIAL,
                observed_identity=quarantine_state.identity,
                quarantined=True,
                detail="exact journaled bytes are already quarantined",
            )

        if staging_state.identity is None:
            raise ArtifactIntegrityError(
                "staging entry has no stable filesystem identity"
            )
        if quarantine_state.exists:
            return BundleRecoveryResult(
                disposition=BundleRecoveryDisposition.COLLISION,
                observed_identity=quarantine_state.identity,
                detail="publication staging and quarantine paths are both occupied",
            )

        if not staging_state.is_directory or staging_state.is_reparse:
            try:
                self._relocate_exact_entry(
                    staging_path,
                    quarantine_path,
                    expected_identity=staging_state.identity,
                    retry_check=recovery_quarantine_check,
                    cancellation_event=cancellation_event,
                )
            except ArtifactCollisionError:
                current = _path_entry_state(quarantine_path)
                return BundleRecoveryResult(
                    disposition=BundleRecoveryDisposition.COLLISION,
                    observed_identity=current.identity,
                    detail="foreign staging entry could not be quarantined exclusively",
                )
            self._fault(fault_hook, "after_recovery_quarantine")
            return BundleRecoveryResult(
                disposition=BundleRecoveryDisposition.FOREIGN,
                observed_identity=staging_state.identity,
                quarantined=True,
                detail="foreign staging entry was quarantined by exact identity",
            )

        staged: PublishedBundle | None = None
        try:
            with self._pin_store_directory(
                staging_path,
                windows_leaf_share_delete=True,
            ) as staging_pin:
                if staging_pin.identity != staging_state.identity:
                    raise ArtifactIntegrityError(
                        "staging identity changed during recovery inspection"
                    )
                staged = self._load_path(
                    staging_path,
                    relative_path=relative_path,
                    expected_digest=expected_bundle_sha256,
                    expected_manifest_digest=expected_manifest_sha256,
                    pinned=staging_pin,
                )
                if not self._metadata_matches(
                    staged,
                    expected_runtime_metadata,
                ):
                    staged = None
                    raise ArtifactIntegrityError(
                        "staging metadata differs from journal evidence"
                    )
                _revalidate_pinned_chain(staging_pin)
        except ArtifactIntegrityError:
            if self._independently_valid_bundle(
                staging_path,
                relative_path=relative_path,
                expected_identity=staging_state.identity,
            ) is not None:
                return BundleRecoveryResult(
                    disposition=BundleRecoveryDisposition.COLLISION,
                    observed_identity=staging_state.identity,
                    detail="staging contains another valid bundle",
                )
            try:
                self._relocate_exact_entry(
                    staging_path,
                    quarantine_path,
                    expected_identity=staging_state.identity,
                    retry_check=recovery_quarantine_check,
                    cancellation_event=cancellation_event,
                )
            except ArtifactCollisionError:
                current = _path_entry_state(quarantine_path)
                return BundleRecoveryResult(
                    disposition=BundleRecoveryDisposition.COLLISION,
                    observed_identity=current.identity,
                    detail="invalid staging could not be quarantined exclusively",
                )
            self._fault(fault_hook, "after_recovery_quarantine")
            return BundleRecoveryResult(
                disposition=BundleRecoveryDisposition.PARTIAL,
                observed_identity=staging_state.identity,
                quarantined=True,
                detail="invalid or partial staging was quarantined by exact identity",
            )

        if staged is None:  # pragma: no cover
            raise ArtifactIntegrityError("recovery did not validate staging bytes")
        try:
            published = self._promote_validated_staging(
                staging_path=staging_path,
                revision_path=revision_path,
                quarantine_path=quarantine_path,
                relative_path=relative_path,
                staging_identity=staging_state.identity,
                expected_digest=expected_bundle_sha256,
                expected_manifest_digest=expected_manifest_sha256,
                expected_runtime_metadata=expected_runtime_metadata,
                fence_check=fence_check,
                cancellation_event=cancellation_event,
                fault_hook=fault_hook,
            )
        except ArtifactCollisionError:
            current = _path_entry_state(revision_path)
            return BundleRecoveryResult(
                disposition=BundleRecoveryDisposition.COLLISION,
                observed_identity=current.identity,
                detail="immutable revision appeared with different bytes",
            )
        published = self._with_receipt(
            published,
            key=key,
            publication_paths=publication_paths,
        )
        return BundleRecoveryResult(
            disposition=BundleRecoveryDisposition.STAGING_PROMOTED,
            published=published,
            observed_identity=published.storage_identity,
            detail="exact durable staging was promoted",
        )

    def quarantine_receipt(self, receipt: BundlePublicationReceipt) -> None:
        if not isinstance(receipt, BundlePublicationReceipt):
            raise TypeError("bundle publication receipt is required")
        if not isinstance(receipt.publication_key, BundlePublicationKey):
            raise ArtifactReconciliationError("bundle receipt key is invalid")
        expected_paths = paths_for(receipt.publication_key)
        if expected_paths != receipt.paths:
            raise ArtifactReconciliationError(
                "bundle receipt paths are inconsistent"
            )
        revision_path = self._root.joinpath(
            *PurePosixPath(expected_paths.revision_relative_path).parts
        )
        quarantine_path = self._root.joinpath(
            *PurePosixPath(expected_paths.quarantine_relative_path).parts
        )
        with self._publication_lock():
            revision_state = _path_entry_state(revision_path)
            quarantine_state = _path_entry_state(quarantine_path)
            if (
                not revision_state.exists
                and quarantine_state.identity == receipt.storage_identity
                and quarantine_state.is_directory
                and not quarantine_state.is_reparse
            ):
                with self._pin_store_directory(
                    quarantine_path,
                ) as quarantine_pin, self._pin_store_directory(
                    revision_path.parent,
                ) as revision_parent_pin:
                    if quarantine_pin.identity != receipt.storage_identity:
                        raise ArtifactReconciliationError(
                            "quarantined artifact identity changed"
                        )
                    self._fsync_directory(
                        quarantine_path,
                        descriptor=quarantine_pin.descriptor,
                    )
                    self._fsync_directory(
                        quarantine_path.parent,
                        descriptor=quarantine_pin.parent_descriptor,
                    )
                    self._fsync_directory(
                        revision_path.parent,
                        descriptor=revision_parent_pin.descriptor,
                    )
                return
            if revision_state.identity != receipt.storage_identity:
                raise ArtifactReconciliationError(
                    "live artifact no longer matches the publication receipt"
                )
            if quarantine_state.exists:
                raise ArtifactReconciliationError(
                    "artifact receipt quarantine path is occupied"
                )
            self.load(
                expected_paths.revision_relative_path,
                expected_digest=receipt.bundle_sha256,
                expected_manifest_digest=receipt.manifest_sha256,
            )
            self._relocate_exact_directory(
                revision_path,
                quarantine_path,
                expected_identity=receipt.storage_identity,
            )

    def _publish_locked(
        self,
        finalized: FinalizedBundle,
        *,
        key: BundlePublicationKey,
        fence_check: Callable[[str], None] | None,
        cancellation_event: threading.Event | None,
        fault_hook: Callable[[str], None] | None,
    ) -> PublishedBundle:
        if not isinstance(finalized, FinalizedBundle):
            raise TypeError("finalized must be FinalizedBundle")
        if not isinstance(key, BundlePublicationKey):
            raise TypeError("publication key is required")
        if finalized.contract != self._contract:
            raise ArtifactIntegrityError("finalized bundle contract does not match store")
        if finalized.scope_id != key.scope_id:
            raise ArtifactIntegrityError("finalized manifest scope does not match path")
        if finalized.revision_id != key.revision_id:
            raise ArtifactIntegrityError("finalized manifest revision does not match path")
        if _SHA256.fullmatch(finalized.bundle_sha256 or "") is None:
            raise ArtifactIntegrityError("finalized bundle digest is invalid")

        publication_paths = paths_for(key)
        relative_path = publication_paths.revision_relative_path
        revision_path = self._root.joinpath(*PurePosixPath(relative_path).parts)
        quarantine_path = self._root.joinpath(
            *PurePosixPath(publication_paths.quarantine_relative_path).parts
        )
        staging_path = self._root.joinpath(
            *PurePosixPath(publication_paths.staging_relative_path).parts
        )
        manifest_digest = finalized.manifest_sha256

        if fence_check is not None:
            fence_check("before_stage")
        _check_publication_not_revoked(cancellation_event)
        self._fault(fault_hook, "before_stage")

        if _path_entry_exists(revision_path):
            existing = self.load(
                relative_path,
                expected_digest=finalized.bundle_sha256,
                expected_manifest_digest=manifest_digest,
            )
            self._redurable_existing_revision(
                revision_path,
                expected_identity=existing.storage_identity,
            )
            self._fsync_existing_directory(staging_path.parent)
            return self._with_receipt(
                existing,
                key=key,
                publication_paths=publication_paths,
            )

        staged = self._stage_locked(
            finalized,
            key=key,
            publication_paths=publication_paths,
            replace_existing=True,
            before_stage_checked=True,
            fence_check=fence_check,
            cancellation_event=cancellation_event,
            fault_hook=fault_hook,
        )
        published = self._promote_validated_staging(
            staging_path=staging_path,
            revision_path=revision_path,
            quarantine_path=quarantine_path,
            relative_path=relative_path,
            staging_identity=staged.storage_identity,
            expected_digest=staged.bundle_sha256,
            expected_manifest_digest=staged.manifest_sha256,
            expected_runtime_metadata=staged.runtime_metadata,
            fence_check=fence_check,
            cancellation_event=cancellation_event,
            fault_hook=fault_hook,
        )
        return self._with_receipt(
            published,
            key=key,
            publication_paths=publication_paths,
        )

    def load(
        self,
        bundle_relative_path: str,
        *,
        expected_digest: str,
        expected_manifest_digest: str | None = None,
    ) -> PublishedBundle:
        if _SHA256.fullmatch(expected_digest or "") is None:
            raise ValueError("expected_digest must be lowercase SHA-256")
        if (
            expected_manifest_digest is not None
            and _SHA256.fullmatch(expected_manifest_digest) is None
        ):
            raise ValueError("expected_manifest_digest must be lowercase SHA-256")
        if not isinstance(bundle_relative_path, str):
            raise ValueError("bundle_relative_path must be text")
        relative = PurePosixPath(bundle_relative_path)
        if (
            relative.is_absolute()
            or "\\" in bundle_relative_path
            or len(relative.parts) != 3
            or relative.parts[0] != "revisions"
            or ".." in relative.parts
        ):
            raise ValueError("bundle_relative_path is outside the revision root")
        _safe_scope_id(relative.parts[1])
        _uuid_text(relative.parts[2], "revision_id")
        path = self._root.joinpath(*relative.parts)
        return self._load_path(
            path,
            relative_path=relative.as_posix(),
            expected_digest=expected_digest,
            expected_manifest_digest=expected_manifest_digest,
        )

    def _load_path(
        self,
        path: Path,
        *,
        relative_path: str,
        expected_digest: str | None,
        expected_manifest_digest: str | None,
        pinned: _PinnedDirectoryChain | None = None,
    ) -> PublishedBundle:
        if pinned is None:
            try:
                with self._pin_store_directory(path) as current_pin:
                    return self._load_path(
                        path,
                        relative_path=relative_path,
                        expected_digest=expected_digest,
                        expected_manifest_digest=expected_manifest_digest,
                        pinned=current_pin,
                    )
            except ArtifactIntegrityError:
                raise
            except ArtifactPublicationError as exc:
                raise ArtifactIntegrityError(
                    "artifact revision directory is unavailable"
                ) from exc
        if pinned.target != _absolute_without_link_resolution(path):
            raise ArtifactIntegrityError("artifact directory pin is for another path")
        _revalidate_pinned_chain(pinned)

        directory_descriptor = pinned.descriptor
        expected_names = set(self._contract.file_names) | {
            self._contract.manifest_filename
        }
        if directory_descriptor is None:
            entry_names = {entry.name for entry in path.iterdir()}
        else:
            entry_names = set(os.listdir(directory_descriptor))
        if entry_names != expected_names:
            raise ArtifactIntegrityError("artifact revision contents are not exact")

        def read_exact_file(filename: str, maximum: int) -> bytes:
            if directory_descriptor is None:
                entry = path / filename
                if _path_is_reparse_or_symlink(entry) or not entry.is_file():
                    raise ArtifactIntegrityError(
                        "artifact revision contains an unsafe entry"
                    )
                raw = entry.read_bytes()
            else:
                status = os.stat(
                    filename,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                file_attributes = int(
                    getattr(status, "st_file_attributes", 0)
                )
                if (
                    not stat.S_ISREG(status.st_mode)
                    or file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise ArtifactIntegrityError(
                        "artifact revision contains an unsafe entry"
                    )
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(
                    filename,
                    flags,
                    dir_fd=directory_descriptor,
                )
                try:
                    chunks = []
                    remaining = maximum + 1
                    while remaining > 0:
                        chunk = os.read(descriptor, min(remaining, 64 * 1024))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                finally:
                    os.close(descriptor)
            if len(raw) > maximum:
                raise ArtifactIntegrityError("artifact file exceeds size limit")
            return raw

        files: dict[str, str] = {}
        for filename in self._contract.file_names:
            raw = read_exact_file(filename, self._contract.max_file_bytes)
            if len(raw) > self._contract.max_file_bytes:
                raise ArtifactIntegrityError("bundle file exceeds size limit")
            try:
                files[filename] = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ArtifactIntegrityError("bundle file is not UTF-8") from exc

        digest = canonical_bundle_digest(files, self._contract)
        if expected_digest is not None and digest != expected_digest:
            raise ArtifactIntegrityError("artifact bundle digest mismatch")

        manifest_bytes = read_exact_file(
            self._contract.manifest_filename,
            self._contract.max_manifest_bytes,
        )
        if len(manifest_bytes) > self._contract.max_manifest_bytes:
            raise ArtifactIntegrityError("runtime manifest exceeds size limit")
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        if (
            expected_manifest_digest is not None
            and manifest_digest != expected_manifest_digest
        ):
            raise ArtifactIntegrityError("runtime manifest digest mismatch")
        try:
            manifest_json = manifest_bytes.decode("utf-8")
            manifest = json.loads(manifest_json)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError("runtime manifest is invalid JSON") from exc
        if not isinstance(manifest, dict):
            raise ArtifactIntegrityError("runtime manifest must be an object")

        try:
            finalized = FinalizedBundle(
                contract=self._contract,
                files=files,
                bundle_sha256=digest,
                manifest=manifest,
                manifest_json=manifest_json,
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactIntegrityError("runtime manifest contract is invalid") from exc
        relative = PurePosixPath(relative_path)
        if finalized.scope_id != relative.parts[1] or finalized.revision_id != relative.parts[2]:
            raise ArtifactIntegrityError("runtime manifest identity mismatch")

        return PublishedBundle(
            bundle_relative_path=relative.as_posix(),
            bundle_sha256=digest,
            manifest_sha256=manifest_digest,
            files=finalized.files,
            manifest=finalized.manifest,
            manifest_json=manifest_json,
            runtime_metadata=finalized.runtime_metadata,
            storage_identity=pinned.identity,
        )


__all__ = [
    "ArtifactCollisionError",
    "ArtifactIntegrityError",
    "ArtifactPublicationError",
    "ArtifactPublicationRevokedError",
    "ArtifactReconciliationError",
    "BundlePublicationKey",
    "BundlePublicationPaths",
    "BundlePublicationReceipt",
    "BundleRecoveryDisposition",
    "BundleRecoveryResult",
    "FinalizedBundle",
    "ImmutableBundleContract",
    "ImmutableBundleStore",
    "PublishedBundle",
    "StagedBundleReceipt",
    "canonical_bundle_digest",
    "paths_for",
    "runtime_metadata_for_manifest",
]
