"""Supervises the kernel's blind mailbox as a child process of the node, so devices of the owner's mesh that are never online together exchange events through it.
node_mesh.py starts and stops it and reports its address, which devices learn from the pairing response and, once the node has joined the mesh, from the descriptor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("Orchestrator.NodeMailbox")

ENABLE_ENV = "ASTRAL_NODE_MAILBOX"
BINARY_ENV = "ASTRAL_MAILBOX_BIN"
BIND_ENV = "ASTRAL_NODE_MAILBOX_BIND"
RETENTION_ENV = "ASTRAL_NODE_MAILBOX_MAX_AGE_DAYS"
SIZE_ENV = "ASTRAL_NODE_MAILBOX_MAX_MIB"
DEFAULT_BINARY = "astral-mailbox"
STARTUP_SECONDS = 30.0
STOP_SECONDS = 10.0
RESTART_FIRST_SECONDS = 1.0
RESTART_MAX_SECONDS = 60.0
STABLE_SECONDS = 30.0
MAX_MESHES = 1


class MailboxError(Exception):
    pass


@dataclass(frozen=True)
class MailboxSettings:
    enabled: bool = True
    binary: str = ""
    loopback: bool = False
    bind: Sequence[str] = ()
    relay: Sequence[str] = ("default",)
    max_age_days: Optional[int] = None
    max_mib: Optional[int] = None

    @classmethod
    def from_env(cls, env: Dict[str, str], *, loopback: bool = False) -> "MailboxSettings":
        def number(name: str) -> Optional[int]:
            raw = env.get(name, "").strip()
            return int(raw) if raw.isdigit() and int(raw) > 0 else None

        return cls(
            enabled=env.get(ENABLE_ENV, "").strip().lower() not in {"0", "false", "no", "off"},
            binary=env.get(BINARY_ENV, "").strip(),
            loopback=loopback,
            bind=tuple(part.strip() for part in env.get(BIND_ENV, "").split(",") if part.strip()),
            relay=("none",) if loopback else ("default",),
            max_age_days=number(RETENTION_ENV),
            max_mib=number(SIZE_ENV),
        )

    def resolve_binary(self) -> Optional[str]:
        return shutil.which(self.binary or DEFAULT_BINARY)

    def arguments(self, data_dir: Path) -> List[str]:
        args = ["serve", "--data-dir", str(data_dir), "--max-meshes", str(MAX_MESHES)]
        for relay in self.relay:
            if relay != "default":
                args += ["--relay", relay]
        if self.loopback:
            args += ["--no-local-discovery", "--no-internet-lookup"]
        for address in self.bind:
            args += ["--bind", address]
        if self.max_age_days:
            args += ["--max-age-days", str(self.max_age_days)]
        if self.max_mib:
            args += ["--max-mesh-mib", str(self.max_mib)]
        return args


def parse_announcement(lines: Sequence[str]) -> Optional[Dict[str, Any]]:
    mailbox_id: Optional[str] = None
    address: Optional[str] = None
    for line in lines:
        if line.startswith("mailbox id "):
            mailbox_id = line[len("mailbox id "):].strip()
        elif line.startswith("address "):
            raw = line[len("address "):].strip()
            try:
                json.loads(raw)
            except ValueError:
                return None
            address = raw
    if not mailbox_id or not address:
        return None
    return {"id": mailbox_id, "address": address}


class MailboxProcess:
    def __init__(self, settings: MailboxSettings, *, data_dir: Path):
        self._settings = settings
        self._data_dir = Path(data_dir)
        self._process: Optional[asyncio.subprocess.Process] = None
        self._announcement: Optional[Dict[str, Any]] = None
        self._supervisor: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._stopping = False
        self._restarts = 0
        self._last_error: Optional[str] = None

    def snapshot(self) -> Dict[str, Any]:
        running = self._process is not None and self._process.returncode is None
        return {
            "running": running,
            "id": self._announcement["id"] if self._announcement and running else None,
            "address": self._announcement["address"] if self._announcement and running else None,
            "restarts": self._restarts,
            "last_error": self._last_error,
        }

    @property
    def address(self) -> Optional[str]:
        return self.snapshot()["address"]

    async def start(self) -> None:
        binary = self._settings.resolve_binary()
        if binary is None:
            raise MailboxError("mailbox_binary_missing")
        self._data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._data_dir, 0o700)
        self._stopping = False
        self._supervisor = asyncio.create_task(self._supervise(binary), name="node-mailbox")
        try:
            await asyncio.wait_for(self._ready.wait(), STARTUP_SECONDS)
        except asyncio.TimeoutError:
            await self.stop()
            raise MailboxError(self._last_error or "mailbox_start_timeout") from None

    async def _spawn(self, binary: str) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            binary,
            *self._settings.arguments(self._data_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )

    async def _announce(self, process: asyncio.subprocess.Process) -> None:
        lines: List[str] = []
        assert process.stdout is not None
        while self._announcement_pending(lines):
            raw = await process.stdout.readline()
            if not raw:
                raise MailboxError("mailbox_exited_before_ready")
            lines.append(raw.decode(errors="replace").strip())
        self._announcement = parse_announcement(lines)

    @staticmethod
    def _announcement_pending(lines: Sequence[str]) -> bool:
        return parse_announcement(lines) is None

    async def _supervise(self, binary: str) -> None:
        delay = RESTART_FIRST_SECONDS
        loop = asyncio.get_running_loop()
        while not self._stopping:
            started = loop.time()
            try:
                self._process = await self._spawn(binary)
                await asyncio.wait_for(self._announce(self._process), STARTUP_SECONDS)
                self._last_error = None
                self._ready.set()
                await self._process.wait()
                self._last_error = f"mailbox_exit_{self._process.returncode}"
            except (MailboxError, OSError, asyncio.TimeoutError) as exc:
                self._last_error = str(exc) or type(exc).__name__
                await self._terminate()
            if self._stopping:
                return
            logger.warning("node_mailbox_restarting: %s", self._last_error)
            self._restarts += 1
            if loop.time() - started >= STABLE_SECONDS:
                delay = RESTART_FIRST_SECONDS
            await asyncio.sleep(delay)
            delay = min(delay * 2, RESTART_MAX_SECONDS)

    async def _terminate(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), STOP_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def stop(self) -> None:
        self._stopping = True
        supervisor, self._supervisor = self._supervisor, None
        await self._terminate()
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        self._announcement = None
        self._ready.clear()
