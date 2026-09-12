"""Real local HTTP/process tests for the private feature-088 transport."""

from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from shared import external_http, isolated_http as transport
from shared.process_supervision import (
    BoundedStreamReader, OutputStream, ProcessOwner, ProcessSupervisor, TerminationReason,
)

_PRIVATE = "private-http-synthetic-sentinel"


@pytest.fixture
def server():
    seen = []
    entered, disconnected = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            seen.append((self.path, dict(self.headers), None))
            entered.set()
            if self.path.startswith("/status/"):
                status = int(self.path.rsplit("/", 1)[1])
            else:
                status = 200
            body = _PRIVATE.encode()
            if self.path == "/big":
                body = b"x" * 200_000
            if self.path == "/compressed":
                body = gzip.compress(b"x" * 2_000_000)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            if self.path == "/compressed":
                self.send_header("Content-Encoding", "gzip")
            if self.path == "/bad-media":
                self.send_header("Content-Type", "x" * 300)
            self.end_headers()
            try:
                if self.path == "/drip":
                    for _ in range(300):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.01)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, dict(self.headers), json.loads(body)))
            entered.set()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", seen, entered, disconnected
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)
        assert not thread.is_alive()


@pytest.fixture
def children(monkeypatch):
    spawned = []
    original = ProcessSupervisor.spawn

    def spawn(self, **kwargs):
        assert kwargs["private_output"] is True
        assert kwargs["env"] == {"LANG": "C.UTF-8"}
        assert _PRIVATE not in repr(kwargs)
        child = original(self, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(ProcessSupervisor, "spawn", spawn)
    yield spawned
    for child in spawned:
        snapshot = child.terminate(reason=TerminationReason.STOP)
        assert snapshot.process_tree_terminated
        assert snapshot.readers_joined and snapshot.pipes_closed
        assert snapshot.stdout.lines == snapshot.stderr.lines == ()
        assert snapshot.stdout.retained_bytes == snapshot.stderr.retained_bytes == 0


def _envelope(url="https://public.example/page", **kwargs):
    value = {
        "version": 1, "method": "GET", "url": url, "api_key": "", "json_body": None,
        "allowed_private_hosts": [], "max_response_bytes": 1000,
        "deadline": time.monotonic() + 10,
    }
    value.update(kwargs)
    return value


@pytest.mark.asyncio
async def test_real_get_post_freeze_and_no_secret_output(server, children, capfd, monkeypatch):
    url, seen, _, _ = server
    monkeypatch.setenv("INHERITED_PRIVATE_SENTINEL", _PRIVATE)
    body = {"prompt": _PRIVATE, "nested": ["original"]}
    original_write = transport._write

    async def mutate_after_freeze(fd, raw):
        body["nested"][0] = "changed"
        await original_write(fd, raw)

    monkeypatch.setattr(transport, "_write", mutate_after_freeze)
    response = await transport.request(
        "POST", url + "/post", api_key=_PRIVATE, json_body=body,
        allowed_private_hosts=("127.0.0.1",), timeout_seconds=3,
    )
    assert response.status_code == 201
    assert json.loads(response.body)["nested"] == ["original"]
    assert seen[0][1]["Authorization"] == "Bearer " + _PRIVATE
    assert seen[0][1]["Accept-Encoding"] == "identity"
    assert _PRIVATE not in repr(response)
    response = await transport.request("GET", url, allowed_private_hosts=("127.0.0.1",))
    assert response.body == _PRIVATE.encode()
    assert response.final_url == url + "/"
    assert "Authorization" not in seen[1][1]
    assert len(children) == 2
    assert _PRIVATE not in repr(capfd.readouterr())


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "code"), [
    ("/status/401", "authentication"), ("/status/403", "authentication"),
    ("/status/429", "upstream_failure"), ("/status/503", "upstream_failure"),
    ("/status/400", "bad_request"), ("/status/302", "redirect"),
    ("/big", "response_too_large"), ("/compressed", "content_encoding"),
    ("/bad-media", "invalid_response"),
])
async def test_actual_upstream_failures_are_closed(server, children, capfd, path, code):
    with pytest.raises(transport.IsolatedHttpError) as error:
        await transport.request("GET", server[0] + path, max_response_bytes=1000,
                                allowed_private_hosts=("127.0.0.1",))
    assert str(error.value) == code
    assert _PRIVATE not in repr(capfd.readouterr())


@pytest.mark.asyncio
async def test_actual_egress_refusal_does_not_connect(server, children):
    with pytest.raises(transport.IsolatedHttpError, match="^egress_blocked$"):
        await transport.request("GET", server[0])
    assert not server[1]


