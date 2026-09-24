"""Runs one HTTP attempt in an isolated, supervised POSIX child process so no request,
response, credential, or exception is logged or persisted; a transport only — callers
must commit their permit first.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import signal
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import external_http  # noqa: E402
from shared.process_supervision import (  # noqa: E402
    ProcessOwner,
    ProcessSupervisor,
    SupervisedProcess,
    TerminationReason,
)

CLEANUP_SECONDS = 5.0
MAX_ATTEMPT_SECONDS = 60.0
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REPLY_BYTES = 3 * 1024 * 1024
_CODES = frozenset({
    "invalid_request", "unsupported_platform", "deadline", "egress_blocked",
    "authentication", "upstream_failure", "bad_request", "response_too_large",
    "content_encoding", "unreachable", "redirect", "invalid_response",
    "ipc_failure", "child_failure", "cleanup_uncertain",
})


class IsolatedHttpError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in _CODES else "child_failure"
        super().__init__(self.code)


class IsolatedHttpCancelled(asyncio.CancelledError):
    def __init__(self, *, cleanup_confirmed: bool) -> None:
        self.cleanup_confirmed = cleanup_confirmed
        super().__init__("isolated HTTP cancelled")


@dataclass(frozen=True)
class IsolatedHttpResponse:
    status_code: int
    body: bytes = field(repr=False)
    final_url: str = field(repr=False)
    content_type: str = field(repr=False)


def _json_bytes(value: Any, limit: int) -> bytes:
    encoded = bytearray()
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        for chunk in encoder.iterencode(value):
            if len(encoded) + len(chunk) > limit:
                raise ValueError
            encoded.extend(chunk.encode("ascii"))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise IsolatedHttpError("invalid_request") from None
    return bytes(encoded)


def _json_load(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
        return result

    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite,
                            parse_float=finite_float)
        if type(result) is not dict:
            raise ValueError
        return result
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise IsolatedHttpError("ipc_failure") from None


def _safe_url(url: Any) -> bool:
    if type(url) is not str or not 1 <= len(url) <= 4096:
        return False
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        return False
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme in ("http", "https") and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not parsed.fragment and (parsed.port is None or parsed.port > 0)
        )
    except ValueError:
        return False


def _validate_request(value: dict) -> None:
    expected = {
        "version", "method", "url", "api_key", "json_body",
        "allowed_private_hosts", "max_response_bytes", "deadline",
    }
    if set(value) != expected or type(value["version"]) is not int or value["version"] != 1:
        raise IsolatedHttpError("invalid_request")
    method, key, hosts = value["method"], value["api_key"], value["allowed_private_hosts"]
    size, deadline = value["max_response_bytes"], value["deadline"]
    if (
        method not in ("GET", "POST") or not _safe_url(value["url"])
        or type(key) is not str or len(key) > 8192
        or any(not 33 <= ord(ch) <= 126 for ch in key)
        or type(hosts) is not list or len(hosts) > 32
        or any(type(host) is not str or not 1 <= len(host) <= 253
               or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789.:-" for ch in host)
               for host in hosts)
        or type(size) is not int or not 1 <= size <= MAX_RESPONSE_BYTES
        or type(deadline) not in (int, float) or not math.isfinite(deadline)
        or (method == "GET" and value["json_body"] is not None)
        or (method == "POST" and type(value["json_body"]) is not dict)
    ):
        raise IsolatedHttpError("invalid_request")


async def _ready(fd: int, *, write: bool) -> None:
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def ready():
        if not future.done():
            future.set_result(None)

    add = loop.add_writer if write else loop.add_reader
    remove = loop.remove_writer if write else loop.remove_reader
    add(fd, ready)
    try:
        await future
    finally:
        remove(fd)


async def _write(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        try:
            view = view[os.write(fd, view):]
        except BlockingIOError:
            await _ready(fd, write=True)


async def _read(fd: int, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        try:
            part = os.read(fd, min(size - len(result), 65536))
        except BlockingIOError:
            await _ready(fd, write=False)
            continue
        if not part:
            raise IsolatedHttpError("ipc_failure")
        result.extend(part)
    return bytes(result)


async def _read_reply(fd: int, maximum: int) -> IsolatedHttpResponse:
    size = struct.unpack("!I", await _read(fd, 4))[0]
    if not 1 <= size <= MAX_REPLY_BYTES:
        raise IsolatedHttpError("ipc_failure")
    value = _json_load(await _read(fd, size))
    while True:
        try:
            if os.read(fd, 1):
                raise IsolatedHttpError("ipc_failure")
            break
        except BlockingIOError:
            await _ready(fd, write=False)
    if set(value) == {"error"} and type(value["error"]) is str and value["error"] in _CODES:
        raise IsolatedHttpError(value["error"])
    if set(value) != {"status", "body", "url", "content_type"}:
        raise IsolatedHttpError("ipc_failure")
    try:
        body = base64.b64decode(value["body"], validate=True)
        if (
            type(value["status"]) is not int or not 200 <= value["status"] < 300
            or len(body) > maximum or not _safe_url(value["url"])
            or type(value["content_type"]) is not str or len(value["content_type"]) > 256
            or any(ord(ch) < 32 or ord(ch) > 126 for ch in value["content_type"])
        ):
            raise ValueError
    except (ValueError, TypeError, binascii.Error):
        raise IsolatedHttpError("ipc_failure") from None
    return IsolatedHttpResponse(value["status"], body, value["url"], value["content_type"])


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


async def _settle(task: asyncio.Task, deadline: float) -> tuple[bool, bool]:
    cancelled = False
    while True:
        try:
            async with asyncio.timeout_at(deadline):
                return bool(await asyncio.shield(task)), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.cancelled() or time.monotonic() >= deadline:
                return False, cancelled
        except Exception:
            return False, cancelled


async def request(
    method: str,
    url: str,
    *,
    api_key: str = "",
    json_body: Any = None,
    allowed_private_hosts: tuple[str, ...] = (),
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    timeout_seconds: float = MAX_ATTEMPT_SECONDS,
) -> IsolatedHttpResponse:
    if sys.platform not in ("linux", "darwin") or os.name != "posix":
        raise IsolatedHttpError("unsupported_platform")
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= MAX_ATTEMPT_SECONDS:
        raise IsolatedHttpError("invalid_request")
    if type(allowed_private_hosts) is not tuple:
        raise IsolatedHttpError("invalid_request")
    deadline = time.monotonic() + timeout_seconds
    envelope = {
        "version": 1, "method": method, "url": url, "api_key": api_key,
        "json_body": json_body, "allowed_private_hosts": list(allowed_private_hosts),
        "max_response_bytes": max_response_bytes, "deadline": deadline,
    }
    _validate_request(envelope)
    raw = _json_bytes(envelope, MAX_REQUEST_BYTES)
    supervisor = ProcessSupervisor()
    try:
        request_read, request_write = os.pipe()
    except OSError:
        raise IsolatedHttpError("ipc_failure") from None
    try:
        response_read, response_write = os.pipe()
    except OSError:
        _close(request_read)
        _close(request_write)
        raise IsolatedHttpError("ipc_failure") from None
    stopped = threading.Event()

    def spawn() -> SupervisedProcess:
        # This thread alone owns these fds until spawn() finishes
        try:
            child = supervisor.spawn(
                process_id=uuid.uuid4(), owner=ProcessOwner("private_http", "attempt"),
                argv=(sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                      str(request_read), str(response_write)),
                env={"LANG": "C.UTF-8"}, private_output=True,
                pass_fds=(request_read, response_write),
            )
            if stopped.is_set():
                child.terminate(reason=TerminationReason.CANCEL)
            return child
        finally:
            _close(request_read)
            _close(response_write)

    spawn_task = asyncio.create_task(asyncio.to_thread(spawn))
    cancelled = False
    failure: IsolatedHttpError | None = None
    result = None
    try:
        os.set_blocking(request_write, False)
        os.set_blocking(response_read, False)
        async with asyncio.timeout_at(deadline):
            await asyncio.shield(spawn_task)
            await _write(request_write, struct.pack("!I", len(raw)) + raw)
            result = await _read_reply(response_read, max_response_bytes)
    except asyncio.CancelledError:
        cancelled = True
    except TimeoutError:
        failure = IsolatedHttpError("deadline")
    except IsolatedHttpError as exc:
        failure = exc
    except Exception:
        failure = IsolatedHttpError("child_failure")
    finally:
        stopped.set()

        async def cleanup() -> bool:
            child = await asyncio.shield(spawn_task)
            snapshot = await asyncio.to_thread(child.terminate, reason=TerminationReason.STOP)
            return bool(
                snapshot.process_tree_terminated and snapshot.readers_joined
                and snapshot.pipes_closed and snapshot.cleanup_error is None
            )

        cleanup_task = asyncio.create_task(cleanup())
        confirmed, cleanup_cancelled = await _settle(
            cleanup_task, time.monotonic() + CLEANUP_SECONDS
        )
        # Order matters here: races SIGKILL against SIGTERM (macOS)
        _close(request_write)
        _close(response_read)
        cancelled = cancelled or cleanup_cancelled
        cleanup_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
    if cancelled:
        raise IsolatedHttpCancelled(cleanup_confirmed=confirmed) from None
    if not confirmed:
        raise IsolatedHttpError("cleanup_uncertain") from None
    if failure is not None:
        raise failure from None
    return result


def _sync_read(fd: int, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        part = os.read(fd, min(size - len(result), 65536))
        if not part:
            raise IsolatedHttpError("ipc_failure")
        result.extend(part)
    return bytes(result)


def _parent_watch(fd: int) -> None:
    try:
        os.read(fd, 1)
    finally:
        os.killpg(os.getpid(), signal.SIGKILL)


def _perform(value: dict) -> dict:
    _validate_request(value)
    remaining = value["deadline"] - time.monotonic()
    if not 0 < remaining <= MAX_ATTEMPT_SECONDS:
        raise IsolatedHttpError("deadline")
    response = external_http.request(
        value["method"], value["url"], api_key=value["api_key"],
        json_body=value["json_body"], allowed_private_hosts=value["allowed_private_hosts"],
        max_response_bytes=value["max_response_bytes"], timeout=remaining,
        allow_redirects=False, require_identity_encoding=True, trust_environment=False,
    )
    if 300 <= response.status_code < 400:
        raise IsolatedHttpError("redirect")
    content_type = response.headers.get("Content-Type", "")
    if len(content_type) > 256 or any(ord(ch) < 32 or ord(ch) > 126 for ch in content_type):
        raise IsolatedHttpError("invalid_response")
    return {
        "status": response.status_code, "url": response.url,
        "body": base64.b64encode(response.content).decode("ascii"),
        "content_type": content_type,
    }


def _child_main(read_fd: int, write_fd: int) -> None:
    if os.getpid() != os.getpgrp() or os.getsid(0) != os.getpid():
        return
    try:
        size = struct.unpack("!I", _sync_read(read_fd, 4))[0]
        if not 1 <= size <= MAX_REQUEST_BYTES:
            raise IsolatedHttpError("ipc_failure")
        value = _json_load(_sync_read(read_fd, size))
        threading.Thread(target=_parent_watch, args=(read_fd,), daemon=True).start()
        reply = _perform(value)
    except IsolatedHttpError as exc:
        reply = {"error": exc.code}
    except external_http.ExternalHttpError as exc:
        codes = {
            external_http.EgressBlockedError: "egress_blocked",
            external_http.AuthFailedError: "authentication",
            external_http.RateLimitedError: "upstream_failure",
            external_http.BadRequestError: "bad_request",
            external_http.ResponseTooLargeError: "response_too_large",
            external_http.ContentEncodingError: "content_encoding",
            external_http.ServiceUnreachableError: "unreachable",
        }
        reply = {"error": codes.get(type(exc), "child_failure")}
    except Exception:
        reply = {"error": "child_failure"}
    try:
        raw = _json_bytes(reply, MAX_REPLY_BYTES)
        view = memoryview(struct.pack("!I", len(raw)) + raw)
        while view:
            view = view[os.write(write_fd, view):]
    finally:
        _close(write_fd)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3:
            _child_main(int(sys.argv[1]), int(sys.argv[2]))
    except Exception:
        pass
