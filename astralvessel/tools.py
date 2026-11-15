# Tool definitions the executor publishes and the file and shell tools that run inside the sandbox.
# run_sandbox_tool executes one call with time and output limits and never raises for tool-level failures.

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import confine as landlock
from .files import OUTBOX
from .manifest import DeclaredTool

SAFE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
            "HOME": "/tmp", "TMPDIR": "/tmp"}
# Session ids of tool processes still running; the sandbox server kills every other process it finds.
ACTIVE_SESSIONS: set[int] = set()
SPAWNING = [0]
MAX_OUTPUT_BYTES = 1_000_000
MAX_LINE_CHARS = 4000
WORKSPACE_KEY = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Confinement:
    # Each call gets a fresh scratch directory under scratch_root, or its turn's workspace when one is set, and
    # may read only these roots (plus the system).
    readable: tuple[str, ...]
    scratch_root: str
    workspace: str | None = None
    max_file_bytes: int = 0  # 0: no limit beyond the scratch filesystem

    def for_turn(self, key: str) -> Confinement:
        # A turn's calls share one workspace; the key is random per turn, so concurrent turns never meet.
        if not WORKSPACE_KEY.fullmatch(key):
            raise ValueError("invalid workspace key")
        path = os.path.join(self.scratch_root, f"turn-{key}")
        return replace(self, readable=(*self.readable, path), workspace=path)

    def allows(self, path: Path) -> bool:
        real = os.path.realpath(path)
        return any(real == r or real.startswith(r.rstrip("/") + "/") for r in map(os.path.realpath, self.readable))

BUILTIN_SCHEMAS: dict[str, dict[str, Any]] = {
    "read": {
        "description": "Read a text file. Returns numbered lines. Use offset and limit for large files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path, or relative to the working directory"},
            "offset": {"type": "integer", "description": "First line to return, starting at 1"},
            "limit": {"type": "integer", "description": "Number of lines to return (default 400)"},
        }, "required": ["path"]},
    },
    "grep": {
        "description": "Search file contents with a regular expression (ripgrep syntax). "
                       "Returns matching lines as path:line:text.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory to search (default: working directory)"},
            "glob": {"type": "string", "description": "Only search files matching this glob, e.g. *.md"},
            "ignore_case": {"type": "boolean"},
            "max_results": {"type": "integer", "description": "Default 100"},
        }, "required": ["pattern"]},
    },
    "glob": {
        "description": "Find files whose path matches a glob pattern such as docs/*.md.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Directory to search from (default: working directory)"},
            "max_results": {"type": "integer", "description": "Default 200"},
        }, "required": ["pattern"]},
    },
    "list": {
        "description": "List the entries of a directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory (default: working directory)"},
        }},
    },
    "bash": {
        "description": "Run a shell command in the sandbox and return its combined output and exit code.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
        }, "required": ["command"]},
    },
}


def clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[: limit - 200]
    return head + f"\n\n[output truncated: {len(text) - len(head)} more characters]", True


def _resolve(path: str | None, workdir: str) -> Path:
    base = Path(workdir)
    if not path:
        return base
    candidate = Path(path)
    return candidate if candidate.is_absolute() else base / candidate


