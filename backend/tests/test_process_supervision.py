"""Feature-060 conformance tests for the backend child-process supervisor."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from astralprojection.resources import fixture_path
from shared.process_supervision import (
    DEFAULT_PROCESS_SUPERVISION_LIMITS,
    BoundedStreamReader,
    OutputStream,
    ProcessOwner,
    ProcessSupervisionLimits,
    ProcessState,
    ProcessSupervisor,
    TerminationReason,
)


_VECTOR_PATH = fixture_path(
    "runtime_reliability_060/process-supervision-vectors.json"
)
_CORPUS = json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))
_VECTORS = {item["id"]: item for item in _CORPUS["vectors"]}
_CONSUMED_VECTOR_IDS = {
    "dual-stream-high-output",
    "oversized-logical-line",
    "one-pipe-eof-other-continues",
    "descendant-tree-stop",
    "descendant-tree-quit",
    "crash-after-output",
    "silent-tree-cancellation",
}


class _RecordingPipe(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.requested_sizes: list[int] = []
        self.close_called = False

    def read(self, size: int = -1) -> bytes:
        self.requested_sizes.append(size)
        return super().read(size)

    def close(self) -> None:
        self.close_called = True
        super().close()


def _owner(vector_id: str) -> ProcessOwner:
    return ProcessOwner(owner_kind="test_vector", owner_id=vector_id)


def _spawn_script(supervisor: ProcessSupervisor, vector_id: str, script: str):
    return supervisor.spawn(
        process_id=uuid.uuid4(),
        owner=_owner(vector_id),
        argv=(sys.executable, "-u", "-c", script),
    )


def _emit_then_exit_script(vector: dict) -> str:
    behavior = vector["behavior"]
    stdout = behavior["stdout"]
    stderr = behavior["stderr"]
    return (
        "import os\n"
        f"stdout_line = b'O' * {stdout['line_bytes']} + b'\\n'\n"
        f"stderr_line = b'E' * {stderr['line_bytes']} + b'\\n'\n"
        f"for _ in range({stdout['line_count']}): os.write(1, stdout_line)\n"
        f"for _ in range({stderr['line_count']}): os.write(2, stderr_line)\n"
        f"raise SystemExit({behavior['exit_code']})\n"
    )


def _tree_script(vector: dict) -> str:
    behavior = vector["behavior"]
    period_seconds = behavior.get("period_ms", 10) / 1000
    descendant = (
        "import signal,time\n"
        "signal.signal(signal.SIGTERM, lambda *_: raise SystemExit(0))\n"
        "signal.signal(signal.SIGINT, lambda *_: raise SystemExit(0))\n"
        "while True: time.sleep(0.05)\n"
    )
    periodic = behavior["parent_mode"] == "periodic_output"
    return (
        "import os,signal,subprocess,sys,time\n"
        f"child = subprocess.Popen([sys.executable, '-u', '-c', {descendant!r}])\n"
        "def shutdown(*_):\n"
        "    try:\n"
        "        child.wait(timeout=1.0)\n"
        "    except Exception:\n"
        "        pass\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, shutdown)\n"
        "signal.signal(signal.SIGINT, shutdown)\n"
        "os.write(1, f'READY {child.pid}\\n'.encode())\n"
        "while True:\n"
        + (
            "    os.write(1, b'parent-tick\\n')\n    os.write(2, b'parent-tick\\n')\n"
            if periodic
            else ""
        )
        + f"    time.sleep({period_seconds!r})\n"
    )


def _assert_complete_cleanup(snapshot) -> None:
    assert snapshot.readers_joined is True
    assert snapshot.pipes_closed is True
    assert snapshot.stdout.reader_done is True
    assert snapshot.stderr.reader_done is True
    assert snapshot.stdout.pipe_closed is True
    assert snapshot.stderr.pipe_closed is True


def test_neutral_fixture_and_default_limits_are_one_contract() -> None:
    assert _CORPUS["schema_version"] == 1
    assert _CORPUS["contract"] == "astraldeep-process-supervision-060"
    assert set(_VECTORS) == _CONSUMED_VECTOR_IDS
    limits = _CORPUS["limits"]

    assert (
        DEFAULT_PROCESS_SUPERVISION_LIMITS.read_chunk_bytes
        == limits["read_chunk_bytes"]
    )
    assert (
        DEFAULT_PROCESS_SUPERVISION_LIMITS.maximum_logical_line_bytes
        == limits["maximum_logical_line_bytes"]
    )
    assert (
        DEFAULT_PROCESS_SUPERVISION_LIMITS.ring_capacity_bytes_per_stream
        == limits["ring_capacity_bytes_per_stream"]
    )
    assert (
        DEFAULT_PROCESS_SUPERVISION_LIMITS.ring_capacity_bytes_per_process
        == limits["ring_capacity_bytes_per_process"]
    )
    assert DEFAULT_PROCESS_SUPERVISION_LIMITS.termination_deadline_seconds == (
        limits["termination_deadline_ms"] / 1000
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_PROCESS_SUPERVISION_LIMITS.read_chunk_bytes = 1  # type: ignore[misc]


def test_reader_uses_only_fixed_16kib_reads_and_closes_its_pipe() -> None:
    limits = DEFAULT_PROCESS_SUPERVISION_LIMITS
    payload = (b"line\n" * ((limits.read_chunk_bytes * 3) // 5)) + b"tail"
    pipe = _RecordingPipe(payload)
    reader = BoundedStreamReader(
        stream=OutputStream.STDOUT,
        pipe=pipe,
        limits=limits,
    )

    reader.run()
    snapshot = reader.snapshot()

    assert pipe.requested_sizes
    assert set(pipe.requested_sizes) == {limits.read_chunk_bytes}
    assert snapshot.total_bytes == len(payload)
    assert snapshot.reader_done is True
    assert snapshot.pipe_closed is True
    assert pipe.close_called is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"read_chunk_bytes": 0},
        {"termination_deadline_seconds": 0},
        {
            "ring_capacity_bytes_per_stream": 2,
            "ring_capacity_bytes_per_process": 3,
        },
    ],
)
def test_limits_and_owner_reject_invalid_contract_values(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        ProcessSupervisionLimits(**kwargs)
    with pytest.raises(ValueError):
        ProcessOwner(owner_kind="", owner_id="missing-kind")


def test_reader_forwards_child_output_to_supervisor_stream(monkeypatch) -> None:
    """docker-logs observability: every consumed chunk is teed verbatim to the
    supervisor's matching stream, while the bounded ring fills unchanged."""

    host = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", host)
    payload = b"hello from the child\nsecond line\n"
    reader = BoundedStreamReader(
        stream=OutputStream.STDOUT,
        pipe=_RecordingPipe(payload),
        limits=DEFAULT_PROCESS_SUPERVISION_LIMITS,
    )

    reader.run()

    assert host.buffer.getvalue() == payload
    snapshot = reader.snapshot()
    assert snapshot.lines == (b"hello from the child", b"second line")
    assert snapshot.dropped_bytes == 0


