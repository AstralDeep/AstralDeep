"""Exercises bounded stream readers through run and snapshot with counted local pipes.
Oversized input, private diagnostics, broken reads and forwarding retain deterministic work bounds.
"""

import io
import math
import sys
import threading
from uuid import uuid4

import pytest

from shared.process_supervision import (
    BoundedStreamReader,
    OutputStream,
    ProcessOwner,
    ProcessSupervisionLimits,
    ProcessSupervisor,
)

LIMITS = ProcessSupervisionLimits(
    read_chunk_bytes=32,
    maximum_logical_line_bytes=64,
    ring_capacity_bytes_per_stream=128,
    ring_capacity_bytes_per_process=256,
)


class CountedPipe(io.BytesIO):
    def __init__(self, content, *, fail_after=None):
        super().__init__(content)
        self.requested = []
        self.close_count = 0
        self.fail_after = fail_after

    def read(self, size=-1):
        self.requested.append(size)
        if self.fail_after is not None and len(self.requested) > self.fail_after:
            raise OSError("SYNTHETIC_PRIVATE_DIAGNOSTIC")
        return super().read(size)

    def close(self):
        self.close_count += 1
        super().close()


class CountedHost:
    def __init__(self, *, broken=False):
        self.buffer = self
        self.content = bytearray()
        self.write_count = 0
        self.flush_count = 0
        self.broken = broken

    def write(self, content):
        self.write_count += 1
        if self.broken:
            raise OSError("host unavailable")
        self.content.extend(content)

    def flush(self):
        self.flush_count += 1


def run_reader(
    monkeypatch, content, *, private=False, fail_after=None, broken_host=False
):
    pipe = CountedPipe(content, fail_after=fail_after)
    host = CountedHost(broken=broken_host)
    monkeypatch.setattr(sys, "stdout", host)
    reader = BoundedStreamReader(
        stream=OutputStream.STDOUT,
        pipe=pipe,
        limits=LIMITS,
        private_output=private,
    )
    reader.run()
    return reader, pipe, host


@pytest.mark.parametrize("scale", [1, 4])
def test_reader_oversized_line_has_counted_linear_work(monkeypatch, scale):
    content = b"x" * (LIMITS.maximum_logical_line_bytes * scale + 3) + b"\n"
    reader, pipe, host = run_reader(monkeypatch, content)
    snapshot = reader.snapshot()
    assert set(pipe.requested) == {LIMITS.read_chunk_bytes}
    assert len(pipe.requested) == math.ceil(len(content) / LIMITS.read_chunk_bytes) + 1
    assert host.write_count == len(pipe.requested) - 1
    assert bytes(host.content) == content
    assert snapshot.lines == (b"x" * LIMITS.maximum_logical_line_bytes,)
    assert snapshot.total_bytes == len(content)
    assert (
        snapshot.dropped_bytes == len(content) - LIMITS.maximum_logical_line_bytes - 1
    )
    assert snapshot.overlong_lines == 1
    assert snapshot.reader_done and snapshot.pipe_closed and pipe.close_count == 1


def test_reader_ring_retains_bounded_recent_complete_lines(monkeypatch):
    lines = [str(index).encode().ljust(32, b"x") for index in range(10)]
    content = b"\n".join(lines) + b"\n"
    reader, pipe, _ = run_reader(monkeypatch, content)
    snapshot = reader.snapshot()
    assert snapshot.retained_bytes <= LIMITS.ring_capacity_bytes_per_stream, (
        "ring bound removed"
    )
    assert snapshot.lines == tuple(lines[-3:])
    assert snapshot.total_lines == 10 and snapshot.dropped_lines == 7
    assert snapshot.retained_bytes + snapshot.dropped_bytes == len(content)
    assert len(pipe.requested) == math.ceil(len(content) / LIMITS.read_chunk_bytes) + 1


@pytest.mark.parametrize("failure", [False, True])
def test_private_reader_never_forwards_or_retains_diagnostics(monkeypatch, failure):
    content = b"SYNTHETIC_PRIVATE_DIAGNOSTIC\n" * 9
    reader, pipe, host = run_reader(
        monkeypatch,
        content,
        private=True,
        fail_after=1 if failure else None,
    )
    snapshot = reader.snapshot()
    assert host.write_count == 0 and not host.content, "private output was forwarded"
    assert snapshot.lines == () and snapshot.retained_bytes == 0, (
        "private output was retained"
    )
    assert snapshot.total_bytes == snapshot.dropped_bytes
    assert snapshot.total_bytes == (32 if failure else len(content))
    assert snapshot.read_error == ("private output read failed" if failure else None)
    assert "SYNTHETIC_PRIVATE_DIAGNOSTIC" not in repr(snapshot)
    assert snapshot.reader_done and snapshot.pipe_closed and pipe.close_count == 1
    with pytest.raises(ValueError, match="private output"):
        reader.wait_for_line(prefix=b"SYNTHETIC", timeout=1)