@pytest.mark.asyncio
async def test_actual_slow_drip_has_total_deadline_and_socket_closes(server, children):
    start = time.monotonic()
    with pytest.raises(transport.IsolatedHttpError, match="^deadline$"):
        await transport.request("GET", server[0] + "/drip", timeout_seconds=0.6,
                                allowed_private_hosts=("127.0.0.1",))
    assert time.monotonic() - start < 0.6 + transport.CLEANUP_SECONDS
    assert server[2].is_set()
    assert await asyncio.to_thread(server[3].wait, 2)
    assert children[0].snapshot().process_tree_terminated


@pytest.mark.asyncio
async def test_cancellation_reaps_real_request(server, children):
    task = asyncio.create_task(transport.request(
        "GET", server[0] + "/drip", allowed_private_hosts=("127.0.0.1",)
    ))
    assert await asyncio.to_thread(server[2].wait, 3)
    task.cancel()
    with pytest.raises(transport.IsolatedHttpCancelled) as error:
        await task
    assert error.value.cleanup_confirmed
    assert await asyncio.to_thread(server[3].wait, 2)
    assert children[0].snapshot().process_tree_terminated


@pytest.mark.asyncio
async def test_delayed_spawn_cancellation_never_transmits(server, children, monkeypatch):
    original = ProcessSupervisor.spawn
    entered = threading.Event()

    def delayed(self, **kwargs):
        entered.set()
        time.sleep(0.2)
        return original(self, **kwargs)

    monkeypatch.setattr(ProcessSupervisor, "spawn", delayed)
    task = asyncio.create_task(transport.request(
        "GET", server[0], allowed_private_hosts=("127.0.0.1",)
    ))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(transport.IsolatedHttpCancelled) as error:
        await task
    assert error.value.cleanup_confirmed
    assert not server[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"method": "DELETE"}, {"url": "file:///etc/passwd"},
    {"url": "https://u:p@example.com"}, {"url": "https://example.com/#fragment"},
    {"url": "https://example.com:invalid/"}, {"url": "https://example.com/\n"},
    {"url": ""}, {"url": None}, {"api_key": "bad\nkey"},
    {"api_key": "x" * 8193}, {"api_key": 1}, {"json_body": {}},
    {"method": "POST", "json_body": []}, {"method": "POST", "json_body": {"x": float("nan")}},
    {"method": "POST", "json_body": {"x": "x" * transport.MAX_REQUEST_BYTES}},
    {"allowed_private_hosts": []}, {"allowed_private_hosts": ("A",)},
    {"allowed_private_hosts": ("x",) * 33}, {"max_response_bytes": True},
    {"max_response_bytes": 0}, {"timeout_seconds": True},
    {"timeout_seconds": float("nan")}, {"timeout_seconds": 61}, {"timeout_seconds": 0},
])
async def test_invalid_inputs_fail_before_spawn(children, kwargs):
    arguments = {"method": "GET", "url": "https://example.com"}
    arguments.update(kwargs)
    with pytest.raises(transport.IsolatedHttpError, match="^invalid_request$"):
        await transport.request(**arguments)
    assert not children


def test_closed_canonical_json_and_schema():
    assert transport._json_bytes({"b": 1, "a": "é"}, 100) == b'{"a":"\\u00e9","b":1}'
    circular = []
    circular.append(circular)
    for value in (circular, object(), {"x": float("inf")}):
        with pytest.raises(transport.IsolatedHttpError):
            transport._json_bytes(value, 100)
    for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'[]', b'\xff', b'{', b'{"x":1e999}'):
        with pytest.raises(transport.IsolatedHttpError):
            transport._json_load(raw)
    for value in (_envelope(version=True), _envelope(extra=1), _envelope(deadline=float("inf"))):
        with pytest.raises(transport.IsolatedHttpError):
            transport._validate_request(value)
    assert transport.IsolatedHttpError(_PRIVATE).code == "child_failure"


@pytest.mark.asyncio
async def test_unsupported_and_os_resource_failures(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "unsupported")
        with pytest.raises(transport.IsolatedHttpError, match="unsupported_platform"):
            await transport.request("GET", "https://example.com")
    original = os.pipe
    opened = []

    def exhausted():
        if not opened:
            value = original()
            opened.extend(value)
            return value
        raise OSError(_PRIVATE)

    monkeypatch.setattr(os, "pipe", exhausted)
    with pytest.raises(transport.IsolatedHttpError, match="^ipc_failure$"):
        await transport.request("GET", "https://example.com")
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    with pytest.raises(transport.IsolatedHttpError, match="^ipc_failure$"):
        await transport.request("GET", "https://example.com")


