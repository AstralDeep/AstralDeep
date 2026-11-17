# Files from tools: a call writes into its turn's outbox, the sandbox collects what the agent allows, and the
# vessel streams each file to the caller as an A2A artifact. Every hop checks names, types and sizes again.

from __future__ import annotations

import base64
import contextlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from typing import Any

OUTBOX = "outbox"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES_PER_CALL = 4
MAX_FILES_PER_TURN = 8
MAX_TURN_BYTES = 16 * 1024 * 1024
MAX_NOTES = 10
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _text(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return b"\x00" not in data


# extension -> (media type, content check)
FILE_TYPES = {
    "png": ("image/png", lambda d: d.startswith(b"\x89PNG\r\n\x1a\n")),
    "svg": ("image/svg+xml", lambda d: _text(d) and b"<svg" in d[:4096]),
    "csv": ("text/csv", _text),
    "pdf": ("application/pdf", lambda d: d.startswith(b"%PDF-")),
}
MEDIA_TYPES = {media for media, _ in FILE_TYPES.values()}


@dataclass(frozen=True)
class ToolFile:
    name: str
    media_type: str
    data: bytes

    def wire(self) -> dict[str, str]:
        return {"name": self.name, "media_type": self.media_type, "data": base64.b64encode(self.data).decode()}


def check(name: str, data: bytes, allowed: list[str]) -> tuple[str | None, str]:
    # The media type for an acceptable file, or None and the reason it was refused.
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if not NAME.fullmatch(name) or extension not in FILE_TYPES:
        return None, "only names like chart.png with an extension from " + ", ".join(sorted(FILE_TYPES))
    if extension not in allowed:
        return None, f"this agent may not return .{extension} files"
    if len(data) > MAX_FILE_BYTES:
        return None, f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"
    media_type, valid = FILE_TYPES[extension]
    if not valid(data):
        return None, f"not a valid .{extension} file"
    return media_type, ""


def collect(outbox: str, allowed: list[str]) -> tuple[list[ToolFile], list[str]]:
    # Take every entry out of the outbox, keeping the regular files that pass the checks; the rest are refused.
    # The call controls the outbox, so everything goes through a descriptor opened without following links.
    files: list[ToolFile] = []
    notes: list[str] = []
    try:
        parent = os.open(os.path.dirname(outbox), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return files, notes
    try:
        try:
            box = os.open(os.path.basename(outbox), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return files, notes
        except OSError:
            _remove(os.path.basename(outbox), parent)
            return files, ["[not attached: the outbox was replaced by something that is not a folder]"]
        try:
            _drain(box, allowed, files, notes)
        finally:
            os.close(box)
    finally:
        os.close(parent)
    if len(notes) > MAX_NOTES:
        notes[MAX_NOTES - 1:] = [f"[{len(notes) - MAX_NOTES + 1} more entries not attached]"]
    return files, notes


def _drain(box: int, allowed: list[str], files: list[ToolFile], notes: list[str]) -> None:
    for name in sorted(os.listdir(box)):
        data, reason = None, ""
        try:
            info = os.lstat(name, dir_fd=box)
            if not stat.S_ISREG(info.st_mode):
                reason = "not a regular file"
            elif info.st_size > MAX_FILE_BYTES:
                reason = f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"
            elif len(files) >= MAX_FILES_PER_CALL:
                reason = f"at most {MAX_FILES_PER_CALL} files per call"
            else:
                # A process the call left running could swap the file for a FIFO or link after the lstat.
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=box)
                with os.fdopen(fd, "rb") as handle:
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        data = handle.read(MAX_FILE_BYTES + 1)
                    else:
                        reason = "not a regular file"
        except OSError:
            reason = "could not be read"
        finally:
            _remove(name, box)
        if data is not None:
            media_type, reason = check(name, data, allowed)
            if media_type:
                files.append(ToolFile(name, media_type, data))
                continue
        notes.append(f"[not attached {name[:100]}: {reason}]")


def from_wire(items: Any, allowed: list[str]) -> list[ToolFile]:
    files: list[ToolFile] = []
    for item in items if isinstance(items, list) else []:
        if len(files) >= MAX_FILES_PER_CALL or not isinstance(item, dict):
            break
        name, encoded = item.get("name"), item.get("data")
        if not isinstance(name, str) or not isinstance(encoded, str) or len(encoded) > MAX_FILE_BYTES * 4 // 3 + 4:
            continue
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue
        media_type, _ = check(name, data, allowed)
        if media_type:
            files.append(ToolFile(name, media_type, data))
    return files


def _remove(name: str, dir_fd: int) -> None:
    # Relative to an open folder, never following a link; rmtree with dir_fd does not follow links either.
    with contextlib.suppress(OSError):
        if stat.S_ISDIR(os.lstat(name, dir_fd=dir_fd).st_mode):
            shutil.rmtree(name, dir_fd=dir_fd, ignore_errors=True)
        else:
            os.unlink(name, dir_fd=dir_fd)


def size_text(count: int) -> str:
    return f"{count} bytes" if count < 1024 else f"{count / 1024:.0f} KB"
