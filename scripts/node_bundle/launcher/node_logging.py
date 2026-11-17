"""Captures the node's stdout and stderr into a size-rotated log file with secrets removed.

The launcher starts it before the orchestrator runs, so the log keeps filling while the supervising app is closed and the node keeps running.
"""

import errno
import os
import re
import threading
from pathlib import Path

SECRET_NAME = re.compile(r"(^|_)(KEY|SECRET|TOKEN|PASSWORD|CREDENTIALS?)($|_)", re.IGNORECASE)
BEARER = re.compile(r"(?i)\b(bearer|dpop)\s+[A-Za-z0-9._~+/=-]{8,}")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
ASSIGNMENT = re.compile(r"(?i)\b([A-Za-z0-9_]*(?:key|secret|token|password|credential)[A-Za-z0-9_]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)")
MASK = "[redacted]"
MIN_SECRET_LENGTH = 6


def secret_values(environment: dict[str, str]) -> list[str]:
    values = {value for name, value in environment.items() if SECRET_NAME.search(name) and len(value) >= MIN_SECRET_LENGTH}
    return sorted(values, key=len, reverse=True)


def redact(line: str, secrets: list[str]) -> str:
    for value in secrets:
        line = line.replace(value, MASK)
    line = JWT.sub(MASK, line)
    line = BEARER.sub(lambda match: f"{match.group(1)} {MASK}", line)
    return ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}{MASK}", line)


class RotatingFile:
    def __init__(self, directory: Path, name: str, max_bytes: int, backups: int) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / name
        self.max_bytes = max_bytes
        self.backups = backups
        self.failures: list[str] = []
        self.reported = 0
        self.handle = self._open()

    def _open(self):
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8", errors="replace", buffering=1)

    def write(self, text: str) -> None:
        try:
            if self.handle is None:
                self.handle = self._open()
            if self.handle.tell() + len(text.encode("utf-8", "replace")) > self.max_bytes:
                self.rotate()
            self.handle.write(text)
            if len(self.failures) > self.reported:
                self.handle.write(f"[log write failed: {', '.join(self.failures[self.reported:])}]\n")
                self.reported = len(self.failures)
        except OSError as error:
            code = errno.errorcode.get(error.errno or 0, "EIO")
            if code not in self.failures:
                self.failures.append(code)

    def rotate(self) -> None:
        handle, self.handle = self.handle, None
        handle.close()
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else self.path.with_name(f"{self.path.name}.{index - 1}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{index}"))
        self.handle = self._open()


def capture_output(directory: Path, secrets: list[str], max_bytes: int = 2 * 1024 * 1024, backups: int = 4) -> RotatingFile:
    sink = RotatingFile(directory, "node.log", max_bytes, backups)
    read_end, write_end = os.pipe()
    os.dup2(write_end, 1)
    os.dup2(write_end, 2)
    os.close(write_end)

    def pump() -> None:
        with os.fdopen(read_end, "r", encoding="utf-8", errors="replace") as source:
            for line in source:
                sink.write(redact(line, secrets))

    threading.Thread(target=pump, name="node-log", daemon=True).start()
    return sink