def test_private_reader_never_forwards_retains_or_includes_error(capfd):
    class Broken(io.BytesIO):
        def read(self, _size):
            raise OSError(_PRIVATE)

    for pipe in (io.BytesIO((_PRIVATE + "\n").encode() * 1000), Broken()):
        reader = BoundedStreamReader(stream=OutputStream.STDERR, pipe=pipe, private_output=True)
        reader.run()
        snapshot = reader.snapshot()
        assert snapshot.lines == () and snapshot.retained_bytes == 0
        assert snapshot.total_bytes == snapshot.dropped_bytes
        assert _PRIVATE not in repr(snapshot)
        with pytest.raises(ValueError, match="^private output cannot be read$"):
            reader.wait_for_line(prefix=_PRIVATE.encode(), timeout=1)
    assert _PRIVATE not in repr(capfd.readouterr())


def test_real_private_child_discards_both_streams(capfd):
    child = ProcessSupervisor().spawn(
        process_id=uuid.uuid4(), owner=ProcessOwner("test", "private"), private_output=True,
        argv=(sys.executable, "-c", "import os; os.write(1,b'private-http-synthetic-sentinel'); "
              "os.write(2,b'private-http-synthetic-sentinel')"),
    )
    snapshot = child.wait(3)
    assert snapshot.stdout.total_bytes == snapshot.stderr.total_bytes == len(_PRIVATE)
    assert snapshot.stdout.lines == snapshot.stderr.lines == ()
    assert _PRIVATE not in repr(capfd.readouterr())


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [
    {"status": 200, "body": "!!!!", "url": "https://example.com", "content_type": ""},
    {"status": True, "body": "", "url": "https://example.com", "content_type": ""},
    {"status": 200, "body": "", "url": "file:///tmp/x", "content_type": ""},
    {"status": 200, "body": "", "url": "https://example.com", "content_type": "\n"},
    {"status": 200, "body": base64.b64encode(b"too large").decode(),
     "url": "https://example.com", "content_type": ""},
    {"error": _PRIVATE}, {"other": 1},
])
async def test_parent_rejects_untrusted_reply(value):
    read_fd, write_fd = os.pipe()
    raw = json.dumps(value).encode()
    os.write(write_fd, struct.pack("!I", len(raw)) + raw)
    os.close(write_fd)
    try:
        with pytest.raises(transport.IsolatedHttpError, match="^ipc_failure$"):
            await transport._read_reply(read_fd, 1)
    finally:
        os.close(read_fd)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"", b"abc", struct.pack("!I", 0),
    struct.pack("!I", transport.MAX_REPLY_BYTES + 1), struct.pack("!I", 2) + b"{}extra"])
async def test_parent_rejects_truncated_oversized_and_extra_frames(raw):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, raw)
    os.close(write_fd)
    try:
        with pytest.raises(transport.IsolatedHttpError, match="^ipc_failure$"):
            await transport._read_reply(read_fd, 1)
    finally:
        os.close(read_fd)


@pytest.mark.asyncio
async def test_pipe_backpressure_and_stalled_reader_timeout():
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    raw = b"x" * 200_000
    try:
        writer = asyncio.create_task(transport._write(write_fd, raw))
        await asyncio.sleep(0.01)
        assert not writer.done()
        assert await transport._read(read_fd, len(raw)) == raw
        await writer
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                await transport._read(read_fd, 1)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                await transport._write(write_fd, raw)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.asyncio
async def test_cleanup_uncertainty_and_repeated_cancellation_are_explicit():
    async def complete():
        await asyncio.sleep(0.06)
        return True

    async def clean():
        return await transport._settle(asyncio.create_task(complete()), time.monotonic() + 1)

    task = asyncio.create_task(clean())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    assert await task == (True, True)
    pending = asyncio.create_task(complete())
    assert await transport._settle(pending, time.monotonic() + 0.001) == (False, False)
    await pending
    cancelled = asyncio.create_task(complete())
    cancelled.cancel()
    assert await transport._settle(cancelled, time.monotonic() + 1) == (False, True)


@pytest.mark.asyncio
async def test_child_spawn_failure_is_closed(monkeypatch):
    def failure(*_args, **_kwargs):
        raise RuntimeError(_PRIVATE)

    monkeypatch.setattr(ProcessSupervisor, "spawn", failure)
    with pytest.raises(transport.IsolatedHttpError, match="^cleanup_uncertain$"):
        await transport.request("GET", "https://example.com")


def test_direct_worker_real_http_and_expired_before_network(server):
    value = _envelope(server[0], allowed_private_hosts=["127.0.0.1"])
    response = transport._perform(value)
    assert base64.b64decode(response["body"]) == _PRIVATE.encode()
    for deadline in (time.monotonic() - 1, time.monotonic() + 100):
        with pytest.raises(transport.IsolatedHttpError, match="^deadline$"):
            transport._perform({**value, "deadline": deadline})
    assert len(server[1]) == 1