def test_reader_forwarding_failure_never_breaks_the_ring(monkeypatch) -> None:
    """A broken host stream must not take the reader (or the child) down."""

    class _BrokenHost:
        def write(self, _data) -> int:
            raise OSError("host stream gone")

        def flush(self) -> None:
            raise OSError("host stream gone")

    monkeypatch.setattr(sys, "stderr", _BrokenHost())
    reader = BoundedStreamReader(
        stream=OutputStream.STDERR,
        pipe=_RecordingPipe(b"err line\n"),
        limits=DEFAULT_PROCESS_SUPERVISION_LIMITS,
    )

    reader.run()

    snapshot = reader.snapshot()
    assert snapshot.lines == (b"err line",)
    assert snapshot.read_error is None
    assert snapshot.reader_done is True


def test_reader_reports_read_errors_eof_timeout_and_tiny_ring_drops() -> None:
    class _BrokenPipe(_RecordingPipe):
        def read(self, size: int = -1) -> bytes:
            raise OSError("fixture read failure")

    broken = BoundedStreamReader(
        stream=OutputStream.STDERR,
        pipe=_BrokenPipe(b""),
    )
    broken.run()
    assert "fixture read failure" in (broken.snapshot().read_error or "")
    with pytest.raises(EOFError):
        broken.wait_for_line(prefix=b"never", timeout=0.01)

    pending = BoundedStreamReader(
        stream=OutputStream.STDOUT,
        pipe=_RecordingPipe(b""),
    )
    with pytest.raises(TimeoutError):
        pending.wait_for_line(prefix=b"never", timeout=0.001)
    pending.close_pipe()
    pending.close_pipe()

    tiny_limits = ProcessSupervisionLimits(
        read_chunk_bytes=4,
        maximum_logical_line_bytes=8,
        ring_capacity_bytes_per_stream=4,
        ring_capacity_bytes_per_process=8,
        termination_deadline_seconds=1,
    )
    tiny = BoundedStreamReader(
        stream=OutputStream.STDOUT,
        pipe=_RecordingPipe(b"abcdef\n"),
        limits=tiny_limits,
    )
    tiny.run()
    tiny_snapshot = tiny.snapshot()
    assert tiny_snapshot.lines == ()
    assert tiny_snapshot.dropped_lines == 1