def test_reader_failure_settles_partial_line_without_extra_reads(monkeypatch):
    content = b"a" * 65
    reader, pipe, host = run_reader(monkeypatch, content, fail_after=1)
    snapshot = reader.snapshot()
    assert len(pipe.requested) == 2
    assert snapshot.total_bytes == 32 and snapshot.lines == (b"a" * 32,)
    assert host.write_count == 1 and host.content == b"a" * 32
    assert snapshot.read_error == "OSError: SYNTHETIC_PRIVATE_DIAGNOSTIC"
    assert snapshot.reader_done and snapshot.pipe_closed and pipe.close_count == 1


def test_failed_forwarding_does_not_repeat_or_abandon_reads(monkeypatch):
    content = b"complete line\n" * 4
    reader, pipe, host = run_reader(monkeypatch, content, broken_host=True)
    snapshot = reader.snapshot()
    assert len(pipe.requested) == math.ceil(len(content) / LIMITS.read_chunk_bytes) + 1
    assert host.write_count == len(pipe.requested) - 1
    assert snapshot.total_bytes == len(content) and snapshot.total_lines == 4
    assert snapshot.lines == (b"complete line",) * 4
    assert snapshot.read_error is None and snapshot.reader_done and snapshot.pipe_closed


class CloseFailurePipe(CountedPipe):
    def __init__(self, content, *, close_before_failure):
        super().__init__(content)
        self.close_before_failure = close_before_failure

    def close(self):
        self.close_count += 1
        if self.close_before_failure:
            io.BytesIO.close(self)
        raise OSError("SYNTHETIC_PRIVATE_CLOSE_DIAGNOSTIC" * 64)


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize(
    "content", [b"complete line\n", b"partial line"], ids=["complete", "partial"]
)
@pytest.mark.parametrize("closed_before_failure", [False, True])
def test_close_failure_settles_reader_with_truthful_bounded_metadata(
    monkeypatch,
    private,
    content,
    closed_before_failure,
):
    pipe = CloseFailurePipe(content, close_before_failure=closed_before_failure)
    host = CountedHost()
    failures = []
    monkeypatch.setattr(sys, "stdout", host)
    monkeypatch.setattr(
        threading, "excepthook", lambda event: failures.append(event.exc_value)
    )
    reader = BoundedStreamReader(
        stream=OutputStream.STDOUT,
        pipe=pipe,
        limits=LIMITS,
        private_output=private,
    )
    try:
        thread = threading.Thread(target=reader.run)
        thread.start()
        thread.join()
        snapshot = reader.snapshot()
        assert failures == [], (
            "reader cleanup exception escaped to threading.excepthook"
        )
        assert snapshot.reader_done, "close failure prevented reader settlement"
        assert snapshot.pipe_closed is pipe.closed is closed_before_failure
        assert pipe.close_count == 1 and len(pipe.requested) == 2
        if private:
            assert snapshot.read_error == "private output close failed"
            assert "SYNTHETIC_PRIVATE" not in repr(snapshot)
            assert snapshot.lines == () and host.write_count == 0
        else:
            assert (
                "close failed" in snapshot.read_error
                and "OSError" in snapshot.read_error
            )
            assert (
                len(snapshot.read_error.encode("utf-8"))
                <= LIMITS.maximum_logical_line_bytes
            )
            assert snapshot.lines == (content.rstrip(b"\n"),)
            assert host.content == content
    finally:
        io.BytesIO.close(pipe)


class CountedReadFailure(OSError):
    rendered = 0

    def __str__(self):
        type(self).rendered += 1
        return super().__str__()


@pytest.mark.parametrize(
    "message", ["diagnostic" * 512, "🚫" * 512], ids=["ascii", "unicode"]
)
def test_public_read_error_retention_and_formatting_work_are_bounded(
    monkeypatch, message
):
    class BrokenPipe(CountedPipe):
        def read(self, size=-1):
            self.requested.append(size)
            raise CountedReadFailure(message)

    CountedReadFailure.rendered = 0
    pipe = BrokenPipe(b"")
    reader = BoundedStreamReader(stream=OutputStream.STDOUT, pipe=pipe, limits=LIMITS)
    reader.run()
    snapshot = reader.snapshot()
    assert len(pipe.requested) == 1 and pipe.close_count == 1
    assert snapshot.reader_done and snapshot.pipe_closed
    assert snapshot.read_error.startswith("CountedReadFailure:")
    assert len(snapshot.read_error.encode("utf-8")) <= LIMITS.maximum_logical_line_bytes
    assert CountedReadFailure.rendered == 0, (
        "unbounded exception formatting was invoked"
    )