def test_parent_death_closes_real_child_socket(server, tmp_path):
    # A task-owned grandparent kills only this disposable caller. The normal
    # helper observes private pipe EOF and kills its own isolated process group.
    script = tmp_path / "caller.py"
    script.write_text(
        "import asyncio,sys\n"
        f"sys.path.insert(0,{str(Path(transport.__file__).parents[1])!r})\n"
        "from shared.isolated_http import request\n"
        "from shared.process_supervision import ProcessSupervisor\n"
        "original=ProcessSupervisor.spawn\n"
        "def spawn(self,**kwargs):\n"
        "    child=original(self,**kwargs)\n"
        "    print(child.pid,flush=True)\n"
        "    return child\n"
        "ProcessSupervisor.spawn=spawn\n"
        f"asyncio.run(request('GET',{(server[0] + '/drip')!r},"
        "allowed_private_hosts=('127.0.0.1',)))\n"
    )
    parent = subprocess.Popen((sys.executable, "-I", "-B", str(script)),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert server[2].wait(3)
        child_pid = int(parent.stdout.readline())
        parent.kill()
        output = parent.communicate(timeout=3)
        assert _PRIVATE.encode() not in b"".join(output)
        assert server[3].wait(3)
        deadline = time.monotonic() + 3
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "orphaned helper was not reaped"
            time.sleep(0.01)
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=3)


@pytest.mark.parametrize("error,code", [
    (external_http.AuthFailedError, "authentication"),
    (external_http.RateLimitedError, "upstream_failure"),
    (external_http.BadRequestError, "bad_request"),
    (external_http.ResponseTooLargeError, "response_too_large"),
    (external_http.ContentEncodingError, "content_encoding"),
    (external_http.ServiceUnreachableError, "unreachable"),
    (external_http.EgressBlockedError, "egress_blocked"),
    (external_http.ExternalHttpError, "child_failure"), (RuntimeError, "child_failure"),
])
def test_worker_errors_use_codes_only(monkeypatch, error, code):
    request_read, request_write = os.pipe()
    response_read, response_write = os.pipe()
    raw = transport._json_bytes(_envelope(), transport.MAX_REQUEST_BYTES)
    os.write(request_write, struct.pack("!I", len(raw)) + raw)
    monkeypatch.setattr(os, "getpgrp", os.getpid)
    monkeypatch.setattr(os, "getsid", lambda _pid: os.getpid())
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    def fail(_value):
        raise error(_PRIVATE)

    monkeypatch.setattr(transport, "_perform", fail)
    try:
        transport._child_main(request_read, response_write)
        size = struct.unpack("!I", transport._sync_read(response_read, 4))[0]
        assert json.loads(transport._sync_read(response_read, size)) == {"error": code}
    finally:
        for fd in (request_read, request_write, response_read):
            os.close(fd)


def test_worker_parent_watch_targets_own_group(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "read", lambda fd, size: b"")
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    transport._parent_watch(999)
    assert calls == [(os.getpid(), signal.SIGKILL)]


@pytest.mark.asyncio
async def test_complete_reply_with_unconfirmed_cleanup_is_refused(server, monkeypatch):
    original = transport._settle

    async def unconfirmed(task, deadline):
        await original(task, deadline)
        return False, False

    monkeypatch.setattr(transport, "_settle", unconfirmed)
    with pytest.raises(transport.IsolatedHttpError, match="^cleanup_uncertain$"):
        await transport.request("GET", server[0], allowed_private_hosts=("127.0.0.1",))


def test_isolated_transport_does_not_read_or_substitute_netrc(server, monkeypatch, tmp_path):
    import requests.sessions
    from requests.utils import get_netrc_auth

    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login synthetic-user password synthetic-password\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    calls = []

    def lookup(url):
        calls.append(url)
        return get_netrc_auth(url)

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lookup)
    # Establish the real Requests behavior with this synthetic fixture. The
    # existing helper's default remains unchanged by the explicit opt-in.
    external_http.request("GET", server[0], api_key=_PRIVATE,
                          allowed_private_hosts=["127.0.0.1"])
    assert calls
    assert server[1][-1][1]["Authorization"].startswith("Basic ")
    calls.clear()
    transport._perform(_envelope(server[0], api_key=_PRIVATE,
                                allowed_private_hosts=["127.0.0.1"]))
    assert calls == []
    assert server[1][-1][1]["Authorization"] == "Bearer " + _PRIVATE