def test_stream_ring_discards_oldest_complete_lines() -> None:
    limits = DEFAULT_PROCESS_SUPERVISION_LIMITS
    line_bytes = 128
    line_count = (limits.ring_capacity_bytes_per_stream // line_bytes) + 128
    lines = [
        f"{index:06d}:".encode("ascii") + b"X" * (line_bytes - 7)
        for index in range(line_count)
    ]
    pipe = _RecordingPipe(b"\n".join(lines) + b"\n")
    reader = BoundedStreamReader(
        stream=OutputStream.STDERR,
        pipe=pipe,
        limits=limits,
    )

    reader.run()
    snapshot = reader.snapshot()

    assert snapshot.retained_bytes <= limits.ring_capacity_bytes_per_stream
    assert snapshot.dropped_bytes > 0
    assert snapshot.dropped_lines > 0
    assert lines[0] not in snapshot.lines
    assert snapshot.lines[-1] == lines[-1]
    assert all(len(line) == line_bytes for line in snapshot.lines)


def test_dual_stream_high_output_is_continuous_and_ring_bounded() -> None:
    vector = _VECTORS["dual-stream-high-output"]
    behavior = vector["behavior"]
    expected = vector["expected"]
    supervisor = ProcessSupervisor()
    process = _spawn_script(
        supervisor,
        vector["id"],
        _emit_then_exit_script(vector),
    )

    snapshot = process.wait(timeout=5.0)

    assert snapshot.exit_code == expected["exit_code"]
    assert snapshot.state is ProcessState.EXITED
    for stream_name in ("stdout", "stderr"):
        stream = getattr(snapshot, stream_name)
        stream_vector = behavior[stream_name]
        assert stream.total_lines == stream_vector["line_count"]
        assert stream.total_bytes == stream_vector["line_count"] * (
            stream_vector["line_bytes"] + 1
        )
        assert stream.retained_bytes <= (
            DEFAULT_PROCESS_SUPERVISION_LIMITS.ring_capacity_bytes_per_stream
        )
        assert stream.dropped_bytes >= expected[f"{stream_name}_dropped_bytes_minimum"]
        assert stream.dropped_lines > 0
    assert snapshot.stdout.retained_bytes + snapshot.stderr.retained_bytes <= (
        DEFAULT_PROCESS_SUPERVISION_LIMITS.ring_capacity_bytes_per_process
    )
    _assert_complete_cleanup(snapshot)


def test_oversized_logical_line_is_truncated_and_counted() -> None:
    vector = _VECTORS["oversized-logical-line"]
    expected = vector["expected"]
    supervisor = ProcessSupervisor()
    process = _spawn_script(
        supervisor,
        vector["id"],
        _emit_then_exit_script(vector),
    )

    snapshot = process.wait(timeout=5.0)

    assert snapshot.exit_code == expected["exit_code"]
    assert snapshot.stdout.overlong_lines >= expected["stdout_overlong_lines_minimum"]
    assert (
        snapshot.stdout.maximum_retained_line_bytes
        <= expected["maximum_retained_line_bytes"]
    )
    assert all(
        len(line) <= DEFAULT_PROCESS_SUPERVISION_LIMITS.maximum_logical_line_bytes
        for line in snapshot.stdout.lines
    )
    _assert_complete_cleanup(snapshot)


def test_one_pipe_eof_does_not_starve_the_continuing_reader() -> None:
    vector = _VECTORS["one-pipe-eof-other-continues"]
    behavior = vector["behavior"]
    close_fd = 1 if behavior["close_stream"] == "stdout" else 2
    continuing_fd = 1 if behavior["continuing_stream"] == "stdout" else 2
    script = (
        "import os,time\n"
        f"os.close({close_fd})\n"
        f"time.sleep({behavior['delay_after_close_ms'] / 1000!r})\n"
        f"line = b'C' * {behavior['continuing_line_bytes']} + b'\\n'\n"
        f"for _ in range({behavior['continuing_line_count']}): "
        f"os.write({continuing_fd}, line)\n"
        f"os._exit({behavior['exit_code']})\n"
    )
    supervisor = ProcessSupervisor()
    process = _spawn_script(supervisor, vector["id"], script)

    snapshot = process.wait(timeout=5.0)

    continuing = getattr(snapshot, behavior["continuing_stream"])
    closed = getattr(snapshot, behavior["close_stream"])
    assert snapshot.exit_code == vector["expected"]["exit_code"]
    assert closed.reader_done is True
    assert continuing.total_lines == behavior["continuing_line_count"]
    assert continuing.total_bytes == behavior["continuing_line_count"] * (
        behavior["continuing_line_bytes"] + 1
    )
    assert continuing.lines[-1] == b"C" * behavior["continuing_line_bytes"]
    _assert_complete_cleanup(snapshot)


def test_nonzero_exit_is_failed_only_after_output_cleanup() -> None:
    vector = _VECTORS["crash-after-output"]
    supervisor = ProcessSupervisor()
    process = _spawn_script(
        supervisor,
        vector["id"],
        _emit_then_exit_script(vector),
    )

    snapshot = process.wait(timeout=5.0)

    assert snapshot.exit_code == vector["expected"]["exit_code"]
    assert snapshot.state.value == vector["expected"]["terminal_state"]
    assert snapshot.stdout.total_lines == vector["behavior"]["stdout"]["line_count"]
    assert snapshot.stderr.total_lines == vector["behavior"]["stderr"]["line_count"]
    _assert_complete_cleanup(snapshot)


def test_unobserved_exit_is_monitored_and_settled() -> None:
    supervisor = ProcessSupervisor()
    process = _spawn_script(
        supervisor,
        "unobserved-exit",
        "import os\nos.write(2, b'failure-before-exit\\n')\nraise SystemExit(17)\n",
    )
    deadline = time.monotonic() + 2
    snapshot = process.snapshot()
    while time.monotonic() < deadline:
        snapshot = process.snapshot()
        if snapshot.state is ProcessState.FAILED and snapshot.readers_joined:
            break
        time.sleep(0.01)

    assert snapshot.state is ProcessState.FAILED
    assert snapshot.exit_code == 17
    assert snapshot.stderr.lines == (b"failure-before-exit",)
    _assert_complete_cleanup(snapshot)


def test_supervisor_validates_spawn_and_owns_single_and_bulk_termination() -> None:
    supervisor = ProcessSupervisor()
    owner = _owner("supervisor-api")
    with pytest.raises(TypeError):
        supervisor.spawn(  # type: ignore[arg-type]
            process_id="not-a-uuid",
            owner=owner,
            argv=(sys.executable, "-c", "pass"),
        )
    with pytest.raises(ValueError):
        supervisor.spawn(process_id=uuid.uuid4(), owner=owner, argv=())
    with pytest.raises(ValueError):
        supervisor.spawn(
            process_id=uuid.uuid4(),
            owner=owner,
            argv=(sys.executable, "-c", "pass"),
            shell=True,
        )

    process_id = uuid.uuid4()
    first = supervisor.spawn(
        process_id=process_id,
        owner=owner,
        argv=(sys.executable, "-u", "-c", "import time; time.sleep(30)"),
    )
    with pytest.raises(ValueError):
        supervisor.spawn(
            process_id=process_id,
            owner=owner,
            argv=(sys.executable, "-c", "pass"),
        )
    assert supervisor.snapshots()[0].state is ProcessState.RUNNING
    with pytest.raises(subprocess.TimeoutExpired):
        first.wait(timeout=0.001)
    stopped = supervisor.terminate(process_id, reason=TerminationReason.CANCEL)
    assert stopped.process_tree_terminated is True

    for identifier in ("bulk-a", "bulk-b"):
        _spawn_script(
            supervisor,
            identifier,
            "import time\ntime.sleep(30)\n",
        )
    snapshots = supervisor.terminate_all(reason=TerminationReason.QUIT)
    assert len(snapshots) == 3
    assert all(snapshot.process_tree_terminated for snapshot in snapshots)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
@pytest.mark.parametrize(
    "vector_id",
    [
        "descendant-tree-stop",
        "descendant-tree-quit",
        "silent-tree-cancellation",
    ],
)
def test_termination_actions_end_the_complete_process_group(vector_id: str) -> None:
    vector = _VECTORS[vector_id]
    supervisor = ProcessSupervisor()
    process = _spawn_script(supervisor, vector_id, _tree_script(vector))
    ready = process.wait_for_line(
        OutputStream.STDOUT,
        prefix=b"READY ",
        timeout=2.0,
    )
    assert ready.startswith(b"READY ")
    assert int(ready.split()[1]) > 1

    started = time.monotonic()
    snapshot = process.terminate(
        reason=TerminationReason(vector["action"]["kind"]),
    )
    elapsed = time.monotonic() - started

    deadline = _CORPUS["limits"]["termination_deadline_ms"] / 1000
    assert snapshot.process_tree_terminated is True
    assert snapshot.cleanup_duration_seconds <= deadline
    assert elapsed <= deadline + 0.5
    assert process.process_tree_alive() is False
    _assert_complete_cleanup(snapshot)
