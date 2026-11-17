"""Configured, path-independent streaming blob storage: consumers get validated
owner/key identities, never a reopenable path, and new bytes can only be staged
through the materialization repository's DB-fenced session.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import os
import re
import stat
import threading
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Final, Protocol, runtime_checkable

if os.name == "nt":
    import ctypes.wintypes as wintypes
    import msvcrt
else:
    import fcntl

from astralplane.errors import PlaneError, SQLContractError

_SAFE_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,254}$")
_SAFE_VERIFICATION_OWNER: Final = re.compile(
    r"^__verif__[A-Za-z0-9][A-Za-z0-9._-]{0,245}$"
)
_SAFE_STAGING_ID: Final = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9._:@/-]{0,126}[A-Za-z0-9])?$"
)
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
_DEFAULT_IO_CHUNK_BYTES: Final = 1024 * 1024
_MAX_IO_CHUNK_BYTES: Final = 16 * 1024 * 1024
_MAX_BLOB_BYTES: Final = (1 << 63) - 1
_MAX_KEY_CHARACTERS: Final = 4096
_MAX_KEY_COMPONENTS: Final = 32
_WINDOWS_RESERVED_COMPONENTS: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)

if os.name == "nt":
    _WIN_GENERIC_READ: Final = 0x80000000
    _WIN_GENERIC_WRITE: Final = 0x40000000
    _WIN_FILE_SHARE_READ: Final = 0x00000001
    _WIN_FILE_SHARE_WRITE: Final = 0x00000002
    _WIN_FILE_SHARE_DELETE: Final = 0x00000004
    _WIN_CREATE_NEW: Final = 1
    _WIN_OPEN_EXISTING: Final = 3
    _WIN_FILE_ATTRIBUTE_NORMAL: Final = 0x00000080
    _WIN_FILE_ATTRIBUTE_DIRECTORY: Final = 0x00000010
    _WIN_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
    _WIN_FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
    _WIN_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value

    class _WinByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    _WIN_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _WIN_CREATE_FILE = _WIN_KERNEL32.CreateFileW
    _WIN_CREATE_FILE.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _WIN_CREATE_FILE.restype = wintypes.HANDLE
    _WIN_CLOSE_HANDLE = _WIN_KERNEL32.CloseHandle
    _WIN_CLOSE_HANDLE.argtypes = (wintypes.HANDLE,)
    _WIN_CLOSE_HANDLE.restype = wintypes.BOOL
    _WIN_GET_FILE_INFORMATION = _WIN_KERNEL32.GetFileInformationByHandle
    _WIN_GET_FILE_INFORMATION.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_WinByHandleFileInformation),
    )
    _WIN_GET_FILE_INFORMATION.restype = wintypes.BOOL


class BlobSizeLimitError(PlaneError):
    default_code = "blob_size_limit_exceeded"


class BlobIntegrityError(PlaneError):
    default_code = "blob_integrity_mismatch"


class _OwnerExclusionBusyError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BlobWriteResult:
    storage_key: str
    size_bytes: int
    sha256: str


_BLOB_PUBLISH_AUTHORITY_TOKEN: Final = object()


class _BlobPublishAuthority:
    __slots__ = ("lease_id", "max_bytes", "owner_id", "storage_key")

    def __init__(
        self,
        token: object,
        *,
        owner_id: str,
        storage_key: str,
        max_bytes: int,
        lease_id: str,
    ) -> None:
        if token is not _BLOB_PUBLISH_AUTHORITY_TOKEN:
            raise SQLContractError("blob publish authority is not constructible by callers")
        self.owner_id = validate_blob_owner_id(owner_id)
        self.storage_key = validate_blob_storage_key(storage_key)
        self.max_bytes = _positive_bound(max_bytes, name="max_bytes")
        self.lease_id = _normalized_staging_id(lease_id)


def _create_blob_publish_authority(
    *,
    owner_id: str,
    storage_key: str,
    max_bytes: int,
    lease_id: str,
) -> _BlobPublishAuthority:
    return _BlobPublishAuthority(
        _BLOB_PUBLISH_AUTHORITY_TOKEN,
        owner_id=owner_id,
        storage_key=storage_key,
        max_bytes=max_bytes,
        lease_id=lease_id,
    )


_BLOB_PURGE_AUTHORITY_TOKEN: Final = object()


class _BlobPurgeAuthority:
    __slots__ = ("owner_id", "storage_key", "target_scope")

    def __init__(
        self,
        token: object,
        *,
        owner_id: str,
        target_scope: str,
        storage_key: str,
    ) -> None:
        if token is not _BLOB_PURGE_AUTHORITY_TOKEN:
            raise SQLContractError("blob purge authority is not constructible by callers")
        self.owner_id = validate_blob_owner_id(owner_id)
        if target_scope not in {"attachment_prefix", "owner_namespace"}:
            raise SQLContractError("blob purge authority scope is unsupported")
        self.target_scope = target_scope
        self.storage_key = (
            validate_blob_storage_key(storage_key)
            if target_scope == "attachment_prefix"
            else "owner-namespace"
        )


def _create_blob_purge_authority(
    *,
    owner_id: str,
    target_scope: str,
    storage_key: str,
) -> _BlobPurgeAuthority:
    return _BlobPurgeAuthority(
        _BLOB_PURGE_AUTHORITY_TOKEN,
        owner_id=owner_id,
        target_scope=target_scope,
        storage_key=storage_key,
    )


@dataclass(frozen=True, slots=True)
class BlobDeleteResult:
    deleted_files: int
    deleted_directories: int
    absent_verified: bool


class BlobParserPath(os.PathLike[str]):
    __slots__ = ("__is_active", "__path")

    def __init__(self, path: Path, *, is_active: Callable[[], bool]) -> None:
        self.__path = os.fspath(path)
        self.__is_active = is_active

    def __fspath__(self) -> str:
        if not self.__is_active():
            raise PlaneError("blob parser capability lease is closed", code="blob_lease_closed")
        return self.__path


class _OwnerLockToken:
    __slots__ = ("_cross_process", "_owner", "_released", "_table")

    def __init__(self, table: _OwnerLockTable, owner: str) -> None:
        self._table = table
        self._owner = owner
        self._released = False
        self._cross_process: _CrossProcessOwnerLock | None = None

    def bind_cross_process(self, lock: _CrossProcessOwnerLock) -> None:
        if self._released or self._cross_process is not None:
            raise RuntimeError("blob owner lock token is not bindable")
        self._cross_process = lock

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        release_error: BaseException | None = None
        try:
            if self._cross_process is not None:
                self._cross_process.release()
        except BaseException as exc:
            release_error = exc
        finally:
            self._table.release(self._owner)
        if release_error is not None:
            raise release_error


class _OwnerLockTable:
    __slots__ = ("_entries", "_guard")

    def __init__(self) -> None:
        self._entries: dict[str, tuple[threading.Lock, int]] = {}
        self._guard = threading.Lock()

    def acquire(self, owner: str) -> _OwnerLockToken:
        owner = owner.casefold()
        with self._guard:
            lock, references = self._entries.get(owner, (threading.Lock(), 0))
            self._entries[owner] = (lock, references + 1)
        lock.acquire()
        return _OwnerLockToken(self, owner)

    def try_acquire(self, owner: str) -> _OwnerLockToken | None:
        owner = owner.casefold()
        with self._guard:
            lock, references = self._entries.get(owner, (threading.Lock(), 0))
            self._entries[owner] = (lock, references + 1)
        if lock.acquire(blocking=False):
            return _OwnerLockToken(self, owner)
        with self._guard:
            current_lock, current_references = self._entries[owner]
            if current_lock is not lock or current_references < 1:
                raise RuntimeError("blob owner-lock bookkeeping was corrupted")
            if current_references == 1:
                del self._entries[owner]
            else:
                self._entries[owner] = (lock, current_references - 1)
        return None

    def release(self, owner: str) -> None:
        with self._guard:
            lock, _ = self._entries[owner]
        lock.release()
        with self._guard:
            current_lock, current_references = self._entries[owner]
            if current_lock is not lock or current_references < 1:
                raise RuntimeError("blob owner-lock bookkeeping was corrupted")
            if current_references == 1:
                del self._entries[owner]
            else:
                self._entries[owner] = (lock, current_references - 1)

    def has_entries(self) -> bool:
        with self._guard:
            return bool(self._entries)


class _CrossProcessOwnerLock:
    __slots__ = ("_anchor", "_released", "_stream")

    def __init__(
        self,
        store: ExplicitRootStreamingBlobStore,
        *,
        owner: str,
        blocking: bool = True,
    ) -> None:
        digest = hashlib.sha256(owner.casefold().encode("utf-8")).hexdigest()
        anchor = _DirectoryAnchor(
            store,
            components=(".astralplane-owner-locks",),
            create=True,
        )
        stream: BinaryIO | None = None
        descriptor: int | None = None
        try:
            lock_name = f"{digest}.lock"
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = anchor.open_file(
                    lock_name,
                    flags=flags,
                    mode=0o600,
                )
            except FileExistsError:
                descriptor = anchor.open_file(
                    lock_name,
                    flags=os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                )
            stream = os.fdopen(descriptor, "r+b", closefd=True)
            descriptor = None
            metadata = os.fstat(stream.fileno())
            if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                raise PlaneError(
                    "blob owner lock is not a safe regular file",
                    code="blob_path_unsafe",
                )
            if metadata.st_size == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
                store._fsync_anchor(anchor)
            stream.seek(0)
            if os.name == "nt":
                while True:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError as exc:
                        if not blocking:
                            raise _OwnerExclusionBusyError from exc
                        time.sleep(0.01)
                        continue
                    break
            else:
                operation = fcntl.LOCK_EX
                if not blocking:
                    operation |= fcntl.LOCK_NB
                try:
                    fcntl.flock(stream.fileno(), operation)
                except BlockingIOError as exc:
                    raise _OwnerExclusionBusyError from exc
            anchor.assert_current()
        except BaseException:
            if stream is not None:
                stream.close()
            elif descriptor is not None:
                os.close(descriptor)
            anchor.close()
            raise
        self._anchor = anchor
        self._stream = stream
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        error: BaseException | None = None
        try:
            if os.name == "nt":
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        except BaseException as exc:
            error = exc
        finally:
            try:
                self._stream.close()
            finally:
                self._anchor.close()
        if error is not None:
            raise PlaneError(
                "failed to release cross-process blob owner exclusion",
                code="blob_cleanup_failed",
            ) from error


class BlobStagingReservation:
    __slots__ = ("_owner", "_store", "_token")

    def __init__(
        self,
        store: ExplicitRootStreamingBlobStore,
        *,
        owner: str,
        token: _OwnerLockToken,
    ) -> None:
        self._store = store
        self._owner = owner
        self._token: _OwnerLockToken | None = token

    def take(
        self,
        store: ExplicitRootStreamingBlobStore,
        *,
        owner: str,
    ) -> _OwnerLockToken:
        if self._store is not store or self._owner != owner or self._token is None:
            raise PlaneError(
                "blob staging reservation is invalid or already consumed",
                code="blob_staging_closed",
            )
        token = self._token
        self._token = None
        return token

    def release(self) -> None:
        token = self._token
        self._token = None
        if token is not None:
            token.release()

    def __enter__(self) -> BlobStagingReservation:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


@runtime_checkable
class StreamingBlobStore(Protocol):
    def reserve_materialization_staging(
        self,
        *,
        owner_id: str,
    ) -> BlobStagingReservation: ...

    async def areserve_materialization_staging(
        self,
        *,
        owner_id: str,
    ) -> BlobStagingReservation: ...

    def open_reader(
        self,
        *,
        owner_id: str,
        key: str,
        max_bytes: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> BlobReadStream: ...

    def open_parser_lease(
        self,
        *,
        owner_id: str,
        key: str,
        max_bytes: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> BlobParserLease: ...

    def iter_chunks(
        self,
        *,
        owner_id: str,
        key: str,
        max_bytes: int,
        chunk_size: int | None = None,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> Iterator[bytes]: ...

    async def aiter_chunks(
        self,
        *,
        owner_id: str,
        key: str,
        max_bytes: int,
        chunk_size: int | None = None,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> AsyncIterator[bytes]: ...

    def is_absent(self, *, owner_id: str, key: str) -> bool: ...

    def is_prefix_absent(self, *, owner_id: str, prefix: str) -> bool: ...

    def is_owner_absent(self, *, owner_id: str) -> bool: ...

    @property
    def root_identity_digest(self) -> str: ...

    def close(self) -> None: ...


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def validate_blob_owner_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or (
            _SAFE_IDENTIFIER.fullmatch(value) is None
            and _SAFE_VERIFICATION_OWNER.fullmatch(value) is None
        )
        or not _safe_platform_component(value)
    ):
        raise SQLContractError("owner_id is not a valid bounded identifier")
    return value


def _safe_platform_component(value: str) -> bool:
    stem = value.split(".", 1)[0].casefold()
    return (
        value.rstrip(" .") == value
        and stem not in _WINDOWS_RESERVED_COMPONENTS
        and not any(character in '<>:"|?*' or ord(character) < 32 for character in value)
    )


def _normalized_key(key: str, *, name: str = "key") -> str:
    if not isinstance(key, str) or not key or "\x00" in key:
        raise SQLContractError(f"blob {name} must be a non-empty string without NUL bytes")
    if len(key) > _MAX_KEY_CHARACTERS:
        raise SQLContractError(
            f"blob {name} must not exceed {_MAX_KEY_CHARACTERS} characters"
        )
    if key.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", key):
        raise SQLContractError(f"blob {name} must be relative")
    parts = re.split(r"[/\\]", key)
    if len(parts) > _MAX_KEY_COMPONENTS:
        raise SQLContractError(
            f"blob {name} must not exceed {_MAX_KEY_COMPONENTS} components"
        )
    if any(not part or part in {".", ".."} or len(part) > 255 for part in parts):
        raise SQLContractError(f"blob {name} contains an unsafe path component")
    if any(part.startswith(".astralplane-") for part in parts):
        raise SQLContractError(f"blob {name} uses a reserved Plane storage component")
    if any(not _safe_platform_component(part) for part in parts):
        raise SQLContractError(f"blob {name} contains platform-specific syntax")
    return "/".join(parts)


def validate_blob_storage_key(value: str) -> str:
    return _normalized_key(value)


def _normalized_staging_id(value: str) -> str:
    if not isinstance(value, str) or _SAFE_STAGING_ID.fullmatch(value) is None:
        raise SQLContractError("staging_id is not a canonical bounded identifier")
    return value


def _positive_bound(value: int, *, name: str, maximum: int = _MAX_BLOB_BYTES) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise SQLContractError(f"{name} must be an integer in [1, {maximum}]")
    return value


def _optional_size(value: int | None, *, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise SQLContractError(f"expected_size_bytes must be an integer in [0, {maximum}]")
    return value


def _optional_digest(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or _SHA256.fullmatch(value) is None):
        raise SQLContractError("expected_sha256 must be a lowercase SHA-256 digest")
    return value


def _chunk_bytes(chunk: object) -> memoryview:
    if not isinstance(chunk, bytes):
        raise SQLContractError("blob chunks must be bytes")
    return memoryview(chunk)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int] | None:
    device = getattr(metadata, "st_dev", 0)
    inode = getattr(metadata, "st_ino", 0)
    if os.name == "nt":
        device &= 0xFFFFFFFF
    return None if not device or not inode else (device, inode)


async def _cancel_safe_to_thread(
    function: Callable[..., Any],
    /,
    *args: object,
    cleanup_on_cancel: Callable[[Any], None] | None = None,
    **kwargs: object,
) -> Any:
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    return await _observe_cancel_safe_worker(
        worker,
        cleanup_on_cancel=cleanup_on_cancel,
        cleanup_executor=None,
    )


async def _cancel_safe_in_executor(
    executor: Executor,
    function: Callable[..., Any],
    /,
    *args: object,
    cleanup_on_cancel: Callable[[Any], None] | None = None,
    **kwargs: object,
) -> Any:
    worker = asyncio.ensure_future(
        asyncio.get_running_loop().run_in_executor(
            executor,
            partial(function, *args, **kwargs),
        )
    )
    return await _observe_cancel_safe_worker(
        worker,
        cleanup_on_cancel=cleanup_on_cancel,
        cleanup_executor=executor,
    )


async def _observe_cancel_safe_worker(
    worker: asyncio.Future[Any],
    *,
    cleanup_on_cancel: Callable[[Any], None] | None,
    cleanup_executor: Executor | None,
) -> Any:
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as first_cancellation:
        cancellation = first_cancellation

    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as repeated_cancellation:
            cancellation = repeated_cancellation
        except BaseException as worker_error:
            raise worker_error from cancellation

    try:
        result = worker.result()
    except BaseException as worker_error:
        raise worker_error from cancellation

    if cleanup_on_cancel is not None:
        if cleanup_executor is None:
            cleanup = asyncio.create_task(asyncio.to_thread(cleanup_on_cancel, result))
        else:
            cleanup = asyncio.ensure_future(
                asyncio.get_running_loop().run_in_executor(
                    cleanup_executor,
                    cleanup_on_cancel,
                    result,
                )
            )
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as repeated_cancellation:
                cancellation = repeated_cancellation
        try:
            cleanup.result()
        except BaseException as cleanup_error:
            raise cleanup_error from cancellation
    raise cancellation


def _windows_handle_information(handle: int) -> Any:
    information = _WinByHandleFileInformation()
    if not _WIN_GET_FILE_INFORMATION(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    return information


def _windows_handle_identity(handle: int) -> tuple[int, int]:
    information = _windows_handle_information(handle)
    file_index = (information.file_index_high << 32) | information.file_index_low
    if not information.volume_serial_number or not file_index:
        raise PlaneError(
            "blob filesystem does not expose stable object identities",
            code="blob_path_unsafe",
        )
    return information.volume_serial_number, file_index


def _windows_extended_path(path: Path) -> str:
    supplied = os.fspath(path)
    drive, tail = os.path.splitdrive(supplied)
    if (
        supplied.startswith(("\\\\?\\", "\\\\.\\", "\\\\"))
        or re.fullmatch(r"[A-Za-z]:", drive) is None
        or not tail.startswith(("\\", "/"))
    ):
        raise PlaneError(
            "blob path must remain on its configured local drive",
            code="blob_path_unsafe",
        )
    exact = os.path.abspath(supplied)
    return "\\\\?\\" + exact


def _windows_open_directory(path: Path) -> int:
    handle = _WIN_CREATE_FILE(
        _windows_extended_path(path),
        _WIN_GENERIC_READ,
        _WIN_FILE_SHARE_READ | _WIN_FILE_SHARE_WRITE,
        None,
        _WIN_OPEN_EXISTING,
        _WIN_FILE_FLAG_BACKUP_SEMANTICS | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _WIN_INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _windows_handle_information(handle)
        if (
            information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or not information.file_attributes & _WIN_FILE_ATTRIBUTE_DIRECTORY
        ):
            raise PlaneError(
                "blob path crosses a link, reparse point, or non-directory",
                code="blob_path_unsafe",
            )
        return int(handle)
    except BaseException:
        _WIN_CLOSE_HANDLE(handle)
        raise


def _windows_open_file_descriptor(
    path: Path,
    *,
    flags: int,
    temporary: bool = False,
    deny_write_sharing: bool = False,
) -> int:
    wants_write = bool(flags & (os.O_WRONLY | os.O_RDWR))
    wants_read = not bool(flags & os.O_WRONLY) or bool(flags & os.O_RDWR)
    access = (0 if not wants_read else _WIN_GENERIC_READ) | (
        0 if not wants_write else _WIN_GENERIC_WRITE
    )
    disposition = (
        _WIN_CREATE_NEW
        if flags & os.O_CREAT and flags & os.O_EXCL
        else _WIN_OPEN_EXISTING
    )
    share = _WIN_FILE_SHARE_READ | _WIN_FILE_SHARE_WRITE
    if deny_write_sharing:
        share = _WIN_FILE_SHARE_READ
    if temporary:
        share = _WIN_FILE_SHARE_READ | _WIN_FILE_SHARE_DELETE
    handle = _WIN_CREATE_FILE(
        _windows_extended_path(path),
        access,
        share,
        None,
        disposition,
        _WIN_FILE_ATTRIBUTE_NORMAL | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _WIN_INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor_flags = os.O_BINARY
        if wants_read and wants_write:
            descriptor_flags |= os.O_RDWR
        elif wants_write:
            descriptor_flags |= os.O_WRONLY
        else:
            descriptor_flags |= os.O_RDONLY
        descriptor = msvcrt.open_osfhandle(int(handle), descriptor_flags)
        metadata = os.fstat(descriptor)
        if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise PlaneError(
                "blob locator is not a safe regular file",
                code="blob_path_unsafe",
            )
        return descriptor
    except BaseException:
        if "descriptor" not in locals():
            _WIN_CLOSE_HANDLE(handle)
        raise


class _DirectoryAnchor:
    __slots__ = ("_closed", "_components", "_identities", "_resources", "_store", "path")

    def __init__(
        self,
        store: ExplicitRootStreamingBlobStore,
        *,
        components: tuple[str, ...],
        create: bool,
    ) -> None:
        self._store = store
        self._components = components
        self._resources: list[int] = []
        self._identities: list[tuple[int, int]] = []
        self._closed = False
        self.path = store._root
        try:
            if os.name == "nt":
                root_resource = _windows_open_directory(store._root)
            else:
                root_resource = os.open(
                    store._root,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
            self._append_resource(root_resource)
            current = store._root
            for component in components:
                exists = self._exact_component_exists(current, component)
                if not exists:
                    if not create:
                        raise FileNotFoundError(os.fspath(current / component))
                    self._mkdir(component, current)
                    if not self._exact_component_exists(current, component):
                        raise PlaneError(
                            "blob directory did not retain its exact identity",
                            code="blob_path_unsafe",
                        )
                child = current / component
                try:
                    resource = (
                        _windows_open_directory(child)
                        if os.name == "nt"
                        else os.open(
                            component,
                            os.O_RDONLY
                            | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=self._resources[-1],
                        )
                    )
                except FileNotFoundError:
                    raise
                except OSError as exc:
                    raise PlaneError(
                        "blob path crosses a link, reparse point, or non-directory",
                        code="blob_path_unsafe",
                    ) from exc
                self._append_resource(resource)
                if not self._exact_component_exists(current, component, resource_index=-2):
                    raise PlaneError(
                        "blob path component changed its exact identity",
                        code="blob_path_unsafe",
                    )
                current = child
            self.path = current
            self.assert_current()
        except BaseException:
            self.close()
            raise

    @property
    def components(self) -> tuple[str, ...]:
        return self._components

    @property
    def identity(self) -> tuple[int, int]:
        return self._identities[-1]

    @property
    def descriptor(self) -> int:
        if self._closed or os.name == "nt":
            raise RuntimeError("a POSIX directory descriptor is unavailable")
        return self._resources[-1]

    def __enter__(self) -> _DirectoryAnchor:
        if self._closed:
            raise RuntimeError("directory anchor is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource in reversed(self._resources):
            if os.name == "nt":
                _WIN_CLOSE_HANDLE(resource)
            else:
                os.close(resource)
        self._resources.clear()

    def assert_current(self) -> None:
        current = self._store._root
        paths = [current]
        for component in self._components:
            current /= component
            paths.append(current)
        for path, identity in zip(paths, self._identities, strict=True):
            try:
                metadata = os.lstat(
                    _windows_extended_path(path) if os.name == "nt" else path
                )
            except OSError as exc:
                raise PlaneError(
                    "blob directory anchor no longer resolves below its configured root",
                    code="blob_path_unsafe",
                ) from exc
            if (
                _is_reparse(metadata)
                or not stat.S_ISDIR(metadata.st_mode)
                or _metadata_identity(metadata) != identity
            ):
                raise PlaneError(
                    "blob directory anchor changed identity",
                    code="blob_path_unsafe",
                )

    def stat_entry(self, name: str) -> os.stat_result:
        if os.name == "nt":
            return os.lstat(_windows_extended_path(self.path / name))
        return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)

    def open_file(
        self,
        name: str,
        *,
        flags: int,
        mode: int = 0o600,
        temporary: bool = False,
        deny_write_sharing: bool = False,
    ) -> int:
        self._assert_exact_file_case(name, allow_missing=bool(flags & os.O_CREAT))
        if os.name == "nt":
            descriptor = _windows_open_file_descriptor(
                self.path / name,
                flags=flags,
                temporary=temporary,
                deny_write_sharing=deny_write_sharing,
            )
        else:
            descriptor = os.open(
                name,
                flags | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=self.descriptor,
            )
        self._assert_exact_file_case(name, allow_missing=False)
        return descriptor

    def replace(self, source: str, target: str) -> None:
        if os.name == "nt":
            os.replace(
                _windows_extended_path(self.path / source),
                _windows_extended_path(self.path / target),
            )
        else:
            os.replace(
                source,
                target,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )

    def unlink(self, name: str) -> None:
        if os.name == "nt":
            os.unlink(_windows_extended_path(self.path / name))
        else:
            os.unlink(name, dir_fd=self.descriptor)

    def rmdir(self, name: str) -> None:
        if os.name == "nt":
            os.rmdir(_windows_extended_path(self.path / name))
        else:
            os.rmdir(name, dir_fd=self.descriptor)

    def scandir(self) -> Any:
        return os.scandir(
            _windows_extended_path(self.path) if os.name == "nt" else self.descriptor
        )

    def _append_resource(self, resource: int) -> None:
        try:
            identity = (
                _windows_handle_identity(resource)
                if os.name == "nt"
                else _metadata_identity(os.fstat(resource))
            )
        except BaseException:
            if os.name == "nt":
                _WIN_CLOSE_HANDLE(resource)
            else:
                os.close(resource)
            raise
        if identity is None:
            if os.name == "nt":
                _WIN_CLOSE_HANDLE(resource)
            else:
                os.close(resource)
            raise PlaneError(
                "blob filesystem does not expose stable object identities",
                code="blob_path_unsafe",
            )
        self._resources.append(resource)
        self._identities.append(identity)
        if len(self._identities) == 1 and identity != self._store._root_identity:
            raise PlaneError(
                "configured blob root changed identity",
                code="blob_path_unsafe",
            )

    def _mkdir(self, component: str, current: Path) -> None:
        try:
            if os.name == "nt":
                os.mkdir(_windows_extended_path(current / component), mode=0o700)
            else:
                os.mkdir(component, mode=0o700, dir_fd=self._resources[-1])
        except FileExistsError:
            pass
        if os.name != "nt":
            os.fsync(self._resources[-1])

    def _exact_component_exists(
        self,
        parent: Path,
        expected: str,
        *,
        resource_index: int = -1,
    ) -> bool:
        source: int | Path = (
            Path(_windows_extended_path(parent))
            if os.name == "nt"
            else self._resources[resource_index]
        )
        exact = False
        folded_match = False
        with os.scandir(source) as entries:
            for entry in entries:
                if entry.name.casefold() != expected.casefold():
                    continue
                folded_match = True
                if entry.name == expected:
                    exact = True
                else:
                    raise PlaneError(
                        "blob identity aliases an existing case-folded path component",
                        code="blob_path_unsafe",
                    )
        return exact if folded_match else False

    def _assert_exact_file_case(self, expected: str, *, allow_missing: bool) -> None:
        exact = False
        source: int | Path = (
            Path(_windows_extended_path(self.path))
            if os.name == "nt"
            else self.descriptor
        )
        with os.scandir(source) as entries:
            for entry in entries:
                if entry.name.casefold() != expected.casefold():
                    continue
                if entry.name != expected:
                    raise PlaneError(
                        "blob identity aliases an existing case-folded path component",
                        code="blob_path_unsafe",
                    )
                exact = True
        if not exact and not allow_missing:
            raise FileNotFoundError(os.fspath(self.path / expected))


class BlobReadStream:
    __slots__ = (
        "_closed",
        "_expected_sha256",
        "_hasher",
        "_io_chunk_bytes",
        "_owner_lock",
        "_read_bytes",
        "_stream",
        "_verified",
        "size_bytes",
    )

    def __init__(
        self,
        stream: BinaryIO,
        *,
        size_bytes: int,
        io_chunk_bytes: int,
        expected_sha256: str | None,
        owner_lock: _OwnerLockToken,
    ) -> None:
        self._stream = stream
        self.size_bytes = size_bytes
        self._io_chunk_bytes = io_chunk_bytes
        self._expected_sha256 = expected_sha256
        self._owner_lock = owner_lock
        self._hasher = hashlib.sha256()
        self._read_bytes = 0
        self._verified = False
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> BlobReadStream:
        if self._closed:
            raise PlaneError("blob reader is closed", code="blob_reader_closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise PlaneError("blob reader is closed", code="blob_reader_closed")
        bounded = self._io_chunk_bytes if size == -1 else _positive_bound(
            size,
            name="size",
            maximum=self._io_chunk_bytes,
        )
        remaining = self.size_bytes - self._read_bytes
        if remaining == 0:
            self._verify_complete()
            return b""
        data = self._stream.read(min(bounded, remaining))
        self._read_bytes += len(data)
        self._hasher.update(data)
        if not data or self._read_bytes == self.size_bytes:
            self._verify_complete()
        return data

    def iter_chunks(self, *, chunk_size: int | None = None) -> Iterator[bytes]:
        size = self._io_chunk_bytes if chunk_size is None else _positive_bound(
            chunk_size,
            name="chunk_size",
            maximum=self._io_chunk_bytes,
        )
        while True:
            chunk = self.read(size)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        if self._closed:
            return
        verification_error: BaseException | None = None
        try:
            if self._expected_sha256 is not None and not self._verified:
                try:
                    while self._read_bytes < self.size_bytes:
                        remaining = self.size_bytes - self._read_bytes
                        data = self._stream.read(min(self._io_chunk_bytes, remaining))
                        self._read_bytes += len(data)
                        self._hasher.update(data)
                        if not data:
                            break
                    self._verify_complete()
                except BaseException as caught:
                    verification_error = caught
        finally:
            self._closed = True
            try:
                self._stream.close()
            finally:
                self._owner_lock.release()
        if verification_error is not None:
            raise verification_error

    def _abandon_unverified(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.close()
        finally:
            self._owner_lock.release()

    def _verify_complete(self) -> None:
        if self._verified:
            return
        if self._read_bytes != self.size_bytes:
            raise BlobIntegrityError("blob changed while it was being read")
        if self._stream.read(1):
            raise BlobIntegrityError("blob changed while it was being read")
        if self._expected_sha256 is not None and self._hasher.hexdigest() != self._expected_sha256:
            raise BlobIntegrityError("blob digest does not match the expected SHA-256")
        self._verified = True


class BlobParserLease:
    __slots__ = (
        "_active",
        "_anchor",
        "_capability_path",
        "_initial_metadata",
        "_max_bytes",
        "_owner_lock",
        "_store",
        "_stream",
        "_target",
    )

    def __init__(
        self,
        store: ExplicitRootStreamingBlobStore,
        *,
        target: Path,
        capability_path: Path,
        max_bytes: int,
        stream: BinaryIO,
        initial_metadata: os.stat_result,
        anchor: _DirectoryAnchor,
        owner_lock: _OwnerLockToken,
    ) -> None:
        self._store = store
        self._target = target
        self._capability_path = capability_path
        self._max_bytes = max_bytes
        self._stream = stream
        self._initial_metadata = initial_metadata
        self._anchor = anchor
        self._owner_lock = owner_lock
        self._active = False

    def __enter__(self) -> BlobParserPath:
        if self._active or self._stream.closed:
            raise PlaneError("blob parser lease is not reusable", code="blob_lease_closed")
        try:
            self._store._validate_lease_target(
                self._target,
                self._stream,
                initial=self._initial_metadata,
                max_bytes=self._max_bytes,
                anchor=self._anchor,
            )
            self._active = True
            return BlobParserPath(self._capability_path, is_active=lambda: self._active)
        except BaseException:
            try:
                self._stream.close()
            finally:
                try:
                    self._anchor.close()
                finally:
                    self._owner_lock.release()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        validation_error: BaseException | None = None
        try:
            if self._active:
                try:
                    self._store._validate_lease_target(
                        self._target,
                        self._stream,
                        initial=self._initial_metadata,
                        max_bytes=self._max_bytes,
                        anchor=self._anchor,
                    )
                except BaseException as caught:
                    validation_error = caught
        finally:
            self._active = False
            try:
                self._stream.close()
            finally:
                try:
                    self._anchor.close()
                finally:
                    self._owner_lock.release()
        if validation_error is not None:
            raise validation_error


class _AtomicWriteSession:
    __slots__ = (
        "_anchor",
        "_expected_sha256",
        "_expected_size_bytes",
        "_finalized",
        "_hasher",
        "_marker_identity",
        "_marker_name",
        "_marker_stream",
        "_max_bytes",
        "_owner",
        "_owner_lock",
        "_prepared_result",
        "_storage_key",
        "_store",
        "_stream",
        "_target",
        "_target_name",
        "_temporary",
        "_temporary_name",
        "_total",
    )

    def __init__(
        self,
        store: ExplicitRootStreamingBlobStore,
        *,
        owner: str,
        storage_key: str,
        target: Path,
        temporary: Path,
        stream: BinaryIO,
        max_bytes: int,
        expected_size_bytes: int | None,
        expected_sha256: str | None,
        marker_name: str | None,
        marker_stream: BinaryIO | None,
        anchor: _DirectoryAnchor,
        owner_lock: _OwnerLockToken,
    ) -> None:
        self._store = store
        self._anchor = anchor
        self._owner = owner
        self._storage_key = storage_key
        self._target = target
        self._target_name = target.name
        self._temporary = temporary
        self._temporary_name = temporary.name
        self._stream = stream
        self._max_bytes = max_bytes
        self._expected_size_bytes = expected_size_bytes
        self._expected_sha256 = expected_sha256
        self._marker_name = marker_name
        self._marker_stream = marker_stream
        self._marker_identity = (
            None
            if marker_stream is None
            else _metadata_identity(os.fstat(marker_stream.fileno()))
        )
        self._owner_lock = owner_lock
        self._hasher = hashlib.sha256()
        self._total = 0
        self._finalized = False
        self._prepared_result: BlobWriteResult | None = None

    def write(self, chunk: memoryview) -> None:
        proposed = self._total + len(chunk)
        if proposed > self._max_bytes:
            raise BlobSizeLimitError("blob stream exceeded its declared maximum size")
        written = self._stream.write(chunk)
        if written != len(chunk):
            raise PlaneError("blob stream accepted a partial write", code="blob_write_incomplete")
        self._hasher.update(chunk)
        self._total = proposed

    def prepare(self) -> BlobWriteResult:
        if self._finalized:
            raise PlaneError("blob staging session is closed", code="blob_staging_closed")
        if self._prepared_result is not None:
            return self._prepared_result
        digest = self._hasher.hexdigest()
        if self._expected_size_bytes is not None and self._total != self._expected_size_bytes:
            raise BlobIntegrityError("blob size does not match expected_size_bytes")
        if self._expected_sha256 is not None and digest != self._expected_sha256:
            raise BlobIntegrityError("blob digest does not match expected_sha256")
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._prepared_result = BlobWriteResult(
            storage_key=self._storage_key,
            size_bytes=self._total,
            sha256=digest,
        )
        return self._prepared_result

    def read_prefix(self, max_bytes: int) -> bytes:
        bound = _positive_bound(
            max_bytes,
            name="max_bytes",
            maximum=_MAX_IO_CHUNK_BYTES,
        )
        self.prepare()
        if self._finalized or self._stream.closed:
            raise PlaneError("blob staging session is closed", code="blob_staging_closed")
        position = self._stream.tell()
        try:
            self._stream.seek(0)
            return self._stream.read(min(bound, self._total))
        finally:
            self._stream.seek(position)

    def publish(self) -> BlobWriteResult:
        result = self.prepare()
        descriptor_metadata = os.fstat(self._stream.fileno())
        published = False
        try:
            self._store._check_chain(self._target.parent)
            self._anchor.assert_current()
            self._assert_staging_marker_current()
            self._store._verify_publish_target(
                self._target,
                anchor=self._anchor,
            )
            self._anchor.replace(self._temporary_name, self._target_name)
            published = True
            self._anchor.assert_current()
            current = self._anchor.stat_entry(self._target_name)
            if (
                _is_reparse(current)
                or not stat.S_ISREG(current.st_mode)
                or _metadata_identity(current) != _metadata_identity(descriptor_metadata)
                or current.st_size != self._total
            ):
                raise BlobIntegrityError("published blob identity changed during replacement")
            self._store._fsync_anchor(self._anchor)
        except BaseException:
            if published:
                try:
                    self._anchor.stat_entry(self._target_name)
                except FileNotFoundError:
                    pass
                else:
                    # Removed on mismatch: never leave unverified bytes published
                    self._anchor.unlink(self._target_name)
                    self._store._fsync_anchor(self._anchor)
            raise
        self._finalize()
        return result

    def abort(self) -> None:
        if self._finalized:
            return
        prune = False
        try:
            if not self._stream.closed:
                self._stream.close()
            if self._marker_stream is not None and not self._marker_stream.closed:
                self._marker_stream.close()
            try:
                self._anchor.unlink(self._temporary_name)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise PlaneError(
                    "failed to remove an unpublished blob temporary",
                    code="blob_cleanup_failed",
                ) from exc
            if self._marker_name is not None:
                try:
                    marker = self._anchor.stat_entry(self._marker_name)
                except FileNotFoundError:
                    pass
                else:
                    if (
                        _is_reparse(marker)
                        or not stat.S_ISREG(marker.st_mode)
                        or _metadata_identity(marker) != self._marker_identity
                    ):
                        raise PlaneError(
                            "unpublished staging sentinel changed identity",
                            code="blob_path_unsafe",
                        )
                    try:
                        self._anchor.unlink(self._marker_name)
                    except OSError as exc:
                        raise PlaneError(
                            "failed to remove an unpublished staging sentinel",
                            code="blob_cleanup_failed",
                        ) from exc
            try:
                self._anchor.assert_current()
            except PlaneError:
                prune = False
            else:
                prune = True
            # Anchor closes first; owner lock stays until pruning ends
            self._anchor.close()
            if prune:
                self._store._prune_empty_parents(self._temporary.parent)
        finally:
            self._finalize()

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            if not self._stream.closed:
                self._stream.close()
            if self._marker_stream is not None and not self._marker_stream.closed:
                self._marker_stream.close()
        finally:
            try:
                self._anchor.close()
            finally:
                self._owner_lock.release()

    def _assert_staging_marker_current(self) -> None:
        if self._marker_name is None:
            return
        if self._marker_stream is None or self._marker_stream.closed:
            raise PlaneError(
                "blob staging sentinel is unavailable",
                code="blob_publish_fence_conflict",
            )
        try:
            current = self._anchor.stat_entry(self._marker_name)
        except FileNotFoundError as exc:
            raise PlaneError(
                "blob staging sentinel disappeared",
                code="blob_publish_fence_conflict",
            ) from exc
        descriptor = os.fstat(self._marker_stream.fileno())
        if (
            _is_reparse(current)
            or not stat.S_ISREG(current.st_mode)
            or _metadata_identity(current) != self._marker_identity
            or _metadata_identity(descriptor) != self._marker_identity
        ):
            raise PlaneError(
                "blob staging sentinel changed identity",
                code="blob_publish_fence_conflict",
            )


class BlobStagedWrite:
    __slots__ = (
        "_evidence",
        "_owner_id",
        "_session",
        "_staging_id",
        "_storage_key",
        "_store",
    )

    def __init__(
        self,
        *,
        store: ExplicitRootStreamingBlobStore,
        session: _AtomicWriteSession,
        owner_id: str,
        staging_id: str,
        storage_key: str,
        evidence: BlobWriteResult,
    ) -> None:
        self._store = store
        self._session = session
        self._owner_id = owner_id
        self._staging_id = staging_id
        self._storage_key = storage_key
        self._evidence = evidence

    @property
    def evidence(self) -> BlobWriteResult:
        return self._evidence

    def read_prefix(self, *, max_bytes: int = 8192) -> bytes:
        return self._session.read_prefix(max_bytes)

    def abort(self) -> None:
        self._session.abort()

    def __enter__(self) -> BlobStagedWrite:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.abort()


class BlobStagingSession:
    __slots__ = ("_owner_id", "_session", "_staging_id", "_storage_key", "_store")

    def __init__(
        self,
        *,
        store: ExplicitRootStreamingBlobStore,
        session: _AtomicWriteSession,
        owner_id: str,
        staging_id: str,
        storage_key: str,
    ) -> None:
        self._store = store
        self._session: _AtomicWriteSession | None = session
        self._owner_id = owner_id
        self._staging_id = staging_id
        self._storage_key = storage_key

    def _take(self) -> _AtomicWriteSession:
        session = self._session
        if session is None:
            raise PlaneError("blob staging capability is closed", code="blob_staging_closed")
        self._session = None
        return session

    def write_chunks(self, chunks: Iterable[bytes]) -> BlobStagedWrite:
        if not isinstance(chunks, Iterable):
            raise SQLContractError("chunks must be an iterable of bytes")
        session = self._take()
        try:
            for chunk in chunks:
                view = _chunk_bytes(chunk)
                for offset in range(0, len(view), self._store._io_chunk_bytes):
                    session.write(view[offset : offset + self._store._io_chunk_bytes])
            evidence = session.prepare()
            return BlobStagedWrite(
                store=self._store,
                session=session,
                owner_id=self._owner_id,
                staging_id=self._staging_id,
                storage_key=self._storage_key,
                evidence=evidence,
            )
        except BaseException:
            session.abort()
            raise

    async def awrite_chunks(self, chunks: AsyncIterable[bytes]) -> BlobStagedWrite:
        if not isinstance(chunks, AsyncIterable):
            raise SQLContractError("chunks must be an async iterable of bytes")
        session = self._take()
        try:
            async for chunk in chunks:
                view = _chunk_bytes(chunk)
                for offset in range(0, len(view), self._store._io_chunk_bytes):
                    await self._store._run_stage_io(
                        session.write,
                        view[offset : offset + self._store._io_chunk_bytes],
                    )
            evidence = await self._store._run_stage_io(session.prepare)
            return BlobStagedWrite(
                store=self._store,
                session=session,
                owner_id=self._owner_id,
                staging_id=self._staging_id,
                storage_key=self._storage_key,
                evidence=evidence,
            )
        except BaseException:
            await self._store._run_stage_io(session.abort)
            raise

    def abort(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            session.abort()

    async def aabort(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            await self._store._run_stage_io(session.abort)

    def __enter__(self) -> BlobStagingSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.abort()

    async def __aenter__(self) -> BlobStagingSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aabort()


class ExplicitRootStreamingBlobStore:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        io_chunk_bytes: int = _DEFAULT_IO_CHUNK_BYTES,
        create_root: bool = False,
    ) -> None:
        if not isinstance(root, (str, os.PathLike)):
            raise SQLContractError("blob root must be a filesystem path")
        if not isinstance(create_root, bool):
            raise SQLContractError("create_root must be a boolean")
        supplied = Path(root)
        if not supplied.is_absolute():
            raise SQLContractError("blob root must be an explicit absolute path")
        if os.name == "nt":
            try:
                _windows_extended_path(supplied)
            except PlaneError as exc:
                raise SQLContractError(
                    "blob root must be a local drive-rooted absolute path"
                ) from exc
        provisioned: tuple[Path, ...] = ()
        stage_executor: ThreadPoolExecutor | None = None
        control_executor: ThreadPoolExecutor | None = None
        try:
            if create_root:
                provisioned = self._provision_root(supplied)
            self._validate_existing_directory(supplied, context="configured blob root")
            self._check_absolute_ancestry(supplied)
            self._root = (
                Path(os.path.abspath(os.fspath(supplied)))
                if os.name == "nt"
                else supplied.resolve(strict=True)
            )
            self._root_identity = _metadata_identity(
                os.lstat(
                    _windows_extended_path(self._root)
                    if os.name == "nt"
                    else self._root
                )
            )
            if self._root_identity is None:
                raise PlaneError(
                    "configured blob root has no stable filesystem identity",
                    code="blob_path_unsafe",
                )
            self._owner_locks = _OwnerLockTable()
            self._lifecycle_guard = threading.Lock()
            self._io_chunk_bytes = _positive_bound(
                io_chunk_bytes,
                name="io_chunk_bytes",
                maximum=_MAX_IO_CHUNK_BYTES,
            )
            stage_executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="astralplane-blob-stage",
            )
            control_executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="astralplane-blob-control",
            )
            self._stage_io_executor = stage_executor
            self._control_io_executor = control_executor
            self._pending_owner_acquisitions = 0
            self._closed = False
        except BaseException:
            if control_executor is not None:
                control_executor.shutdown(wait=True, cancel_futures=False)
            if stage_executor is not None:
                stage_executor.shutdown(wait=True, cancel_futures=False)
            if provisioned:
                try:
                    self._rollback_provisioned_directories(provisioned)
                except BaseException as cleanup_error:
                    raise PlaneError(
                        "failed to roll back configured blob root provisioning",
                        code="blob_cleanup_failed",
                    ) from cleanup_error
            raise

    async def _run_stage_io(
        self,
        function: Callable[..., Any],
        /,
        *args: object,
        cleanup_on_cancel: Callable[[Any], None] | None = None,
        **kwargs: object,
    ) -> Any:
        if self._closed:
            raise PlaneError("blob store is closed", code="blob_store_closed")
        return await _cancel_safe_in_executor(
            self._stage_io_executor,
            function,
            *args,
            cleanup_on_cancel=cleanup_on_cancel,
            **kwargs,
        )

    def close(self) -> None:
        with self._lifecycle_guard:
            if self._closed:
                return
            if self._pending_owner_acquisitions or self._owner_locks.has_entries():
                raise PlaneError(
                    "blob store still has active readers or staging capabilities",
                    code="blob_store_busy",
                )
            self._closed = True
        self._stage_io_executor.shutdown(wait=True, cancel_futures=False)
        self._control_io_executor.shutdown(wait=True, cancel_futures=False)

    def __enter__(self) -> ExplicitRootStreamingBlobStore:
        if self._closed:
            raise PlaneError("blob store is closed", code="blob_store_closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def root_identity_digest(self) -> str:
        with self._lifecycle_guard:
            if self._closed:
                raise PlaneError("blob store is closed", code="blob_store_closed")
            root = self._root
            identity = self._root_identity
        try:
            metadata = os.lstat(
                _windows_extended_path(root) if os.name == "nt" else root
            )
        except OSError as exc:
            raise PlaneError(
                "configured blob root no longer resolves as a directory",
                code="blob_path_unsafe",
            ) from exc
        if (
            _is_reparse(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or _metadata_identity(metadata) != identity
        ):
            raise PlaneError(
                "configured blob root identity changed",
                code="blob_path_unsafe",
            )
        payload = (
            b"astralplane.blob-root/v1\0"
            + os.fsencode(root)
            + b"\0"
            + f"{identity[0]}\0{identity[1]}".encode("ascii")
        )
        return hashlib.sha256(payload).hexdigest()

    def _begin_owner_acquisition(self) -> None:
        with self._lifecycle_guard:
            if self._closed:
                raise PlaneError("blob store is closed", code="blob_store_closed")
            self._pending_owner_acquisitions += 1

    def _finish_owner_acquisition(self) -> None:
        with self._lifecycle_guard:
            self._pending_owner_acquisitions -= 1

    def _acquire_owner_exclusion(self, owner: str) -> _OwnerLockToken:
        self._begin_owner_acquisition()
        try:
            local = self._owner_locks.acquire(owner)
            try:
                local.bind_cross_process(_CrossProcessOwnerLock(self, owner=owner))
            except BaseException:
                local.release()
                raise
            return local
        finally:
            self._finish_owner_acquisition()

    def _try_acquire_owner_exclusion_unadmitted(
        self,
        owner: str,
    ) -> _OwnerLockToken | None:
        local = self._owner_locks.try_acquire(owner)
        if local is None:
            return None
        try:
            local.bind_cross_process(
                _CrossProcessOwnerLock(self, owner=owner, blocking=False)
            )
        except _OwnerExclusionBusyError:
            local.release()
            return None
        except BaseException:
            local.release()
            raise
        return local

    def _try_acquire_owner_exclusion(self, owner: str) -> _OwnerLockToken | None:
        self._begin_owner_acquisition()
        try:
            return self._try_acquire_owner_exclusion_unadmitted(owner)
        finally:
            self._finish_owner_acquisition()

    async def _acquire_owner_exclusion_async(self, owner: str) -> _OwnerLockToken:
        self._begin_owner_acquisition()
        try:
            while True:
                token = await _cancel_safe_in_executor(
                    self._control_io_executor,
                    self._try_acquire_owner_exclusion_unadmitted,
                    owner,
                    cleanup_on_cancel=lambda value: (
                        None if value is None else value.release()
                    ),
                )
                if token is not None:
                    return token
                await asyncio.sleep(0.01)
        finally:
            self._finish_owner_acquisition()

    def reserve_materialization_staging(
        self,
        *,
        owner_id: str,
    ) -> BlobStagingReservation:
        owner = validate_blob_owner_id(owner_id)
        return BlobStagingReservation(
            self,
            owner=owner,
            token=self._acquire_owner_exclusion(owner),
        )

    async def areserve_materialization_staging(
        self,
        *,
        owner_id: str,
    ) -> BlobStagingReservation:
        owner = validate_blob_owner_id(owner_id)
        return BlobStagingReservation(
            self,
            owner=owner,
            token=await self._acquire_owner_exclusion_async(owner),
        )

    def _begin_staged_materialization(
        self,
        *,
        authority: _BlobPublishAuthority,
        reservation: BlobStagingReservation,
    ) -> BlobStagingSession:
        if not isinstance(authority, _BlobPublishAuthority):
            raise SQLContractError("authority must be typed blob staging evidence")
        owner = validate_blob_owner_id(authority.owner_id)
        storage_key = validate_blob_storage_key(authority.storage_key)
        _positive_bound(authority.max_bytes, name="max_bytes")
        lease_id = _normalized_staging_id(authority.lease_id)
        if not isinstance(reservation, BlobStagingReservation):
            raise SQLContractError("reservation must be a blob staging reservation")
        owner_lock = reservation.take(self, owner=owner)
        session = self._begin_authorized_staged_write(
            authority=authority,
            owner_lock=owner_lock,
        )
        return BlobStagingSession(
            store=self,
            session=session,
            owner_id=owner,
            staging_id=lease_id,
            storage_key=storage_key,
        )

    def _publish_staged_materialization(
        self,
        staged: BlobStagedWrite,
        *,
        authority: _BlobPublishAuthority,
    ) -> BlobWriteResult:
        if not isinstance(staged, BlobStagedWrite) or staged._store is not self:
            raise SQLContractError("staged write does not belong to this blob store")
        if not isinstance(authority, _BlobPublishAuthority):
            raise SQLContractError("authority must be typed blob publication evidence")
        owner = validate_blob_owner_id(authority.owner_id)
        storage_key = validate_blob_storage_key(authority.storage_key)
        maximum = _positive_bound(authority.max_bytes, name="max_bytes")
        lease_id = _normalized_staging_id(authority.lease_id)
        if (
            staged._owner_id != owner
            or staged._storage_key != storage_key
            or staged._staging_id != lease_id
        ):
            raise PlaneError(
                "blob staging identity does not match publication authority",
                code="blob_publish_fence_conflict",
            )
        if staged.evidence.size_bytes > maximum:
            raise BlobSizeLimitError("staged blob exceeds publication authority")
        return staged._session.publish()

    def open_reader(
        self,
        *,
        owner_id: str,
        key: str,
        max_bytes: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> BlobReadStream:
        owner = validate_blob_owner_id(owner_id)
        bound = _positive_bound(max_bytes, name="max_bytes")
        expected_size = _optional_size(expected_size_bytes, maximum=bound)
        expected_digest = _optional_digest(expected_sha256)
        storage_key = _normalized_key(key)
        owner_lock = self._acquire_owner_exclusion(owner)
        return self._open_reader_prelocked(
            owner=owner,
            storage_key=storage_key,
            bound=bound,
            expected_size=expected_size,
            expected_digest=expected_digest,
            owner_lock=owner_lock,
        )

    def _open_reader_prelocked(
        self,
        *,
        owner: str,
        storage_key: str,
        bound: int,
        expected_size: int | None,
        expected_digest: str | None,
        owner_lock: _OwnerLockToken,
    ) -> BlobReadStream:
        anchor: _DirectoryAnchor | None = None
        try:
            key_parts = tuple(storage_key.split("/"))
            try:
                anchor = _DirectoryAnchor(
                    self,
                    components=(owner, *key_parts[:-1]),
                    create=False,
                )
            except FileNotFoundError as exc:
                raise PlaneError("blob does not exist", code="blob_not_found") from exc
            target = anchor.path / key_parts[-1]
            self._check_chain(target.parent)
            anchor.assert_current()
            try:
                before = anchor.stat_entry(target.name)
            except FileNotFoundError as exc:
                raise PlaneError("blob does not exist", code="blob_not_found") from exc
            if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
                raise PlaneError(
                    "blob locator is not a safe regular file",
                    code="blob_path_unsafe",
                )
            if before.st_size > bound:
                raise BlobSizeLimitError("blob exceeds the declared read maximum")
            if expected_size is not None and before.st_size != expected_size:
                raise BlobIntegrityError("blob size does not match expected_size_bytes")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = anchor.open_file(target.name, flags=flags)
            try:
                after = os.fstat(descriptor)
                if _is_reparse(after) or not stat.S_ISREG(after.st_mode):
                    raise PlaneError(
                        "blob locator is not a safe regular file",
                        code="blob_path_unsafe",
                    )
                if after.st_size > bound or after.st_size != before.st_size:
                    raise BlobIntegrityError("blob changed while it was being opened")
                before_identity = _metadata_identity(before)
                if before_identity is not None and before_identity != _metadata_identity(after):
                    raise BlobIntegrityError("blob identity changed while it was being opened")
                stream = os.fdopen(descriptor, "rb", closefd=True)
            except BaseException:
                os.close(descriptor)
                raise
        except BaseException:
            if anchor is not None:
                anchor.close()
            owner_lock.release()
            raise
        try:
            anchor.close()
        except BaseException:
            stream.close()
            owner_lock.release()
            raise
        return BlobReadStream(
            stream,
            size_bytes=after.st_size,
            io_chunk_bytes=self._io_chunk_bytes,
            expected_sha256=expected_digest,
            owner_lock=owner_lock,
        )

    def open_parser_lease(
        self,
        *,
        owner_id: str,
        key: str,
        max_bytes: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> BlobParserLease:
        owner = validate_blob_owner_id(owner_id)
        bound = _positive_bound(max_bytes, name="max_bytes")
        expected_size = _optional_size(expected_size_bytes, maximum=bound)
        expected_digest = _optional_digest(expected_sha256)
        owner_lock = self._acquire_owner_exclusion(owner)
        anchor: _DirectoryAnchor | None = None
        try:
            storage_key = _normalized_key(key)
            key_parts = tuple(storage_key.split("/"))
            try:
                anchor = _DirectoryAnchor(
                    self,
                    components=(owner, *key_parts[:-1]),
                    create=False,
                )
            except FileNotFoundError as exc:
                raise PlaneError("blob does not exist", code="blob_not_found") from exc
            target = anchor.path / key_parts[-1]
            self._check_chain(target.parent)
            anchor.assert_current()
            try:
                before = anchor.stat_entry(target.name)
            except FileNotFoundError as exc:
                raise PlaneError("blob does not exist", code="blob_not_found") from exc
            if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
                raise PlaneError(
                    "blob locator is not a safe regular file",
                    code="blob_path_unsafe",
                )
            if before.st_size > bound:
                raise BlobSizeLimitError("blob exceeds the parser lease maximum")
            if expected_size is not None and before.st_size != expected_size:
                raise BlobIntegrityError("blob size does not match expected_size_bytes")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = anchor.open_file(
                target.name,
                flags=flags,
                deny_write_sharing=True,
            )
            stream: BinaryIO | None = None
            try:
                stream = os.fdopen(descriptor, "rb", closefd=True)
                self._validate_lease_target(
                    target,
                    stream,
                    initial=before,
                    max_bytes=bound,
                    anchor=anchor,
                )
                self._verify_descriptor_digest(
                    stream,
                    size_bytes=before.st_size,
                    expected_sha256=expected_digest,
                )
                capability_path = self._parser_capability_path(
                    target,
                    descriptor=stream.fileno(),
                )
            except BaseException:
                if stream is None:
                    os.close(descriptor)
                else:
                    stream.close()
                raise
        except BaseException:
            if anchor is not None:
                anchor.close()
            owner_lock.release()
            raise
        return BlobParserLease(
            self,
            target=target,
            capability_path=capability_path,
            max_bytes=bound,
            stream=stream,
            initial_metadata=before,
            anchor=anchor,
            owner_lock=owner_lock,
        )

    def iter_chunks(
        self,
        *,
        owner_id: str,
        key: str,
        max_bytes: int,
        chunk_size: int | None = None,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> Iterator[bytes]:
        with self.open_reader(
            owner_id=owner_id,
            key=key,
            max_bytes=max_bytes,
            expected_size_bytes=expected_size_bytes,
            expected_sha256=expected_sha256,
        ) as reader:
            yield from reader.iter_chunks(chunk_size=chunk_size)

    async def aiter_chunks(
        self,
        *,
        owner_id: str,
        key: str,
        max_bytes: int,
        chunk_size: int | None = None,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> AsyncIterator[bytes]:
        size = self._io_chunk_bytes if chunk_size is None else _positive_bound(
            chunk_size,
            name="chunk_size",
            maximum=self._io_chunk_bytes,
        )
        owner = validate_blob_owner_id(owner_id)
        bound = _positive_bound(max_bytes, name="max_bytes")
        expected_size = _optional_size(expected_size_bytes, maximum=bound)
        expected_digest = _optional_digest(expected_sha256)
        storage_key = _normalized_key(key)
        owner_lock = await self._acquire_owner_exclusion_async(owner)
        try:
            reader = await _cancel_safe_in_executor(
                self._control_io_executor,
                self._open_reader_prelocked,
                owner=owner,
                storage_key=storage_key,
                bound=bound,
                expected_size=expected_size,
                expected_digest=expected_digest,
                owner_lock=owner_lock,
                cleanup_on_cancel=lambda value: value._abandon_unverified(),
            )
        except BaseException:
            owner_lock.release()
            raise
        completed = False
        try:
            while chunk := await _cancel_safe_in_executor(
                self._control_io_executor,
                reader.read,
                size,
            ):
                yield chunk
            completed = True
        finally:
            cleanup = reader.close if completed else reader._abandon_unverified
            await _cancel_safe_in_executor(self._control_io_executor, cleanup)

    def _delete_for_purge(self, authority: _BlobPurgeAuthority) -> BlobDeleteResult:
        if not isinstance(authority, _BlobPurgeAuthority):
            raise SQLContractError("physical blob deletion requires purge authority")
        owner_lock = self._acquire_owner_exclusion(authority.owner_id)
        try:
            if authority.target_scope == "attachment_prefix":
                components = (
                    authority.owner_id,
                    *authority.storage_key.split("/"),
                )
                return self._delete_components(
                    authority,
                    components=components,
                    prune_parent=True,
                )
            return self._delete_components(
                authority,
                components=(authority.owner_id,),
                prune_parent=False,
            )
        finally:
            owner_lock.release()

    def is_absent(self, *, owner_id: str, key: str) -> bool:
        owner = validate_blob_owner_id(owner_id)
        storage_key = _normalized_key(key)
        key_parts = tuple(storage_key.split("/"))
        owner_lock = self._acquire_owner_exclusion(owner)
        try:
            try:
                with _DirectoryAnchor(
                    self,
                    components=(owner, *key_parts[:-1]),
                    create=False,
                ) as anchor:
                    anchor._assert_exact_file_case(key_parts[-1], allow_missing=True)
                    try:
                        metadata = anchor.stat_entry(key_parts[-1])
                    except FileNotFoundError:
                        return True
                    if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                        raise PlaneError(
                            "blob locator has an unsafe object type",
                            code="blob_path_unsafe",
                        )
                    return False
            except FileNotFoundError:
                return True
        finally:
            owner_lock.release()

    def is_prefix_absent(self, *, owner_id: str, prefix: str) -> bool:
        owner = validate_blob_owner_id(owner_id)
        normalized = _normalized_key(prefix, name="prefix")
        owner_lock = self._acquire_owner_exclusion(owner)
        try:
            return self._directory_components_absent(
                (owner, *normalized.split("/"))
            )
        finally:
            owner_lock.release()

    def is_owner_absent(self, *, owner_id: str) -> bool:
        owner = validate_blob_owner_id(owner_id)
        owner_lock = self._acquire_owner_exclusion(owner)
        try:
            return self._directory_components_absent((owner,))
        finally:
            owner_lock.release()

    def _begin_authorized_staged_write(
        self,
        *,
        authority: _BlobPublishAuthority,
        owner_lock: _OwnerLockToken,
    ) -> _AtomicWriteSession:
        if not isinstance(authority, _BlobPublishAuthority):
            raise SQLContractError("authority must be typed blob staging evidence")
        owner = validate_blob_owner_id(authority.owner_id)
        storage_key = validate_blob_storage_key(authority.storage_key)
        bound = _positive_bound(authority.max_bytes, name="max_bytes")
        _normalized_staging_id(authority.lease_id)
        if (
            not isinstance(owner_lock, _OwnerLockToken)
            or owner_lock._table is not self._owner_locks
            or owner_lock._owner != owner.casefold()
            or owner_lock._released
        ):
            raise SQLContractError("owner_lock must be the active owner staging reservation")
        anchor: _DirectoryAnchor | None = None
        marker_name: str | None = None
        marker_created = False
        marker_descriptor: int | None = None
        marker_stream: BinaryIO | None = None
        temporary: Path | None = None
        descriptor: int | None = None
        stream: BinaryIO | None = None
        try:
            key_parts = tuple(storage_key.split("/"))
            anchor = _DirectoryAnchor(
                self,
                components=(owner, *key_parts[:-1]),
                create=True,
            )
            target = anchor.path / key_parts[-1]
            self._check_chain(target.parent)
            anchor.assert_current()
            self._verify_publish_target(target, anchor=anchor)
            staging_digest = hashlib.sha256(
                f"{owner}\0{storage_key}".encode()
            ).hexdigest()
            marker_name = f".astralplane-stage-{staging_digest}.lock"
            try:
                marker_metadata = anchor.stat_entry(marker_name)
            except FileNotFoundError:
                marker_metadata = None
            if marker_metadata is not None:
                if _is_reparse(marker_metadata) or not stat.S_ISREG(
                    marker_metadata.st_mode
                ):
                    raise PlaneError(
                        "blob staging sentinel is unsafe",
                        code="blob_path_unsafe",
                    )
                raise PlaneError(
                    "blob staging lease is already active or finalized",
                    code="blob_staging_conflict",
                )
            marker_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                marker_descriptor = anchor.open_file(
                    marker_name,
                    flags=marker_flags,
                    mode=0o600,
                    deny_write_sharing=True,
                )
                marker_created = True
            except FileExistsError as exc:
                raise PlaneError(
                    "blob staging lease is already active or finalized",
                    code="blob_staging_conflict",
                ) from exc
            marker_stream = os.fdopen(marker_descriptor, "wb", closefd=True)
            marker_descriptor = None
            marker_stream.flush()
            os.fsync(marker_stream.fileno())
            self._fsync_anchor(anchor)
            temporary = target.parent / f".astralplane-stage-{staging_digest}.tmp"
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = anchor.open_file(
                temporary.name,
                flags=flags,
                mode=0o600,
                temporary=True,
            )
            stream = os.fdopen(
                descriptor,
                "w+b",
                closefd=True,
            )
            descriptor = None
            session = _AtomicWriteSession(
                self,
                owner=owner,
                storage_key=storage_key,
                target=target,
                temporary=temporary,
                stream=stream,
                max_bytes=bound,
                expected_size_bytes=None,
                expected_sha256=None,
                marker_name=marker_name,
                marker_stream=marker_stream,
                anchor=anchor,
                owner_lock=owner_lock,
            )
        except BaseException as original:
            cleanup_errors: list[BaseException] = []

            def cleanup(action: Callable[[], object]) -> None:
                try:
                    action()
                except FileNotFoundError:
                    pass
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)

            if stream is not None:
                cleanup(stream.close)
            elif descriptor is not None:
                cleanup(lambda: os.close(descriptor))
            if marker_stream is not None:
                cleanup(marker_stream.close)
            elif marker_descriptor is not None:
                cleanup(lambda: os.close(marker_descriptor))
            if anchor is not None and temporary is not None:
                cleanup(lambda: anchor.unlink(temporary.name))
            if anchor is not None and marker_name is not None and marker_created:
                cleanup(lambda: anchor.unlink(marker_name))
            if anchor is not None:
                cleanup(anchor.close)
            cleanup(owner_lock.release)
            if cleanup_errors:
                raise PlaneError(
                    "failed to clean an unpublished blob construction",
                    code="blob_cleanup_failed",
                ) from original
            raise
        return session

    def _check_chain(self, directory: Path) -> None:
        self._validate_existing_directory(self._root, context="configured blob root")
        self._check_absolute_ancestry(self._root)
        relative = self._relative(directory)
        current = self._root
        for part in relative.parts:
            current /= part
            try:
                metadata = os.lstat(
                    _windows_extended_path(current) if os.name == "nt" else current
                )
            except FileNotFoundError:
                return
            if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise PlaneError(
                    "blob path crosses a link, reparse point, or non-directory",
                    code="blob_path_unsafe",
                )

    def _relative(self, path: Path) -> Path:
        try:
            return path.relative_to(self._root)
        except ValueError as exc:
            raise PlaneError(
                "blob path escaped its configured root",
                code="blob_path_unsafe",
            ) from exc

    def _verify_publish_target(
        self,
        target: Path,
        *,
        anchor: _DirectoryAnchor | None = None,
    ) -> None:
        if anchor is not None:
            anchor._assert_exact_file_case(target.name, allow_missing=True)
        try:
            metadata = (
                os.lstat(
                    _windows_extended_path(target) if os.name == "nt" else target
                )
                if anchor is None
                else anchor.stat_entry(target.name)
            )
        except FileNotFoundError:
            return
        if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise PlaneError("blob locator is not a safe regular file", code="blob_path_unsafe")

    def _validate_lease_target(
        self,
        target: Path,
        stream: BinaryIO,
        *,
        initial: os.stat_result,
        max_bytes: int,
        anchor: _DirectoryAnchor,
    ) -> None:
        self._validate_existing_directory(self._root, context="configured blob root")
        self._check_absolute_ancestry(self._root)
        self._check_chain(target.parent)
        anchor.assert_current()
        try:
            current = anchor.stat_entry(target.name)
        except FileNotFoundError as exc:
            raise BlobIntegrityError("blob identity changed during parser lease") from exc
        if _is_reparse(current) or not stat.S_ISREG(current.st_mode):
            raise PlaneError("blob locator is not a safe regular file", code="blob_path_unsafe")
        descriptor_metadata = os.fstat(stream.fileno())
        if _is_reparse(descriptor_metadata) or not stat.S_ISREG(descriptor_metadata.st_mode):
            raise PlaneError("parser lease descriptor is unsafe", code="blob_path_unsafe")
        identities = (
            _metadata_identity(initial),
            _metadata_identity(current),
            _metadata_identity(descriptor_metadata),
        )
        known_identities = tuple(identity for identity in identities if identity is not None)
        if known_identities and len(set(known_identities)) != 1:
            raise BlobIntegrityError("blob identity changed during parser lease")
        sizes = (initial.st_size, current.st_size, descriptor_metadata.st_size)
        if any(size > max_bytes for size in sizes):
            raise BlobSizeLimitError("blob exceeds the parser lease maximum")
        if len(set(sizes)) != 1:
            raise BlobIntegrityError("blob size changed during parser lease")
        for field_name in ("st_mtime_ns", "st_ctime_ns"):
            initial_value = getattr(initial, field_name, None)
            current_value = getattr(current, field_name, None)
            if initial_value is not None and current_value != initial_value:
                raise BlobIntegrityError("blob metadata changed during parser lease")

    def _verify_descriptor_digest(
        self,
        stream: BinaryIO,
        *,
        size_bytes: int,
        expected_sha256: str | None,
    ) -> None:
        if expected_sha256 is None:
            return
        stream.seek(0)
        hasher = hashlib.sha256()
        total = 0
        while total < size_bytes:
            chunk = stream.read(min(self._io_chunk_bytes, size_bytes - total))
            if not chunk:
                raise BlobIntegrityError("blob changed during parser integrity validation")
            total += len(chunk)
            hasher.update(chunk)
        if stream.read(1):
            raise BlobIntegrityError("blob changed during parser integrity validation")
        if hasher.hexdigest() != expected_sha256:
            raise BlobIntegrityError("blob digest does not match expected_sha256")
        stream.seek(0)

    @staticmethod
    def _parser_capability_path(target: Path, *, descriptor: int) -> Path:
        if os.name == "nt":
            return (
                Path(_windows_extended_path(target))
                if len(os.fspath(target)) >= 260
                else target
            )
        for directory in (Path("/proc/self/fd"), Path("/dev/fd")):
            candidate = directory / str(descriptor)
            if candidate.exists():
                return candidate
        raise PlaneError(
            "this platform cannot expose a descriptor-bound parser capability",
            code="blob_parser_lease_unsupported",
        )

    def _directory_components_absent(self, components: tuple[str, ...]) -> bool:
        try:
            with _DirectoryAnchor(self, components=components, create=False):
                return False
        except FileNotFoundError:
            return True

    def _delete_components(
        self,
        authority: _BlobPurgeAuthority,
        *,
        components: tuple[str, ...],
        prune_parent: bool,
    ) -> BlobDeleteResult:
        if not isinstance(authority, _BlobPurgeAuthority):
            raise SQLContractError("physical blob deletion requires purge authority")
        expected = (
            (authority.owner_id, *authority.storage_key.split("/"))
            if authority.target_scope == "attachment_prefix"
            else (authority.owner_id,)
        )
        if components != expected:
            raise SQLContractError("purge authority does not match the deletion target")
        if not components:
            raise PlaneError("configured blob root cannot be deleted", code="blob_path_unsafe")
        parent: _DirectoryAnchor | None = None
        target: _DirectoryAnchor | None = None
        try:
            try:
                parent = _DirectoryAnchor(
                    self,
                    components=components[:-1],
                    create=False,
                )
                target = _DirectoryAnchor(
                    self,
                    components=components,
                    create=False,
                )
            except FileNotFoundError:
                return BlobDeleteResult(0, 0, True)
            expected = parent.stat_entry(components[-1])
            if (
                _is_reparse(expected)
                or not stat.S_ISDIR(expected.st_mode)
                or _metadata_identity(expected) != target.identity
            ):
                raise PlaneError("blob prefix is not a safe directory", code="blob_path_unsafe")
            self._validate_anchor_tree(target, depth=0)
            deleted_files, nested_directories = self._delete_anchor_contents(
                authority,
                target,
                depth=0,
            )
            target.close()
            target = None
            current = parent.stat_entry(components[-1])
            if _metadata_identity(current) != _metadata_identity(expected):
                raise PlaneError("blob tree changed during deletion", code="blob_path_unsafe")
            parent.rmdir(components[-1])
            self._fsync_anchor(parent)
            try:
                parent.stat_entry(components[-1])
            except FileNotFoundError:
                pass
            else:
                raise PlaneError(
                    "blob prefix remained after deletion",
                    code="blob_delete_incomplete",
                )
            parent.close()
            parent = None
            if prune_parent:
                self._prune_empty_components(components[:-1])
            return BlobDeleteResult(
                deleted_files,
                nested_directories + 1,
                True,
            )
        except PlaneError:
            raise
        except OSError as exc:
            raise PlaneError("blob prefix deletion failed", code="blob_delete_failed") from exc
        finally:
            if target is not None:
                target.close()
            if parent is not None:
                parent.close()

    def _delete_anchor_contents(
        self,
        authority: _BlobPurgeAuthority,
        anchor: _DirectoryAnchor,
        *,
        depth: int,
    ) -> tuple[int, int]:
        if not isinstance(authority, _BlobPurgeAuthority):
            raise SQLContractError("physical blob deletion requires purge authority")
        if depth > _MAX_KEY_COMPONENTS + 1:
            raise PlaneError("blob tree exceeds its bounded depth", code="blob_path_unsafe")
        deleted_files = 0
        deleted_directories = 0
        with anchor.scandir() as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if _is_reparse(metadata):
                    raise PlaneError(
                        "blob tree contains a link or reparse point",
                        code="blob_path_unsafe",
                    )
                if stat.S_ISREG(metadata.st_mode):
                    anchor.unlink(entry.name)
                    deleted_files += 1
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    raise PlaneError(
                        "blob tree contains an unsupported object",
                        code="blob_path_unsafe",
                    )
                child = _DirectoryAnchor(
                    self,
                    components=(*anchor.components, entry.name),
                    create=False,
                )
                child_identity = child.identity
                try:
                    observed_identity = _metadata_identity(metadata)
                    if (
                        observed_identity is not None
                        and child_identity != observed_identity
                    ):
                        raise PlaneError(
                            "blob tree changed during deletion",
                            code="blob_path_unsafe",
                        )
                    child_files, child_directories = self._delete_anchor_contents(
                        authority,
                        child,
                        depth=depth + 1,
                    )
                finally:
                    child.close()
                current = anchor.stat_entry(entry.name)
                if _metadata_identity(current) != child_identity:
                    raise PlaneError(
                        "blob tree changed during deletion",
                        code="blob_path_unsafe",
                    )
                anchor.rmdir(entry.name)
                deleted_files += child_files
                deleted_directories += child_directories + 1
        self._fsync_anchor(anchor)
        return deleted_files, deleted_directories

    def _validate_anchor_tree(
        self,
        anchor: _DirectoryAnchor,
        *,
        depth: int,
    ) -> None:
        if depth > _MAX_KEY_COMPONENTS + 1:
            raise PlaneError("blob tree exceeds its bounded depth", code="blob_path_unsafe")
        with anchor.scandir() as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if _is_reparse(metadata):
                    raise PlaneError(
                        "blob tree contains a link or reparse point",
                        code="blob_path_unsafe",
                    )
                if stat.S_ISREG(metadata.st_mode):
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    raise PlaneError(
                        "blob tree contains an unsupported object",
                        code="blob_path_unsafe",
                    )
                child = _DirectoryAnchor(
                    self,
                    components=(*anchor.components, entry.name),
                    create=False,
                )
                try:
                    observed_identity = _metadata_identity(metadata)
                    if (
                        observed_identity is not None
                        and child.identity != observed_identity
                    ):
                        raise PlaneError(
                            "blob tree changed during validation",
                            code="blob_path_unsafe",
                        )
                    self._validate_anchor_tree(child, depth=depth + 1)
                finally:
                    child.close()

    def _prune_empty_parents(self, directory: Path) -> None:
        relative = self._relative(directory)
        self._prune_empty_components(tuple(relative.parts))

    def _prune_empty_components(self, components: tuple[str, ...]) -> None:
        current = components
        while current:
            parent: _DirectoryAnchor | None = None
            target: _DirectoryAnchor | None = None
            try:
                parent = _DirectoryAnchor(
                    self,
                    components=current[:-1],
                    create=False,
                )
                target = _DirectoryAnchor(
                    self,
                    components=current,
                    create=False,
                )
            except FileNotFoundError:
                if target is not None:
                    target.close()
                if parent is not None:
                    parent.close()
                current = current[:-1]
                continue
            identity = target.identity
            target.close()
            target = None
            try:
                metadata = parent.stat_entry(current[-1])
                if _metadata_identity(metadata) != identity:
                    raise PlaneError(
                        "blob directory changed during cleanup",
                        code="blob_path_unsafe",
                    )
                parent.rmdir(current[-1])
                self._fsync_anchor(parent)
            except OSError as exc:
                if getattr(exc, "winerror", None) in {145, 183} or exc.errno in {
                    errno.EEXIST,
                    errno.ENOTEMPTY,
                }:
                    return
                raise PlaneError(
                    "blob directory cleanup failed",
                    code="blob_cleanup_failed",
                ) from exc
            finally:
                if target is not None:
                    target.close()
                if parent is not None:
                    parent.close()
            current = current[:-1]

    @staticmethod
    def _validate_existing_directory(path: Path, *, context: str) -> None:
        try:
            metadata = os.lstat(
                _windows_extended_path(path) if os.name == "nt" else path
            )
        except OSError as exc:
            raise PlaneError(f"{context} is unavailable", code="blob_root_unavailable") from exc
        if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise SQLContractError(f"{context} must be a real directory")

    @classmethod
    def _provision_root(cls, path: Path) -> tuple[Path, ...]:
        missing: list[Path] = []
        current = path
        while True:
            try:
                metadata = os.lstat(
                    _windows_extended_path(current) if os.name == "nt" else current
                )
            except FileNotFoundError as exc:
                missing.append(current)
                if current == current.parent:
                    raise PlaneError(
                        "configured blob root has no available ancestor",
                        code="blob_root_unavailable",
                    ) from exc
                current = current.parent
                continue
            except NotADirectoryError as exc:
                raise SQLContractError(
                    "blob root can only be created below a real directory ancestry"
                ) from exc
            if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise SQLContractError(
                    "blob root can only be created below a real directory ancestry"
                )
            break
        cls._check_absolute_ancestry(current)
        created: list[Path] = []
        try:
            for directory in reversed(missing):
                try:
                    if os.name == "nt":
                        os.mkdir(_windows_extended_path(directory), mode=0o700)
                    else:
                        directory.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                else:
                    created.append(directory)
                cls._validate_existing_directory(
                    directory,
                    context="configured blob root ancestry",
                )
                cls._fsync_directory(directory.parent)
        except BaseException as original:
            try:
                cls._rollback_provisioned_directories(tuple(created))
            except BaseException as cleanup_error:
                raise PlaneError(
                    "failed to roll back configured blob root provisioning",
                    code="blob_cleanup_failed",
                ) from cleanup_error
            raise original
        return tuple(created)

    @classmethod
    def _rollback_provisioned_directories(
        cls,
        directories: tuple[Path, ...],
    ) -> None:
        for directory in reversed(directories):
            cls._validate_existing_directory(
                directory,
                context="provisioned blob root ancestry",
            )
            if os.name == "nt":
                os.rmdir(_windows_extended_path(directory))
            else:
                directory.rmdir()
            cls._fsync_directory(directory.parent)

    @staticmethod
    def _check_absolute_ancestry(path: Path) -> None:
        current = path
        chain: list[Path] = []
        while current != current.parent:
            chain.append(current)
            current = current.parent
        for candidate in reversed(chain):
            metadata = os.lstat(
                _windows_extended_path(candidate) if os.name == "nt" else candidate
            )
            if _is_reparse(metadata):
                raise SQLContractError(
                    "blob root ancestry must not contain links or reparse points"
                )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
            return
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_anchor(anchor: _DirectoryAnchor) -> None:
        if os.name != "nt":
            os.fsync(anchor.descriptor)


__all__ = (
    "BlobDeleteResult",
    "BlobIntegrityError",
    "BlobParserLease",
    "BlobParserPath",
    "BlobReadStream",
    "BlobSizeLimitError",
    "BlobStagedWrite",
    "BlobWriteResult",
    "ExplicitRootStreamingBlobStore",
    "StreamingBlobStore",
    "validate_blob_owner_id",
    "validate_blob_storage_key",
)