@pytest.mark.parametrize("private", [False, True])
def test_successful_close_return_cannot_claim_an_open_pipe_is_closed(private):
    class NoopClosePipe(CountedPipe):
        def close(self):
            self.close_count += 1

    pipe = NoopClosePipe(b"")
    reader = BoundedStreamReader(
        stream=OutputStream.STDOUT,
        pipe=pipe,
        limits=LIMITS,
        private_output=private,
    )
    try:
        reader.run()
        snapshot = reader.snapshot()
        assert snapshot.reader_done and not snapshot.pipe_closed and not pipe.closed
        assert snapshot.read_error == (
            "private output close failed" if private else "output pipe did not close"
        )
    finally:
        io.BytesIO.close(pipe)


@pytest.mark.parametrize("close_error", [False, True])
@pytest.mark.parametrize("private", [False, True])
def test_unavailable_pipe_state_is_reported_without_leaking_or_faking_closure(
    close_error,
    private,
):
    class UnknownClosedState:
        def read(self, size):
            return b""

        def close(self):
            if close_error:
                raise OSError("SYNTHETIC_PRIVATE_CLOSE_DIAGNOSTIC")

        @property
        def closed(self):
            raise ValueError("SYNTHETIC_PRIVATE_STATE_DIAGNOSTIC")

    reader = BoundedStreamReader(
        stream=OutputStream.STDOUT,
        pipe=UnknownClosedState(),
        limits=LIMITS,
        private_output=private,
    )
    reader.run()
    snapshot = reader.snapshot()
    assert snapshot.reader_done and snapshot.pipe_closed is False
    if private:
        assert snapshot.read_error == "private output close failed"
        assert "SYNTHETIC_PRIVATE" not in repr(snapshot)
    else:
        assert snapshot.read_error.startswith("pipe close failed:")
        assert ("OSError" if close_error else "ValueError") in snapshot.read_error
        assert len(snapshot.read_error.encode()) <= LIMITS.maximum_logical_line_bytes


@pytest.mark.parametrize("arguments", [(22, "fixture read error"), (object(),)])
def test_public_read_diagnostics_never_render_opaque_exception_arguments(arguments):
    class BrokenPipe(CountedPipe):
        def read(self, size):
            self.requested.append(size)
            raise OSError(*arguments)

    pipe = BrokenPipe(b"")
    reader = BoundedStreamReader(stream=OutputStream.STDOUT, pipe=pipe, limits=LIMITS)
    reader.run()
    snapshot = reader.snapshot()
    assert snapshot.read_error.startswith("OSError")
    assert "object at" not in snapshot.read_error
    assert snapshot.reader_done and snapshot.pipe_closed and len(pipe.requested) == 1


@pytest.mark.parametrize("private", [False, True])
def test_real_subprocess_output_settles_with_private_policy(monkeypatch, private):
    stdout = CountedHost()
    stderr = CountedHost()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    supervisor = ProcessSupervisor(limits=LIMITS)
    process = supervisor.spawn(
        process_id=uuid4(),
        owner=ProcessOwner("acceptance", "synthetic-child"),
        argv=(
            sys.executable,
            "-c",
            (
                "import os; os.write(1, b'SYNTHETIC_PRIVATE_STDOUT\\n'); "
                "os.write(2, b'SYNTHETIC_PRIVATE_STDERR\\n')"
            ),
        ),
        private_output=private,
    )
    snapshot = process.wait()
    assert snapshot.exit_code == 0 and snapshot.readers_joined and snapshot.pipes_closed
    assert snapshot.cleanup_error is None and snapshot.process_tree_terminated
    for stream, host, label in (
        (snapshot.stdout, stdout, b"SYNTHETIC_PRIVATE_STDOUT"),
        (snapshot.stderr, stderr, b"SYNTHETIC_PRIVATE_STDERR"),
    ):
        assert stream.reader_done and stream.pipe_closed and stream.read_error is None
        assert stream.total_bytes == len(label) + 1
        if private:
            assert (
                stream.lines == ()
                and stream.retained_bytes == 0
                and host.write_count == 0
            )
        else:
            assert stream.lines == (label,) and host.content == label + b"\n"