async def _spawn(argv: list[str], *, cwd: str, env: dict[str, str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)


async def _proc(argv: list[str], *, cwd: str, timeout: int, shell: bool = False,
                stdin: bytes | None = None, confinement: Confinement | None = None) -> tuple[int, str]:
    if shell:
        argv = ["/bin/sh", "-c", argv[0]]
    env, scratch = SAFE_ENV, None
    if confinement is not None:
        env = {**SAFE_ENV}
        if confinement.workspace:
            scratch = confinement.workspace
            env["VESSEL_OUTBOX"] = outbox_of(scratch)
            os.makedirs(scratch, mode=0o700, exist_ok=True)
            _fresh_outbox(env["VESSEL_OUTBOX"])
        else:
            scratch = tempfile.mkdtemp(prefix="call-", dir=confinement.scratch_root)
        env.update(HOME=scratch, TMPDIR=scratch)
        flags = [f for root in confinement.readable for f in ("--read", root)]
        if confinement.max_file_bytes:
            flags += ["--fsize", str(confinement.max_file_bytes)]
        argv = [sys.executable, "-I", "-S", landlock.__file__, "--scratch", scratch, *flags, "--", *argv]
    try:
        return await _supervise(argv, cwd=cwd, env=env, timeout=timeout, stdin=stdin)
    finally:
        if scratch and not confinement.workspace:
            remove_tree(scratch)


async def _supervise(argv: list[str], *, cwd: str, env: dict[str, str], timeout: int,
                     stdin: bytes | None) -> tuple[int, str]:
    SPAWNING[0] += 1
    try:
        proc = await _spawn(argv, cwd=cwd, env=env)
    finally:
        SPAWNING[0] -= 1
    ACTIVE_SESSIONS.add(proc.pid)
    try:
        out = await asyncio.wait_for(_collect(proc, stdin), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 124, f"[timed out after {timeout} s]"
    finally:
        kill_session(proc.pid)
        ACTIVE_SESSIONS.discard(proc.pid)
    return proc.returncode or 0, out.decode("utf-8", "replace")


def kill_session(sid: int) -> None:
    # Confined calls cannot leave their session (seccomp blocks setsid and setpgid), so this ends everything a
    # call started, background processes included, before anything walks or removes its files.
    for _ in range(10):
        alive = False
        for entry in filter(str.isdigit, os.listdir("/proc")):
            with contextlib.suppress(OSError):
                if int(entry) != sid and os.getsid(int(entry)) == sid:
                    os.kill(int(entry), 9)
                    alive = True
        if not alive:
            return


async def _collect(proc: asyncio.subprocess.Process, stdin: bytes | None) -> bytes:
    # Keep at most MAX_OUTPUT_BYTES; a command that prints more is stopped, so a flood cannot exhaust memory.
    if stdin:
        proc.stdin.write(stdin)
        with contextlib.suppress(ConnectionError):
            await proc.stdin.drain()
    proc.stdin.close()
    chunks, size = [], 0
    while chunk := await proc.stdout.read(65536):
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_OUTPUT_BYTES:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 9)
            chunks.append(b"\n[output stopped at %d bytes]" % MAX_OUTPUT_BYTES)
            break
    await proc.wait()
    return b"".join(chunks)


def _open_checked(path: Path, confinement: Confinement | None, *, directory: bool = False) -> int:
    # Open first, then check where the descriptor really points: a call can swap links in its workspace
    # between a path check and the open, and these tools run in the unconfined server.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0))
    try:
        real = os.readlink(f"/proc/self/fd/{fd}")
        if confinement is not None and not confinement.allows(Path(real)):
            raise PermissionError(errno.EACCES, "outside the agent's data")
        if not directory and not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IsADirectoryError(errno.EISDIR, "not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


async def _read(args: dict[str, Any], workdir: str, confinement: Confinement | None = None) -> tuple[str, bool]:
    path = _resolve(args.get("path"), workdir)
    if not path.is_file():
        return f"not a file: {path}", True
    offset = max(int(args.get("offset") or 1), 1)
    limit = min(max(int(args.get("limit") or 400), 1), 2000)

    def scan() -> list[str]:
        lines = []
        fd = _open_checked(path, confinement)
        with open(fd, encoding="utf-8", errors="replace") as handle:
            number = 0
            # readline with a size bound keeps one enormous line from filling memory.
            while line := handle.readline(MAX_LINE_CHARS):
                if not line.endswith("\n") and len(line) == MAX_LINE_CHARS:
                    while (rest := handle.readline(MAX_LINE_CHARS)) and not rest.endswith("\n"):
                        pass  # skip the rest of the over-long line in bounded pieces
                    line += " [line truncated]"
                number += 1
                if number < offset:
                    continue
                if number >= offset + limit:
                    lines.append(f"[more lines follow; continue with offset={number}]")
                    break
                lines.append(f"{number:>6}\t{line.rstrip()}")
        return lines

    return "\n".join(await asyncio.to_thread(scan)) or "[empty]", False


async def _grep(args: dict[str, Any], workdir: str, timeout: int,
                confinement: Confinement | None) -> tuple[str, bool]:
    target = str(_resolve(args.get("path"), workdir))
    cap = min(max(int(args.get("max_results") or 100), 1), 1000)
    if shutil.which("rg"):
        argv = ["rg", "--line-number", "--no-heading", "--color=never", "--max-columns=400"]
        if args.get("ignore_case"):
            argv.append("-i")
        if args.get("glob"):
            argv += ["--glob", str(args["glob"])]
        argv += ["--", str(args["pattern"]), target]
    else:
        argv = ["grep", "-rnE"] + (["-i"] if args.get("ignore_case") else [])
        if args.get("glob"):
            argv.append(f"--include={args['glob']}")
        argv += ["--", str(args["pattern"]), target]
    code, out = await _proc(argv, cwd=workdir, timeout=timeout, confinement=confinement)
    if code == 1 and not out.strip():
        return "no matches", False
    if code not in (0, 1):
        return out.strip() or f"search failed with exit code {code}", True
    lines = out.splitlines()
    tail = f"\n[{len(lines) - cap} more matches not shown]" if len(lines) > cap else ""
    return "\n".join(lines[:cap]) + tail, False


async def _glob(args: dict[str, Any], workdir: str, timeout: int,
                confinement: Confinement | None = None) -> tuple[str, bool]:
    base = _resolve(args.get("path"), workdir)
    cap = min(max(int(args.get("max_results") or 200), 1), 2000)
    pattern = str(args["pattern"])
    if pattern.startswith("/"):
        return "the pattern must be relative; pass the directory as path", True
    if ".." in Path(pattern).parts:
        return "the pattern may not climb out of the directory with ..", True
    # The walk runs in a thread with a time budget so a huge tree cannot stall every other call.
    deadline = time.monotonic() + timeout

    def walk() -> tuple[list[str], bool]:
        found: list[str] = []
        fd = _open_checked(base, confinement, directory=True)
        try:
            # Walk through the opened descriptor, so swapping a folder for a link mid-walk changes nothing.
            root = Path(f"/proc/self/fd/{fd}")
            # A bare name pattern such as "*.md" searches the whole tree.
            for match in root.rglob(pattern) if "/" not in pattern else root.glob(pattern):
                if time.monotonic() > deadline:
                    return found, True
                if confinement is not None and not confinement.allows(match):
                    continue  # a symlink pointing outside the agent's data
                found.append(str(base / match.relative_to(root)))
                if len(found) > cap:
                    break
        finally:
            os.close(fd)
        return found, False

    found, timed_out = await asyncio.to_thread(walk)
    more = f"\n[more than {cap} matches; narrow the pattern]" if len(found) > cap else ""
    if timed_out:
        more += f"\n[stopped after {timeout} s; narrow the pattern or path]"
    return ("\n".join(sorted(found[:cap])) or "no matches") + more, False


async def _list(args: dict[str, Any], workdir: str, confinement: Confinement | None = None) -> tuple[str, bool]:
    path = _resolve(args.get("path"), workdir)
    if not path.is_dir():
        return f"not a directory: {path}", True
    fd = _open_checked(path, confinement, directory=True)
    try:
        with os.scandir(fd) as scan:
            entries = sorted(((e.name, e.is_dir(follow_symlinks=False)) for e in scan),
                             key=lambda e: (not e[1], e[0]))
    finally:
        os.close(fd)
    shown = [name + ("/" if is_dir else "") for name, is_dir in entries[:500]]
    more = f"\n[{len(entries) - 500} more entries]" if len(entries) > 500 else ""
    return "\n".join(shown) + more or "[empty directory]", False


async def _bash(args: dict[str, Any], workdir: str, timeout: int,
                confinement: Confinement | None) -> tuple[str, bool]:
    code, out = await _proc([str(args["command"])], cwd=workdir, timeout=timeout, shell=True,
                            confinement=confinement)
    return f"{out.rstrip()}\n[exit code {code}]".lstrip(), code != 0


async def _declared(tool: DeclaredTool, args: dict[str, Any], workdir: str, package_root: str,
                    timeout: int, confinement: Confinement | None) -> tuple[str, bool]:
    argv = [part.replace("{package}", package_root) for part in tool.command]
    stdin = json.dumps(args).encode()
    if tool.input == "args":
        # Only the declared parameters become flags, so the model cannot add flags of its own.
        allowed = set((tool.parameters or {}).get("properties") or {})
        unknown = sorted(k for k in args if k not in allowed)
        if unknown:
            return f"unknown arguments for {tool.name}: {', '.join(unknown)[:200]}", True
        argv += [f"--{key}={value}" for key, value in args.items()]
        stdin = b""
    code, out = await _proc(argv, cwd=workdir, timeout=tool.timeout_seconds or timeout, stdin=stdin,
                            confinement=confinement)
    return out.rstrip() or f"[exit code {code}]", code != 0


async def run_sandbox_tool(name: str, args: dict[str, Any], *, workdir: str, timeout: int,
                           declared: dict[str, DeclaredTool], package_root: str,
                           confinement: Confinement | None = None,
                           workspace: str | None = None) -> tuple[str, bool]:
    try:
        if workspace and confinement is not None:
            confinement = confinement.for_turn(workspace)
        if name in ("read", "glob", "list") and confinement is not None:
            # These run inside the sandbox server itself, so they get the same path limits a confined call has.
            target = _resolve(args.get("path"), workdir)
            if not confinement.allows(target):
                return f"{name}: {target} is outside the agent's data", True
        if name == "read":
            return await _read(args, workdir, confinement)
        if name == "grep":
            return await _grep(args, workdir, timeout, confinement)
        if name == "glob":
            return await _glob(args, workdir, timeout, confinement)
        if name == "list":
            return await _list(args, workdir, confinement)
        if name == "bash":
            return await _bash(args, workdir, timeout, confinement)
        if name in declared:
            return await _declared(declared[name], args, workdir, package_root, timeout, confinement)
    except (KeyError, ValueError, TypeError) as exc:
        return f"invalid arguments for {name}: {type(exc).__name__}", True
    except OSError as exc:
        return f"{name} failed: {exc.strerror or type(exc).__name__}", True
    except Exception:  # noqa: BLE001  any other failure becomes a generic tool error, never raw text
        return f"{name} failed", True
    return f"unknown tool {name}", True


def _fresh_outbox(path: str) -> None:
    # An earlier call may have replaced the outbox with a file or link; put a real folder back.
    with contextlib.suppress(OSError):
        if not os.path.isdir(path) or os.path.islink(path):
            os.unlink(path)
    with contextlib.suppress(OSError):
        os.mkdir(path, 0o700)


def outbox_of(workspace: str) -> str:
    return os.path.join(workspace, OUTBOX)


def end_workspace(confinement: Confinement | None, workspace: str) -> None:
    if confinement is not None and WORKSPACE_KEY.fullmatch(workspace):
        remove_tree(confinement.for_turn(workspace).workspace)


# Calls and the server share a uid, so a call can chmod its folders shut or nest them past PATH_MAX. The server
# only ever adds owner rights back (Landlock, not file modes, is the boundary) and keeps paths shallow.
ENTRY_BYTES = 1024  # charged per file or folder, so millions of empty ones cannot slip under a limit
MAX_DEPTH = 32
FLAT_DEPTH = 8


def _open_up(path: str) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_PATH | os.O_NOFOLLOW)
        try:
            mode = os.fstat(fd).st_mode
            if stat.S_ISDIR(mode) and mode & 0o700 != 0o700:
                os.chmod(f"/proc/self/fd/{fd}", stat.S_IMODE(mode) | 0o700)
        finally:
            os.close(fd)


def _scan(path: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as entries:
            return list(entries)
    except PermissionError:
        _open_up(path)
        with os.scandir(path) as entries:
            return list(entries)


def disk_usage(path: str, limit: int = 0) -> int:
    # Allocated bytes plus a charge per entry, stopping early once over limit. A tree too deep or too long to
    # walk counts as over any limit.
    total, stack = 0, [(path, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = _scan(folder)
        except FileNotFoundError:
            continue
        except OSError:
            return max(total, limit) + 1
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                return max(total, limit) + 1  # a name too long to reach, or a folder shut mid-walk
            total += info.st_blocks * 512 + ENTRY_BYTES
            if stat.S_ISDIR(info.st_mode):
                if depth + 1 > MAX_DEPTH:
                    return max(total, limit) + 1
                stack.append((entry.path, depth + 1))
        if limit and total > limit:
            return total
    return total


def remove_tree(root: str) -> None:
    # Move anything nested deeper than FLAT_DEPTH up to the top first, so paths stay short and rmtree stays shallow.
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = _scan(folder)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            _open_up(entry.path)
            if depth + 1 < FLAT_DEPTH:
                stack.append((entry.path, depth + 1))
                continue
            moved = os.path.join(root, f".flat-{secrets.token_hex(8)}")
            with contextlib.suppress(OSError):
                os.rename(entry.path, moved)
                stack.append((moved, 1))
    shutil.rmtree(root, ignore_errors=True)


def open_deleted_usage(prefix: str) -> dict[str, int]:
    # Files a call deleted but still holds open or mapped keep their space; charge them to the workspace they were
    # in. Calls cannot leave their sessions and hold at most MAX_OPEN_FILES each, so this scan stays bounded.
    found: dict[tuple[int, int], tuple[str, int]] = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        with contextlib.suppress(OSError):
            if os.getsid(int(pid)) not in ACTIVE_SESSIONS:
                continue
            for fd in os.listdir(f"/proc/{pid}/fd"):
                with contextlib.suppress(OSError):
                    target = os.readlink(f"/proc/{pid}/fd/{fd}")
                    if target.startswith(prefix) and target.endswith(" (deleted)"):
                        info = os.stat(f"/proc/{pid}/fd/{fd}")
                        workspace = target[len(prefix):].split("/", 1)[0]
                        found[(info.st_dev, info.st_ino)] = (workspace, info.st_blocks * 512)
            with open(f"/proc/{pid}/maps") as maps:
                for line in maps:
                    fields = line.split(maxsplit=5)
                    if len(fields) == 6 and fields[5].startswith(prefix) and fields[5].rstrip().endswith(
                            " (deleted)"):
                        start, end = (int(x, 16) for x in fields[0].split("-"))
                        workspace = fields[5][len(prefix):].split("/", 1)[0]
                        key = (int(fields[3].replace(":", ""), 16), int(fields[4]))
                        size = max(found.get(key, ("", 0))[1], end - start)
                        found[key] = (workspace, size)
    usage: dict[str, int] = {}
    for workspace, size in found.values():
        usage[workspace] = usage.get(workspace, 0) + size
    return usage


def sweep_workspaces(confinement: Confinement | None, live: set[str]) -> None:
    # Remove every workspace whose turn the server is not tracking: ended, past its deadline, or from an earlier
    # process. The server's own record decides, not file times, which a call can change.
    if confinement is None:
        return
    with contextlib.suppress(OSError), os.scandir(confinement.scratch_root) as entries:
        for entry in entries:
            if entry.name.startswith("turn-") and entry.name.removeprefix("turn-") not in live:
                remove_tree(entry.path)
