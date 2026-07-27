"""Bounded, tree-aware supervision for backend-owned child processes.

Every child receives one continuous fixed-size reader per output pipe.  Output is
diagnostic only: complete logical lines are retained in per-stream rings while
old data and overlong-line suffixes are counted and discarded.  Each chunk is
also forwarded verbatim to the supervisor's own matching stream, so ``docker
logs`` reflects the running children instead of only the supervisor's ~22
lines.  Termination uses one total deadline for the process tree, both
readers, and both pipes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence


class OutputStream(str, Enum):
    """A supervised diagnostic output stream."""

    STDOUT = "stdout"
    STDERR = "stderr"


class ProcessState(str, Enum):
    """Lifecycle state published only after its required cleanup boundary."""

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    EXITED = "exited"
    FAILED = "failed"
    KILLED = "killed"


class TerminationReason(str, Enum):
    """Server-owned reasons that require complete process-tree cleanup."""

    STOP = "stop"
    QUIT = "quit"
    CANCEL = "cancel"
    FAILURE = "failure"


@dataclass(frozen=True)
class ProcessSupervisionLimits:
    """Immutable resource and cleanup bounds for every supervised child."""

    read_chunk_bytes: int = 16 * 1024
    maximum_logical_line_bytes: int = 64 * 1024
    ring_capacity_bytes_per_stream: int = 256 * 1024
    ring_capacity_bytes_per_process: int = 512 * 1024
    termination_deadline_seconds: float = 5.0

    def __post_init__(self) -> None:
        values = (
            self.read_chunk_bytes,
            self.maximum_logical_line_bytes,
            self.ring_capacity_bytes_per_stream,
            self.ring_capacity_bytes_per_process,
        )
        if any(value <= 0 for value in values):
            raise ValueError("process-supervision byte limits must be positive")
        if self.termination_deadline_seconds <= 0:
            raise ValueError("process-supervision deadline must be positive")
        if (
            self.ring_capacity_bytes_per_stream * 2
            > self.ring_capacity_bytes_per_process
        ):
            raise ValueError("per-stream rings exceed the per-process output bound")


DEFAULT_PROCESS_SUPERVISION_LIMITS = ProcessSupervisionLimits()


@dataclass(frozen=True)
class ProcessOwner:
    """Logical owner of a child process."""

    owner_kind: str
    owner_id: str

    def __post_init__(self) -> None:
        if not self.owner_kind or not self.owner_id:
            raise ValueError("process owner kind and id are required")


@dataclass(frozen=True)
class StreamSnapshot:
    """Immutable bounded stream diagnostics."""

    stream: OutputStream
    lines: tuple[bytes, ...]
    total_bytes: int
    total_lines: int
    retained_bytes: int
    dropped_bytes: int
    dropped_lines: int
    overlong_lines: int
    maximum_retained_line_bytes: int
    reader_done: bool
    pipe_closed: bool
    read_error: str | None


@dataclass(frozen=True)
class ProcessSnapshot:
    """Immutable child state after, or during, supervision."""

    process_id: uuid.UUID
    owner: ProcessOwner
    pid: int
    state: ProcessState
    exit_code: int | None
    termination_reason: TerminationReason | None
    stdout: StreamSnapshot
    stderr: StreamSnapshot
    readers_joined: bool
    pipes_closed: bool
    process_tree_terminated: bool
    cleanup_duration_seconds: float
    cleanup_error: str | None


class BoundedStreamReader:
    """Continuously consume one binary pipe using only fixed 16 KiB reads."""

    def __init__(
        self,
        *,
        stream: OutputStream,
        pipe: BinaryIO,
        limits: ProcessSupervisionLimits = DEFAULT_PROCESS_SUPERVISION_LIMITS,
    ) -> None:
        self.stream = stream
        self._pipe = pipe
        self._limits = limits
        self._condition = threading.Condition(threading.RLock())
        self._lines: deque[tuple[bytes, int]] = deque()
        self._partial = bytearray()
        self._partial_overlong = False
        self._total_bytes = 0
        self._total_lines = 0
        self._retained_bytes = 0
        self._dropped_bytes = 0
        self._dropped_lines = 0
        self._overlong_lines = 0
        self._maximum_retained_line_bytes = 0
        self._reader_done = False
        self._pipe_closed = False
        self._read_error: str | None = None

    def _append_segment(self, segment: bytes) -> None:
        available = self._limits.maximum_logical_line_bytes - len(self._partial)
        if available > 0:
            self._partial.extend(segment[:available])
        discarded = len(segment) - max(available, 0)
        if discarded > 0:
            self._dropped_bytes += discarded
            self._partial_overlong = True

    def _finish_line(self, *, delimiter_bytes: int) -> None:
        line = bytes(self._partial)
        storage_bytes = len(line) + delimiter_bytes
        self._total_lines += 1
        if self._partial_overlong:
            self._overlong_lines += 1
        self._maximum_retained_line_bytes = max(
            self._maximum_retained_line_bytes, len(line)
        )
        while (
            self._lines
            and self._retained_bytes + storage_bytes
            > self._limits.ring_capacity_bytes_per_stream
        ):
            _, removed_bytes = self._lines.popleft()
            self._retained_bytes -= removed_bytes
            self._dropped_bytes += removed_bytes
            self._dropped_lines += 1
        if storage_bytes <= self._limits.ring_capacity_bytes_per_stream:
            self._lines.append((line, storage_bytes))
            self._retained_bytes += storage_bytes
        else:  # Defensive for non-default custom limits.
            self._dropped_bytes += storage_bytes
            self._dropped_lines += 1
        self._partial.clear()
        self._partial_overlong = False
        self._condition.notify_all()

    def _consume(self, chunk: bytes) -> None:
        self._total_bytes += len(chunk)
        cursor = 0
        while cursor < len(chunk):
            newline = chunk.find(b"\n", cursor)
            if newline < 0:
                self._append_segment(chunk[cursor:])
                break
            self._append_segment(chunk[cursor:newline])
            self._finish_line(delimiter_bytes=1)
            cursor = newline + 1

    def close_pipe(self) -> None:
        """Idempotently close the owned pipe and publish that fact."""

        with self._condition:
            if self._pipe_closed:
                return
            try:
                self._pipe.close()
            finally:
                self._pipe_closed = True
                self._condition.notify_all()

    def _forward_chunk(self, chunk: bytes) -> None:
        """Tee one chunk to the supervisor's matching stream, fail-open.

        Forwarding is a straight passthrough of the fixed-size read — nothing
        accumulates — so the bounded-memory contract of the ring is untouched.
        A broken host stream must never take the reader (or the child) down.
        """

        try:
            host = sys.stdout if self.stream is OutputStream.STDOUT else sys.stderr
            buffer = getattr(host, "buffer", None)
            if buffer is not None:
                buffer.write(chunk)
            else:  # Text-only host stream (tests may swap in StringIO).
                host.write(chunk.decode("utf-8", errors="replace"))
            host.flush()
        except (OSError, ValueError, AttributeError):
            pass

    def run(self) -> None:
        """Consume until EOF, finalize a trailing line, and close the pipe."""

        try:
            while True:
                chunk = self._pipe.read(self._limits.read_chunk_bytes)
                if not chunk:
                    break
                self._forward_chunk(chunk)
                with self._condition:
                    self._consume(chunk)
        except (OSError, ValueError) as exc:
            with self._condition:
                if not self._pipe_closed:
                    self._read_error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._condition:
                if self._partial or self._partial_overlong:
                    self._finish_line(delimiter_bytes=0)
            self.close_pipe()
            with self._condition:
                self._reader_done = True
                self._condition.notify_all()

    def wait_for_line(self, *, prefix: bytes, timeout: float) -> bytes:
        """Return the first currently retained line with ``prefix``."""

        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for line, _ in self._lines:
                    if line.startswith(prefix):
                        return line
                if self._reader_done:
                    raise EOFError(f"{self.stream.value} reached EOF before {prefix!r}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out waiting for {prefix!r} on {self.stream.value}"
                    )
                self._condition.wait(remaining)

    def snapshot(self) -> StreamSnapshot:
        """Return one immutable view without exposing the mutable ring."""

        with self._condition:
            return StreamSnapshot(
                stream=self.stream,
                lines=tuple(line for line, _ in self._lines),
                total_bytes=self._total_bytes,
                total_lines=self._total_lines,
                retained_bytes=self._retained_bytes,
                dropped_bytes=self._dropped_bytes,
                dropped_lines=self._dropped_lines,
                overlong_lines=self._overlong_lines,
                maximum_retained_line_bytes=self._maximum_retained_line_bytes,
                reader_done=self._reader_done,
                pipe_closed=self._pipe_closed,
                read_error=self._read_error,
            )


class SupervisedProcess:
    """One child process, its process tree, and its continuous pipe readers."""

    def __init__(
        self,
        *,
        process_id: uuid.UUID,
        owner: ProcessOwner,
        process: subprocess.Popen[bytes],
        stdout_reader: BoundedStreamReader,
        stderr_reader: BoundedStreamReader,
        limits: ProcessSupervisionLimits,
    ) -> None:
        self.process_id = process_id
        self.owner = owner
        self._process = process
        self._limits = limits
        self._readers = {
            OutputStream.STDOUT: stdout_reader,
            OutputStream.STDERR: stderr_reader,
        }
        self._reader_threads = {
            stream: threading.Thread(
                target=reader.run,
                name=f"process-{process_id}-{stream.value}",
                daemon=True,
            )
            for stream, reader in self._readers.items()
        }
        self._cleanup_lock = threading.RLock()
        self._termination_reason: TerminationReason | None = None
        self._terminal_snapshot: ProcessSnapshot | None = None
        self._cleanup_started: float | None = None
        for thread in self._reader_threads.values():
            thread.start()
        self._monitor_thread = threading.Thread(
            target=self._monitor_exit,
            name=f"process-{process_id}-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def poll(self) -> int | None:
        """Compatibility poll for existing readiness and lifecycle code."""

        return self._process.poll()

    def _windows_taskkill(self, *, force: bool, timeout: float) -> None:
        command = ["taskkill", "/T", "/PID", str(self.pid)]
        if force:
            command.insert(1, "/F")
        try:
            subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(timeout, 0.05),
            )
        except (OSError, subprocess.TimeoutExpired):
            return

    def _signal_tree(self, *, force: bool, reason: TerminationReason) -> None:
        if os.name == "posix":
            selected = (
                signal.SIGKILL
                if force
                else (
                    signal.SIGINT
                    if reason is TerminationReason.QUIT
                    else signal.SIGTERM
                )
            )
            try:
                os.killpg(self.pid, selected)
            except ProcessLookupError:
                return
            return
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            if not force and reason is TerminationReason.QUIT:
                try:
                    self._process.send_signal(signal.CTRL_BREAK_EVENT)
                    return
                except (OSError, ValueError):
                    pass
            self._windows_taskkill(
                force=force,
                timeout=self._limits.termination_deadline_seconds / 3,
            )
            return
        try:  # pragma: no cover - unsupported fallback
            self._process.kill() if force else self._process.terminate()
        except OSError:
            return

    def process_tree_alive(self) -> bool:
        """Return whether the supervised process group/tree still exists."""

        if os.name == "posix":
            try:
                os.killpg(self.pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True
        return self._process.poll() is None

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _wait_for_tree(self, deadline: float) -> None:
        while self.process_tree_alive() and self._remaining(deadline) > 0.05:
            time.sleep(min(0.01, self._remaining(deadline)))

    def _join_readers(self, deadline: float) -> None:
        for thread in self._reader_threads.values():
            thread.join(self._remaining(deadline))

    def _close_pipes(self) -> None:
        for reader in self._readers.values():
            reader.close_pipe()

    def _state(self) -> ProcessState:
        returncode = self._process.poll()
        if returncode is None:
            return (
                ProcessState.STOPPING
                if self._termination_reason is not None
                else ProcessState.RUNNING
            )
        if self._termination_reason is TerminationReason.FAILURE:
            return ProcessState.FAILED
        if self._termination_reason is not None:
            return ProcessState.KILLED
        return ProcessState.EXITED if returncode == 0 else ProcessState.FAILED

    def _monitor_exit(self) -> None:
        """Settle output and any surviving descendants after an unobserved exit."""

        self._process.wait()
        deadline = time.monotonic() + self._limits.termination_deadline_seconds
        if self.process_tree_alive():
            self._begin_termination(TerminationReason.FAILURE)
        self._complete_cleanup(
            deadline=deadline,
            reason=self._termination_reason or TerminationReason.FAILURE,
        )

    def snapshot(self) -> ProcessSnapshot:
        """Return current or terminal state without waiting."""

        if self._terminal_snapshot is not None:
            return self._terminal_snapshot
        stdout = self._readers[OutputStream.STDOUT].snapshot()
        stderr = self._readers[OutputStream.STDERR].snapshot()
        readers_joined = all(
            not thread.is_alive() for thread in self._reader_threads.values()
        )
        pipes_closed = stdout.pipe_closed and stderr.pipe_closed
        return ProcessSnapshot(
            process_id=self.process_id,
            owner=self.owner,
            pid=self.pid,
            state=self._state(),
            exit_code=self._process.poll(),
            termination_reason=self._termination_reason,
            stdout=stdout,
            stderr=stderr,
            readers_joined=readers_joined,
            pipes_closed=pipes_closed,
            process_tree_terminated=not self.process_tree_alive(),
            cleanup_duration_seconds=(
                0.0
                if self._cleanup_started is None
                else time.monotonic() - self._cleanup_started
            ),
            cleanup_error=None,
        )

    def _begin_termination(self, reason: TerminationReason) -> None:
        with self._cleanup_lock:
            if self._terminal_snapshot is not None:
                return
            self._termination_reason = reason
            self._cleanup_started = self._cleanup_started or time.monotonic()
            self._signal_tree(force=False, reason=reason)

    def _complete_cleanup(
        self, *, deadline: float, reason: TerminationReason
    ) -> ProcessSnapshot:
        with self._cleanup_lock:
            if self._terminal_snapshot is not None:
                return self._terminal_snapshot
            self._cleanup_started = self._cleanup_started or time.monotonic()

            grace_deadline = min(deadline, time.monotonic() + 1.0)
            try:
                self._process.wait(timeout=self._remaining(grace_deadline))
            except subprocess.TimeoutExpired:
                pass
            self._wait_for_tree(grace_deadline)

            if self.process_tree_alive():
                self._signal_tree(force=True, reason=reason)
            try:
                self._process.wait(timeout=self._remaining(deadline))
            except subprocess.TimeoutExpired:
                pass
            self._wait_for_tree(deadline)
            self._join_readers(deadline)
            self._close_pipes()
            self._join_readers(deadline)

            stdout = self._readers[OutputStream.STDOUT].snapshot()
            stderr = self._readers[OutputStream.STDERR].snapshot()
            readers_joined = all(
                not thread.is_alive() for thread in self._reader_threads.values()
            )
            pipes_closed = stdout.pipe_closed and stderr.pipe_closed
            tree_terminated = not self.process_tree_alive()
            failures = []
            if not tree_terminated:
                failures.append("process tree remains alive")
            if not readers_joined:
                failures.append("output readers did not join")
            if not pipes_closed:
                failures.append("output pipes did not close")
            duration = time.monotonic() - self._cleanup_started
            self._terminal_snapshot = ProcessSnapshot(
                process_id=self.process_id,
                owner=self.owner,
                pid=self.pid,
                state=self._state(),
                exit_code=self._process.poll(),
                termination_reason=self._termination_reason,
                stdout=stdout,
                stderr=stderr,
                readers_joined=readers_joined,
                pipes_closed=pipes_closed,
                process_tree_terminated=tree_terminated,
                cleanup_duration_seconds=duration,
                cleanup_error="; ".join(failures) or None,
            )
            return self._terminal_snapshot

    def wait(self, timeout: float | None = None) -> ProcessSnapshot:
        """Wait for exit, then settle descendants, readers, and pipes."""

        if self._terminal_snapshot is not None:
            return self._terminal_snapshot
        self._process.wait(timeout=timeout)
        started = time.monotonic()
        deadline = started + self._limits.termination_deadline_seconds
        if self.process_tree_alive():
            self._begin_termination(TerminationReason.FAILURE)
        return self._complete_cleanup(
            deadline=deadline,
            reason=self._termination_reason or TerminationReason.FAILURE,
        )

    def wait_for_line(
        self,
        stream: OutputStream,
        *,
        prefix: bytes,
        timeout: float,
    ) -> bytes:
        """Wait for a bounded diagnostic line from one continuously read pipe."""

        return self._readers[stream].wait_for_line(prefix=prefix, timeout=timeout)

    def terminate(self, *, reason: TerminationReason) -> ProcessSnapshot:
        """Terminate the complete tree within one total cleanup deadline."""

        started = time.monotonic()
        deadline = started + self._limits.termination_deadline_seconds
        self._begin_termination(reason)
        return self._complete_cleanup(deadline=deadline, reason=reason)


class ProcessSupervisor:
    """Factory and owner-level cleanup boundary for supervised children."""

    def __init__(
        self,
        *,
        limits: ProcessSupervisionLimits = DEFAULT_PROCESS_SUPERVISION_LIMITS,
    ) -> None:
        self.limits = limits
        self._lock = threading.RLock()
        self._processes: dict[uuid.UUID, SupervisedProcess] = {}

    def spawn(
        self,
        *,
        process_id: uuid.UUID,
        owner: ProcessOwner,
        argv: Sequence[str | os.PathLike[str]],
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        **popen_kwargs: Any,
    ) -> SupervisedProcess:
        """Spawn one isolated process group with continuous bounded readers."""

        if not isinstance(process_id, uuid.UUID):
            raise TypeError("process_id must be a UUID")
        command = tuple(os.fspath(value) for value in argv)
        if not command or any(not value for value in command):
            raise ValueError("argv must contain non-empty arguments")
        forbidden = {
            "args",
            "shell",
            "stdout",
            "stderr",
            "stdin",
            "text",
            "encoding",
            "errors",
            "universal_newlines",
            "start_new_session",
        }
        overlap = forbidden & set(popen_kwargs)
        if overlap:
            raise ValueError(f"supervisor owns Popen options: {sorted(overlap)}")
        with self._lock:
            if process_id in self._processes:
                raise ValueError(f"duplicate supervised process id {process_id}")

        platform_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            platform_kwargs["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover - exercised on Windows CI
            creationflags = int(popen_kwargs.pop("creationflags", 0))
            platform_kwargs["creationflags"] = (
                creationflags | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        process = subprocess.Popen(
            command,
            cwd=Path(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            **platform_kwargs,
            **popen_kwargs,
        )
        if process.stdout is None or process.stderr is None:  # pragma: no cover
            process.kill()
            raise RuntimeError("supervised child pipes were not created")
        supervised = SupervisedProcess(
            process_id=process_id,
            owner=owner,
            process=process,
            stdout_reader=BoundedStreamReader(
                stream=OutputStream.STDOUT,
                pipe=process.stdout,
                limits=self.limits,
            ),
            stderr_reader=BoundedStreamReader(
                stream=OutputStream.STDERR,
                pipe=process.stderr,
                limits=self.limits,
            ),
            limits=self.limits,
        )
        with self._lock:
            self._processes[process_id] = supervised
        return supervised

    def terminate(
        self, process_id: uuid.UUID, *, reason: TerminationReason
    ) -> ProcessSnapshot:
        """Terminate one registered child."""

        with self._lock:
            process = self._processes[process_id]
        return process.terminate(reason=reason)

    def terminate_all(
        self, *, reason: TerminationReason = TerminationReason.QUIT
    ) -> tuple[ProcessSnapshot, ...]:
        """Terminate every child under one shared five-second deadline."""

        started = time.monotonic()
        deadline = started + self.limits.termination_deadline_seconds
        with self._lock:
            processes = tuple(self._processes.values())
        for process in processes:
            process._begin_termination(reason)
        return tuple(
            process._complete_cleanup(deadline=deadline, reason=reason)
            for process in processes
        )

    def snapshots(self) -> tuple[ProcessSnapshot, ...]:
        """Return immutable snapshots in deterministic logical-ID order."""

        with self._lock:
            processes = sorted(
                self._processes.values(), key=lambda process: str(process.process_id)
            )
        return tuple(process.snapshot() for process in processes)
