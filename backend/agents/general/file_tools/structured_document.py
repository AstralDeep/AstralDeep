"""Runs the opt-in structured PDF/DOCX adapter in a bounded host process and validates its
result before it reaches read_document.py. The baseline reader remains the default; this
module fails closed when its host-selected profile, worker, or containment is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from orchestrator.sandbox import SandboxLimits, make_preexec, sandbox_env
from shared.process_supervision import (
    ProcessOwner,
    ProcessSupervisor,
    TerminationReason,
)

from .structured_worker import _validate_profile

WORKER_PATH = Path(__file__).with_name("structured_worker.py")
_PROFILE_ENV = "ASTRAL_STRUCTURED_DOCUMENT_PROFILE"
_DEFAULT_PROFILE = (
    Path(__file__).resolve().parents[4]
    / "config"
    / "documents"
    / "structured-reader-profile.json"
)
_STATUSES = frozenset(
    {"complete", "partial", "truncated", "timeout", "cancelled", "failed"}
)
_NON_ROOT_UID = 65534
_NON_ROOT_GID = 65534
_SLOT_LOCK = threading.Lock()
_SLOTS: dict[int, threading.BoundedSemaphore] = {}


def _worker_slot(limit: int) -> threading.BoundedSemaphore:
    with _SLOT_LOCK:
        return _SLOTS.setdefault(limit, threading.BoundedSemaphore(limit))


def _profile_path() -> Path:
    configured = os.getenv(_PROFILE_ENV, "").strip()
    return Path(configured).expanduser() if configured else _DEFAULT_PROFILE


def _read_regular(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise OSError("profile path is not a regular file")
    stat = path.stat()
    if stat.st_size < 1 or stat.st_size > maximum:
        raise OSError("profile size is outside the worker boundary")
    return path.read_bytes()


def _profile() -> tuple[dict[str, Any], bytes]:
    raw = _read_regular(_profile_path(), 128 * 1024)
    try:
        value = json.loads(raw)
        profile = _validate_profile(value)
    except Exception:
        raise ValueError("invalid structured document profile") from None
    if profile.get("platform", {}).get("os") != "linux":
        raise ValueError("structured document profile is not Linux")
    return profile, raw


def _worker_bytes() -> bytes:
    raw = _read_regular(WORKER_PATH, 1024 * 1024)
    return raw


def _write_file(path: Path, payload: bytes, mode: int) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    finally:
        os.close(descriptor)


def _read_bounded(path: Path, maximum: int) -> bytes:
    if path.is_symlink():
        raise OSError("worker output is a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        stat = os.fstat(descriptor)
        if stat.st_size < 1 or stat.st_size > maximum:
            raise OSError("worker output is outside the result boundary")
        chunks = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if not raw or len(raw) > maximum:
            raise OSError("worker output is outside the result boundary")
        return raw
    finally:
        os.close(descriptor)


@contextmanager
def _scratch(worker: bytes, profile_raw: bytes) -> Iterator[tuple[Path, Path]]:
    root = Path(tempfile.mkdtemp(prefix="astral-document-worker-"))
    runtime = root / "runtime"
    work = root / "work"
    runtime.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    try:
        worker_path = runtime / "worker.py"
        profile_path = runtime / "profile.json"
        _write_file(worker_path, worker, 0o600)
        _write_file(profile_path, profile_raw, 0o600)
        worker_path.chmod(0o400)
        profile_path.chmod(0o400)
        runtime.chmod(0o500)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            root.chmod(0o711)
            runtime.chmod(0o555)
            work.chmod(0o777)
        yield runtime, work
    finally:
        try:
            runtime.chmod(0o700)
        except OSError:
            pass
        shutil.rmtree(root, ignore_errors=True)


def _drop_privileges() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(_NON_ROOT_GID)
    os.setuid(_NON_ROOT_UID)


def _preexec(profile: dict[str, Any]):
    limits = profile["limits"]
    resource_limits = SandboxLimits(
        address_space_mb=limits["memory_mb"],
        file_size_mb=limits["file_size_mb"],
        open_files=limits["open_files"],
        processes=limits["processes"],
        cpu_seconds=limits["cpu_seconds"],
    )
    resource_preexec = make_preexec(resource_limits)
    if os.name != "posix":
        return None

    def apply() -> None:
        if resource_preexec is not None:
            resource_preexec()
        _drop_privileges()

    return apply


def _worker_env(work: Path) -> dict[str, str]:
    return sandbox_env(
        {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        str(work),
    )


def _terminate(
    child: Any, reason: TerminationReason, status: str
) -> tuple[dict[str, Any], bool]:
    snapshot = child.terminate(reason=reason)
    cleanup_ok = bool(
        snapshot.process_tree_terminated
        and snapshot.readers_joined
        and snapshot.pipes_closed
        and snapshot.cleanup_error is None
    )
    result = {
        "status": status,
        "complete": False,
        "truncated": False,
        "text": "",
        "blocks": [],
        "tables": [],
        "pages": [],
        "warnings": [],
        "vision_required": False,
        "error": {
            "code": "cleanup_uncertain" if not cleanup_ok else status,
        },
    }
    return result, cleanup_ok


def _wait_for_worker(
    child: Any,
    deadline: float,
    cancel_event: Any,
) -> tuple[dict[str, Any] | None, bool]:
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return _terminate(child, TerminationReason.CANCEL, "cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _terminate(child, TerminationReason.FAILURE, "timeout")
        try:
            child.wait(timeout=min(0.1, remaining))
            return None, True
        except subprocess.TimeoutExpired:
            continue


def _validate_result(
    result: Any,
    *,
    payload: bytes,
    extension: str,
    profile: dict[str, Any],
    profile_digest: str,
    worker_digest: str,
    max_chars: int,
) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("status") not in _STATUSES:
        raise ValueError("worker result has no valid status")
    if result.get("status") == "failed":
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, str) or not code:
            raise ValueError("worker failure has no code")
        return result
    expected_digest = hashlib.sha256(payload).hexdigest()
    source = result.get("source")
    parser = result.get("parser")
    if (
        not isinstance(source, dict)
        or source.get("sha256") != expected_digest
        or source.get("bytes") != len(payload)
        or source.get("extension") != extension
        or not isinstance(parser, dict)
        or parser.get("profile_id") != profile["profile_id"]
        or parser.get("profile_sha256") != profile_digest
        or parser.get("adapter") != profile["adapter"]["id"]
        or parser.get("adapter_version") != profile["adapter"]["version"]
        or parser.get("worker_sha256") != worker_digest
    ):
        raise ValueError("worker result identity does not match its source")
    text = result.get("text")
    if not isinstance(text, str) or len(text) > max_chars:
        raise ValueError("worker text is outside the output boundary")
    limits = profile["limits"]
    for key, limit in (
        ("blocks", limits["blocks"]),
        ("tables", limits["table_rows"]),
        ("pages", limits["pdf_pages"]),
        ("warnings", limits["warnings"]),
    ):
        value = result.get(key)
        if not isinstance(value, list) or len(value) > limit:
            raise ValueError("worker result exceeds its collection boundary")
    if not isinstance(result.get("truncated"), bool) or not isinstance(
        result.get("complete"), bool
    ):
        raise ValueError("worker result has invalid completion flags")
    if result["status"] == "complete" and (
        not result["complete"] or result["truncated"]
    ):
        raise ValueError("complete result carries incomplete flags")
    if result["status"] in ("partial", "truncated") and result["complete"]:
        raise ValueError("incomplete result claims completion")
    return result


def parse_structured_document(
    payload: bytes,
    extension: str,
    page_range: str | None,
    max_chars: int,
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    extension = extension.lower()
    if extension not in ("pdf", "docx"):
        return {
            "error": {
                "code": "unsupported",
                "message": f"structured reader does not support .{extension}",
            }
        }
    try:
        profile, profile_raw = _profile()
        worker = _worker_bytes()
    except (OSError, ValueError):
        return {
            "error": {
                "code": "structured_parser_unavailable",
                "message": "The structured document worker profile or assets are unavailable.",
            }
        }
    limits = profile["limits"]
    if len(payload) > limits["input_bytes"]:
        return {
            "status": "failed",
            "complete": False,
            "truncated": False,
            "text": "",
            "blocks": [],
            "tables": [],
            "pages": [],
            "warnings": [],
            "vision_required": False,
            "error": {
                "code": "structured_input_too_large",
                "message": "The document exceeds the structured reader input limit.",
            },
        }
    try:
        effective_max_chars = min(max(1, int(max_chars)), limits["max_chars"])
    except (TypeError, ValueError):
        return {
            "error": {
                "code": "invalid_max_chars",
                "message": "max_chars must be a positive integer.",
            }
        }
    slot = _worker_slot(limits["concurrency"])
    if not slot.acquire(blocking=False):
        return {
            "status": "failed",
            "complete": False,
            "truncated": False,
            "text": "",
            "blocks": [],
            "tables": [],
            "pages": [],
            "warnings": [],
            "vision_required": False,
            "error": {
                "code": "structured_parser_busy",
                "message": "The structured document worker is at its concurrency limit.",
            },
        }
    try:
        return _execute_worker(
            payload=payload,
            extension=extension,
            page_range=page_range,
            effective_max_chars=effective_max_chars,
            profile=profile,
            profile_raw=profile_raw,
            worker=worker,
            cancel_event=cancel_event,
            limits=limits,
        )
    finally:
        slot.release()


def _execute_worker(
    *,
    payload: bytes,
    extension: str,
    page_range: str | None,
    effective_max_chars: int,
    profile: dict[str, Any],
    profile_raw: bytes,
    worker: bytes,
    cancel_event: threading.Event | None,
    limits: dict[str, Any],
) -> dict[str, Any]:
    profile_digest = hashlib.sha256(profile_raw).hexdigest()
    worker_digest = hashlib.sha256(worker).hexdigest()
    request = {
        "version": 1,
        "extension": extension,
        "page_range": page_range,
        "max_chars": effective_max_chars,
        "input": "input.bin",
        "output": "output.json",
        "profile": "profile.json",
        "profile_sha256": profile_digest,
    }
    supervisor = ProcessSupervisor()
    with _scratch(worker, profile_raw) as (runtime, work):
        input_path = work / "input.bin"
        request_path = work / "request.json"
        output_path = work / "output.json"
        input_mode = (
            0o644 if hasattr(os, "geteuid") and os.geteuid() == 0 else 0o600
        )
        _write_file(input_path, payload, input_mode)
        _write_file(
            request_path,
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            input_mode,
        )
        process_id = uuid.uuid4()
        try:
            child = supervisor.spawn(
                process_id=process_id,
                owner=ProcessOwner("structured_document", extension),
                argv=(
                    sys.executable,
                    "-I",
                    "-B",
                    str(runtime / "worker.py"),
                    str(request_path),
                ),
                cwd=work,
                env=_worker_env(work),
                private_output=True,
                preexec_fn=_preexec(profile),
            )
        except Exception:
            return {
                "error": {
                    "code": "structured_parser_unavailable",
                    "message": "The structured document worker could not start.",
                }
            }
        deadline = time.monotonic() + limits["deadline_seconds"]
        terminated, cleanup_ok = _wait_for_worker(child, deadline, cancel_event)
        if terminated is not None:
            terminated["parser"] = {
                "profile_id": profile["profile_id"],
                "profile_sha256": profile_digest,
                "adapter": profile["adapter"]["id"],
                "adapter_version": profile["adapter"]["version"],
                "worker_sha256": worker_digest,
            }
            terminated["cleanup_ok"] = cleanup_ok
            if not cleanup_ok:
                terminated["error"]["code"] = "cleanup_uncertain"
            return terminated
        try:
            raw = _read_bounded(output_path, limits["result_bytes"])
            result = json.loads(raw)
        except (OSError, ValueError, TypeError, RecursionError):
            return {
                "error": {
                    "code": "structured_parser_failed",
                    "message": "The structured document worker produced no valid result.",
                }
            }
        try:
            result = _validate_result(
                result,
                payload=payload,
                extension=extension,
                profile=profile,
                profile_digest=profile_digest,
                worker_digest=worker_digest,
                max_chars=effective_max_chars,
            )
        except ValueError:
            return {
                "error": {
                    "code": "structured_parser_failed",
                    "message": "The structured document worker result failed validation.",
                }
            }
        identity = {
            "profile_id": profile["profile_id"],
            "profile_sha256": profile_digest,
            "adapter": profile["adapter"]["id"],
            "adapter_version": profile["adapter"]["version"],
            "worker_sha256": worker_digest,
        }
        result.setdefault("parser", identity)
        result.setdefault("source", {
            "kind": extension,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "extension": extension,
        })
        result["worker"] = {
            "profile_id": profile["profile_id"],
            "profile_sha256": profile_digest,
            "worker_sha256": worker_digest,
            "containment": profile["containment"]["profile"],
            "cleanup_ok": True,
        }
        return result


__all__ = ["parse_structured_document"]
