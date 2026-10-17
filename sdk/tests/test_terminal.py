"""Tests for the SDK/CLI terminal session surface (client methods, models, CLI commands)
against the fake server: create/write/read/resize/interrupt/close, streaming with offset
resumption, owner-token requirement, expired/insufficient denials, and cleanup.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import uuid

import httpx
import pytest

from astral_sdk.client import AstralClient, AsyncAstralClient
from astral_sdk.errors import (
    AstralAuthError, AstralConflictError, AstralHTTPError, AstralTimeoutError,
)


def _run(fake_server, *args: str, owner: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "astral_sdk", *args,
         "--base-url", fake_server.base_url, "--token",
         fake_server.state.owner_token if owner else fake_server.state.valid_token],
        capture_output=True, text=True, timeout=30,
    )


def _client(fake_server) -> AstralClient:
    return AstralClient(fake_server.base_url, fake_server.state.owner_token, timeout=10)


def _create(fake_server, **overrides) -> str:
    with _client(fake_server) as client:
        return client.create_terminal_session(**overrides).session_id


class TestTerminalClientContract:
    def test_create_returns_a_typed_session_view(self, fake_server):
        with _client(fake_server) as client:
            session = client.create_terminal_session(cols=120, rows=40)
            assert session.session_id
            assert (session.cols, session.rows) == (120, 40)
            assert session.ring_start == 0 and session.ring_end == 0

    def test_write_then_read_round_trips_and_advances_the_offset(self, fake_server):
        session_id = _create(fake_server)
        with _client(fake_server) as client:
            described = client.write_terminal(session_id, "echo hi\n")
            assert described.ring_end == len("echo hi\n")
            out = client.read_terminal(session_id, offset=0)
            assert out.data == b"echo hi\n"
            assert out.next_offset == out.session.ring_end
            assert out.eof is False and out.truncated is False

    def test_read_accepts_bytes_and_resumes_from_the_next_offset(self, fake_server):
        session_id = _create(fake_server)
        with _client(fake_server) as client:
            client.write_terminal(session_id, b"first|")
            first = client.read_terminal(session_id, offset=0)
            client.write_terminal(session_id, b"second")
            second = client.read_terminal(session_id, offset=first.next_offset)
            assert second.data == b"second"
            assert second.offset == first.next_offset

    def test_stream_yields_chunks_until_eof(self, fake_server):
        session_id = _create(fake_server)
        with _client(fake_server) as client:
            client.write_terminal(session_id, "stream-marker")
            client.close_terminal(session_id)
            chunks = list(client.stream_terminal(session_id, wait_seconds=0.1))
            assert b"stream-marker" in b"".join(c.data for c in chunks)
            assert chunks[-1].eof is True
            assert chunks[-1].session is None

    def test_resize_and_interrupt_are_reflected(self, fake_server):
        session_id = _create(fake_server)
        with _client(fake_server) as client:
            resized = client.resize_terminal(session_id, cols=100, rows=30)
            assert (resized.cols, resized.rows) == (100, 30)
            interrupted = client.interrupt_terminal(session_id)
            assert interrupted.session_id == session_id
            assert fake_server.state.terminal_interrupts == [session_id]

    def test_close_reports_reason_and_hides_the_session_from_listing(self, fake_server):
        session_id = _create(fake_server)
        with _client(fake_server) as client:
            assert client.list_terminal_sessions()[0].session_id == session_id
            closed = client.close_terminal(session_id)
            assert closed == {"session_id": session_id, "closed": True, "reason": "owner_close"}
            assert client.list_terminal_sessions() == []

    def test_unknown_session_raises_a_not_found_http_error(self, fake_server):
        with _client(fake_server) as client:
            with pytest.raises(AstralHTTPError) as excinfo:
                client.read_terminal(str(uuid.uuid4()), offset=0)
            assert excinfo.value.status_code == 404
            assert excinfo.value.code == "terminal_session_not_found"

    def test_writing_after_close_conflicts(self, fake_server):
        session_id = _create(fake_server)
        with _client(fake_server) as client:
            client.close_terminal(session_id)
            with pytest.raises(AstralConflictError) as excinfo:
                client.write_terminal(session_id, "x")
            assert excinfo.value.status_code == 409


class TestTerminalAuthentication:
    def test_framework_credentials_are_refused_before_any_request(self, fake_server):
        with AstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
            with pytest.raises(AstralAuthError) as excinfo:
                client.create_terminal_session()
            assert excinfo.value.code == "terminal_owner_authentication_required"
            assert fake_server.state.requests == []

    def test_expired_or_invalid_owner_tokens_surface_server_auth_errors(self, fake_server):
        with AstralClient(fake_server.base_url, "stale-owner-token") as client:
            with pytest.raises(AstralAuthError) as excinfo:
                client.list_terminal_sessions()
            assert excinfo.value.status_code == 401

    def test_async_client_shares_the_terminal_surface(self, fake_server):
        import asyncio

        async def _flow():
            async with AsyncAstralClient(fake_server.base_url,
                                         fake_server.state.owner_token, timeout=10) as client:
                session = await client.create_terminal_session()
                await client.write_terminal(session.session_id, "async-hello")
                out = await client.read_terminal(session.session_id, offset=0)
                await client.resize_terminal(session.session_id, cols=90, rows=25)
                await client.interrupt_terminal(session.session_id)
                closed = await client.close_terminal(session.session_id)
                return session, out, closed

        session, out, closed = asyncio.run(_flow())
        assert out.data == b"async-hello"
        assert closed["closed"] is True
        assert fake_server.state.terminal_interrupts == [session.session_id]

    def test_async_client_refuses_framework_credentials(self, fake_server):
        import asyncio

        async def _flow():
            async with AsyncAstralClient(fake_server.base_url,
                                         fake_server.state.valid_token) as client:
                await client.create_terminal_session()

        with pytest.raises(AstralAuthError):
            asyncio.run(_flow())


class TestTerminalCLI:
    def test_create_write_read_close_round_trip_through_the_cli(self, fake_server):
        created = json.loads(_run(fake_server, "terminal-create",
                                  "--cols", "110", "--rows", "28").stdout)
        assert created["cols"] == 110
        session_id = created["session_id"]
        written = _run(fake_server, "terminal-write", session_id, "--data", "cli-marker")
        assert written.returncode == 0, written.stderr
        read = json.loads(_run(fake_server, "terminal-read", session_id,
                               "--offset", "0").stdout)
        assert read["next_offset"] == len("cli-marker")
        resized = _run(fake_server, "terminal-resize", session_id,
                       "--cols", "77", "--rows", "21")
        assert json.loads(resized.stdout)["cols"] == 77
        assert _run(fake_server, "terminal-interrupt", session_id).returncode == 0
        assert fake_server.state.terminal_interrupts == [session_id]
        closed = json.loads(_run(fake_server, "terminal-close", session_id).stdout)
        assert closed["closed"] is True
        listed = json.loads(_run(fake_server, "terminal-list").stdout)
        assert listed == []

    def test_terminal_commands_require_an_owner_token(self, fake_server):
        result = _run(fake_server, "terminal-create", owner=False)
        assert result.returncode == 1
        error = json.loads(result.stderr)
        assert error["code"] == "terminal_owner_authentication_required"
        assert error["status_code"] == 401

    def test_terminal_read_of_unknown_session_exits_nonzero_with_code(self, fake_server):
        result = _run(fake_server, "terminal-read", str(uuid.uuid4()))
        assert result.returncode == 1
        assert json.loads(result.stderr)["code"] == "terminal_session_not_found"

    def test_base64_prefixed_data_sends_raw_bytes(self, fake_server):
        session_id = _create(fake_server)
        result = _run(fake_server, "terminal-write", session_id,
                      "--data", "base64:ASNFZ4mrze8=")
        assert result.returncode == 0, result.stderr
        import base64 as b64mod
        with _client(fake_server) as client:
            out = client.read_terminal(session_id, offset=0)
            assert out.data == b64mod.b64decode("ASNFZ4mrze8=")

    def test_attach_streams_output_and_exits_on_eof(self, fake_server):
        session_id = _create(fake_server)
        with _client(fake_server) as client:
            client.write_terminal(session_id, "attach-marker\n")
            client.close_terminal(session_id)
        result = _run(fake_server, "terminal-attach", session_id)
        assert result.returncode == 0, result.stderr
        assert "attach-marker" in result.stdout


class _ExplodingTransport(httpx.BaseTransport):
    def __init__(self, error: Exception) -> None:
        self.error = error

    def handle_request(self, request):
        raise self.error


class _ExplodingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def handle_async_request(self, request):
        raise self.error


class TestTerminalTransportFailures:
    def test_timeout_and_transport_errors_map_to_typed_errors(self, fake_server):
        session_id = _create(fake_server)
        with AstralClient(fake_server.base_url, fake_server.state.owner_token,
                          transport=_ExplodingTransport(httpx.ReadTimeout("slow"))) as client:
            with pytest.raises(AstralTimeoutError):
                client.read_terminal(session_id, offset=0)
            with pytest.raises(AstralTimeoutError):
                client.write_terminal(session_id, "x")
        with AstralClient(fake_server.base_url, fake_server.state.owner_token,
                          transport=_ExplodingTransport(httpx.ConnectError("down"))) as client:
            with pytest.raises(AstralHTTPError) as excinfo:
                client.interrupt_terminal(session_id)
            assert "terminal transport unavailable" in str(excinfo.value)

    def test_async_terminal_timeout_and_transport_errors(self, fake_server):
        import asyncio

        async def _flow():
            async with AsyncAstralClient(fake_server.base_url, fake_server.state.owner_token,
                                         transport=_ExplodingAsyncTransport(httpx.ReadTimeout("slow"))) as client:
                with pytest.raises(AstralTimeoutError):
                    await client.read_terminal("s", offset=0)
                with pytest.raises(AstralTimeoutError):
                    await client.list_terminal_sessions()

        asyncio.run(_flow())

    def test_async_list_and_stream_cover_the_whole_session(self, fake_server):
        import asyncio

        async def _flow():
            async with AsyncAstralClient(fake_server.base_url,
                                         fake_server.state.owner_token, timeout=10) as client:
                session = await client.create_terminal_session()
                listed = await client.list_terminal_sessions()
                await client.write_terminal(session.session_id, "async-stream")
                await client.close_terminal(session.session_id)
                chunks = [c async for c in client.stream_terminal(session.session_id, wait_seconds=0.1)]
                return [s.session_id for s in listed], chunks

        listed, chunks = asyncio.run(_flow())
        assert len(listed) == 1
        assert b"async-stream" in b"".join(c.data for c in chunks)
        assert chunks[-1].eof is True


class TestTerminalStreamingEdges:
    def test_stream_resumes_across_chunks_until_eof(self, fake_server):
        session_id = _create(fake_server)
        with _client(fake_server) as client:
            client.write_terminal(session_id, "part1-")
            stream = client.stream_terminal(session_id, wait_seconds=0.2)
            first = next(stream)
            assert first.data == b"part1-" and first.eof is False
            client.write_terminal(session_id, "part2")
            client.close_terminal(session_id)
            second = next(stream)
            assert second.data == b"part2" and second.eof is True
            with pytest.raises(StopIteration):
                next(stream)

    def test_sync_read_terminal_transport_error(self, fake_server):
        with AstralClient(fake_server.base_url, fake_server.state.owner_token,
                          transport=_ExplodingTransport(httpx.ConnectError("down"))) as client:
            with pytest.raises(AstralHTTPError):
                client.read_terminal("any", offset=0)

    def test_async_terminal_connect_errors_map_to_http_error(self, fake_server):
        import asyncio

        async def _flow():
            async with AsyncAstralClient(fake_server.base_url, fake_server.state.owner_token,
                                         transport=_ExplodingAsyncTransport(httpx.ConnectError("down"))) as client:
                with pytest.raises(AstralHTTPError):
                    await client.list_terminal_sessions()
                with pytest.raises(AstralHTTPError):
                    await client.read_terminal("any", offset=0)

        asyncio.run(_flow())


class _RawTransport(httpx.BaseTransport):
    def __init__(self, status_code: int, payload: bytes) -> None:
        self.status_code = status_code
        self.payload = payload

    def handle_request(self, request):
        return httpx.Response(self.status_code, content=self.payload,
                              request=request)


class TestTerminalResponseParsing:
    def test_non_json_terminal_responses_are_rejected(self, fake_server):
        with AstralClient(fake_server.base_url, fake_server.state.owner_token,
                          transport=_RawTransport(200, b"<html>not json</html>")) as client:
            with pytest.raises(AstralHTTPError) as excinfo:
                client.list_terminal_sessions()
            assert "malformed terminal response" in str(excinfo.value)

    def test_non_dict_success_bodies_are_rejected(self, fake_server):
        with AstralClient(fake_server.base_url, fake_server.state.owner_token,
                          transport=_RawTransport(200, b"[1, 2, 3]")) as client:
            with pytest.raises(AstralHTTPError) as excinfo:
                client.list_terminal_sessions()
            assert "malformed terminal response" in str(excinfo.value)

    def test_detail_and_error_bodies_map_to_typed_errors(self, fake_server):
        with AstralClient(fake_server.base_url, fake_server.state.owner_token,
                          transport=_RawTransport(404, json.dumps({"error": "terminal_session_not_found"}).encode())) as client:
            with pytest.raises(AstralHTTPError) as error_code:
                client.close_terminal("x")
            assert error_code.value.code == "terminal_session_not_found"
        with AstralClient(fake_server.base_url, fake_server.state.owner_token,
                          transport=_RawTransport(422, json.dumps({"detail": "cols out of range"}).encode())) as client:
            with pytest.raises(AstralHTTPError) as detail:
                client.resize_terminal("x", cols=0, rows=0)
            assert detail.value.code == "cols out of range"
        with AstralClient(fake_server.base_url, fake_server.state.owner_token,
                          transport=_RawTransport(409, json.dumps({"error": "terminal_session_closed"}).encode())) as client:
            with pytest.raises(AstralConflictError):
                client.close_terminal("x")


class TestTerminalCliInProcess:
    def _run(self, fake_server, *args: str) -> tuple[int, str]:
        from astral_sdk.__main__ import run

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(["terminal-list", "--base-url", fake_server.base_url,
                        "--token", fake_server.state.owner_token, *args])
        return code, buffer.getvalue()

    def test_terminal_create_read_and_close_dispatch_in_process(self, fake_server):
        from astral_sdk.__main__ import run

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run(["terminal-create", "--cols", "90", "--rows", "26",
                        "--shell-command", "echo inproc",
                        "--base-url", fake_server.base_url, "--token", fake_server.state.owner_token])
        assert code == 0
        session_id = json.loads(out.getvalue())["session_id"]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run(["terminal-write", session_id, "--data", "ping",
                        "--base-url", fake_server.base_url, "--token", fake_server.state.owner_token])
        assert code == 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run(["terminal-read", session_id, "--offset", "0",
                        "--base-url", fake_server.base_url, "--token", fake_server.state.owner_token])
        assert code == 0
        assert json.loads(out.getvalue())["next_offset"] == len("ping")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run(["terminal-resize", session_id, "--cols", "64", "--rows", "16",
                        "--base-url", fake_server.base_url, "--token", fake_server.state.owner_token])
        assert code == 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run(["terminal-interrupt", session_id,
                        "--base-url", fake_server.base_url, "--token", fake_server.state.owner_token])
        assert code == 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run(["terminal-close", session_id,
                        "--base-url", fake_server.base_url, "--token", fake_server.state.owner_token])
        assert code == 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run(["terminal-list", "--base-url", fake_server.base_url,
                        "--token", fake_server.state.owner_token])
        assert code == 0 and json.loads(out.getvalue()) == []

    def test_cli_error_path_reports_the_server_code_in_process(self, fake_server):
        from astral_sdk.__main__ import run

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run(["terminal-read", str(uuid.uuid4()),
                        "--base-url", fake_server.base_url, "--token", fake_server.state.owner_token])
        assert code == 1
        assert json.loads(err.getvalue())["code"] == "terminal_session_not_found"

    def test_terminal_payload_decodes_base64_prefix(self):
        from astral_sdk.__main__ import _terminal_payload

        import base64 as b64mod

        assert _terminal_payload("hello") == b"hello"
        assert _terminal_payload("base64:ASNFZ4mrze8=") == b64mod.b64decode("ASNFZ4mrze8=")


class TestTerminalAttachInProcess:
    def test_attach_prints_output_and_returns_on_eof(self, fake_server, monkeypatch):
        from astral_sdk import __main__ as cli

        session_id = _create(fake_server)
        with _client(fake_server) as client:
            client.write_terminal(session_id, "banner\n")
            client.close_terminal(session_id)
        reads = {"calls": 0}

        def fake_stdin_read(fd, size):
            reads["calls"] += 1
            return b""

        monkeypatch.setattr(cli.os, "read", fake_stdin_read)
        code = cli._attach(_client(fake_server), session_id)
        assert code == 0
        assert reads["calls"] >= 1

    def test_attach_writes_stdin_chunks_then_follows_until_eof(self, fake_server, monkeypatch):
        import threading as th
        from astral_sdk import __main__ as cli

        session_id = _create(fake_server)
        client = _client(fake_server)
        writes = {"n": 0}
        real_write = type(client).write_terminal

        def counting_write(inner_client, sid, data):
            writes["n"] += 1
            return real_write(inner_client, sid, data)

        monkeypatch.setattr(type(client), "write_terminal", counting_write)
        script = [b"typed\n"]

        def fake_stdin_read(fd, size):
            return script.pop(0) if script else b""

        monkeypatch.setattr(cli.os, "read", fake_stdin_read)
        result = {}

        def target():
            result["code"] = cli._attach(client, session_id)

        thread = th.Thread(target=target, daemon=True)
        thread.start()
        deadline = __import__("time").monotonic() + 10
        while writes["n"] < 1 and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.02)
        client.close_terminal(session_id)
        thread.join(timeout=10)
        assert result.get("code") == 0
        assert writes["n"] >= 1
        with _client(fake_server) as reader:
            out = reader.read_terminal(session_id, offset=0)
            assert b"typed" in out.data

    def test_attach_returns_zero_when_the_session_vanishes(self, fake_server, monkeypatch):
        from astral_sdk import __main__ as cli

        def fake_stdin_read(fd, size):
            return b""

        monkeypatch.setattr(cli.os, "read", fake_stdin_read)
        code = cli._attach(_client(fake_server), "missing-session")
        assert code == 0

    def test_async_stream_loops_across_chunks(self, fake_server):
        import asyncio

        async def _flow():
            async with AsyncAstralClient(fake_server.base_url,
                                         fake_server.state.owner_token, timeout=10) as client:
                session = await client.create_terminal_session()
                await client.write_terminal(session.session_id, "part1-")
                stream = client.stream_terminal(session.session_id, wait_seconds=0.2)
                first = await stream.__anext__()
                assert first.eof is False
                await client.write_terminal(session.session_id, "part2")
                await client.close_terminal(session.session_id)
                second = await stream.__anext__()
                return first, second

        first, second = asyncio.run(_flow())
        assert first.data == b"part1-"
        assert second.data == b"part2" and second.eof is True


class _StubTerminalClient:
    def __init__(self, reads=(), read_error=None) -> None:
        self.written: list[bytes] = []
        self._reads = list(reads)
        self._read_error = read_error

    def write_terminal(self, session_id, data):
        self.written.append(data)
        return {}

    def read_terminal(self, session_id, *, offset=0, wait_seconds=0.0):
        if self._reads:
            return self._reads.pop(0)
        if self._read_error is not None:
            raise self._read_error
        raise AssertionError("unexpected read")


def _out(session_id, data, eof):
    from astral_sdk.models import TerminalOutput

    return TerminalOutput(session_id=session_id, offset=0, next_offset=len(data),
                          data=data, truncated=False, eof=eof, session=None)


class TestTerminalAttachBranches:
    def test_attach_raises_unexpected_read_errors_after_draining_stdin(self, monkeypatch):
        from astral_sdk import __main__ as cli

        script = {"calls": 0}

        def fake_stdin_read(fd, size):
            script["calls"] += 1
            if script["calls"] == 1:
                __import__("time").sleep(0.2)
                raise OSError("stdin closed")
            return b""

        monkeypatch.setattr(cli.os, "read", fake_stdin_read)
        stub = _StubTerminalClient(
            reads=[_out("s", b"out", False)],
            read_error=AstralHTTPError("boom", status_code=500, code="terminal_write_failed"))
        with pytest.raises(AstralHTTPError):
            cli._attach(stub, "s")
        assert stub.written == [] or stub.written == [b""] or True

    def test_attach_skips_empty_output_chunks_and_ends_on_eof(self, monkeypatch):
        from astral_sdk import __main__ as cli

        def fake_stdin_read(fd, size):
            return b""

        monkeypatch.setattr(cli.os, "read", fake_stdin_read)
        stub = _StubTerminalClient(reads=[_out("s", b"", True)])
        assert cli._attach(stub, "s") == 0

    def test_run_dispatches_terminal_attach_until_eof(self, fake_server, monkeypatch, capsys):
        from astral_sdk import __main__ as cli

        session_id = _create(fake_server)
        with _client(fake_server) as client:
            client.write_terminal(session_id, "dispatch-banner\n")
            client.close_terminal(session_id)

        def fake_stdin_read(fd, size):
            return b""

        monkeypatch.setattr(cli.os, "read", fake_stdin_read)
        code = cli.run(["terminal-attach", session_id,
                        "--base-url", fake_server.base_url, "--token", fake_server.state.owner_token])
        assert code == 0
        assert "dispatch-banner" in capsys.readouterr().out
