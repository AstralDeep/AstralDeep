# Runs sandbox tools in-process (the sandbox server and tests) or over HTTP from the executor to a sandbox container.
# The sandbox container holds no session store and no secrets of the node, so a hostile command cannot reach either.

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import signal
import time
from dataclasses import replace
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import confine as landlock
from . import files as outputs
from .manifest import AgentPackage, load_package
from .tools import (
    ACTIVE_SESSIONS,
    SPAWNING,
    WORKSPACE_KEY,
    Confinement,
    disk_usage,
    end_workspace,
    open_deleted_usage,
    outbox_of,
    run_sandbox_tool,
    sweep_workspaces,
)

SCRATCH_ROOT = "/tmp/vessel-calls"
log = logging.getLogger("astralvessel")


def confinement_for(package: AgentPackage, scratch_root: str = SCRATCH_ROOT) -> Confinement:
    sandbox = package.manifest.sandbox
    readable = (sandbox.workdir, str(package.root), *(m.target for m in sandbox.mounts))
    return Confinement(readable=tuple(dict.fromkeys(readable)), scratch_root=scratch_root)


class LocalRunner:
    def __init__(self, package: AgentPackage, confinement: Confinement | None = None):
        self.package = package
        self.declared = {t.name: t for t in package.declared if t.command}  # service tools never run here
        self.confinement = confinement
        self.files: dict[str, list[outputs.ToolFile]] = {}

    async def run(self, name: str, args: dict[str, Any], workspace: str | None = None,
                  owner: str = "") -> tuple[str, bool]:
        sandbox = self.package.manifest.sandbox
        workdir = os.environ.get("VESSEL_WORKDIR") or sandbox.workdir
        output, error = await run_sandbox_tool(name, args, workdir=workdir, timeout=sandbox.tool_timeout_seconds,
                                               declared=self.declared, package_root=str(self.package.root),
                                               confinement=self.confinement, workspace=workspace)
        if workspace and self.confinement is not None and WORKSPACE_KEY.fullmatch(workspace):
            outbox = outbox_of(self.confinement.for_turn(workspace).workspace)
            found, notes = await asyncio.to_thread(outputs.collect, outbox, self.package.manifest.output.files)
            if found:
                self.files.setdefault(workspace, []).extend(found)
            if notes:
                output = "\n".join([*notes, output])  # first, so clipping a long output keeps them
        return output, error

    async def end_turn(self, workspace: str) -> None:
        self.files.pop(workspace, None)
        await asyncio.to_thread(end_workspace, self.confinement, workspace)

    def take_files(self, workspace: str) -> list[outputs.ToolFile]:
        return self.files.pop(workspace, [])

    async def close(self) -> None:
        return None


class RemoteRunner:
    def __init__(self, url: str, token: str, timeout: int, file_types: list[str] | None = None):
        self.file_types = file_types or []
        self.files: dict[str, list[outputs.ToolFile]] = {}
        self._client = httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"},
                                         timeout=httpx.Timeout(timeout + 15, connect=10))

    async def health(self) -> dict[str, Any] | None:
        try:
            response = await self._client.get("/health", timeout=5)
        except httpx.HTTPError:
            return None
        return response.json() if response.status_code == 200 else None

    async def run(self, name: str, args: dict[str, Any], workspace: str | None = None,
                  owner: str = "") -> tuple[str, bool]:
        # The sandbox only needs to tell users apart, so it gets a digest, never the name.
        user = hashlib.sha256(owner.encode()).hexdigest()[:32] if owner else None
        try:
            response = await self._client.post("/run", json={"name": name, "args": args, "workspace": workspace,
                                                             "owner": user})
        except httpx.HTTPError:
            return "the sandbox is unreachable", True
        if response.status_code != 200:
            return f"the sandbox refused the call (HTTP {response.status_code})", True
        body = response.json()
        found = outputs.from_wire(body.get("files"), self.file_types) if workspace else []
        if found:
            self.files.setdefault(workspace, []).extend(found)
        return str(body.get("output", "")), bool(body.get("error"))

    def take_files(self, workspace: str) -> list[outputs.ToolFile]:
        return self.files.pop(workspace, [])

    async def end_turn(self, workspace: str) -> None:
        self.files.pop(workspace, None)
        try:
            await self._client.post("/end", json={"workspace": workspace}, timeout=10)
        except httpx.HTTPError:
            pass  # the sandbox sweeps workspaces older than any turn could run

    async def close(self) -> None:
        await self._client.aclose()


def reap_strays() -> None:
    # Background processes a call left behind (nohup, setsid, a daemon) must not outlive it: they could
    # watch later calls made for other users. Only the sandbox server runs this, inside its own container.
    me = os.getpid()
    if me != 1 or SPAWNING[0]:
        return  # not the container's init process (never reap a developer's machine), or a call is starting
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        pid = int(entry)
        try:
            if os.getsid(pid) not in ACTIVE_SESSIONS:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def sandbox_app(package_path: str | None = None, token: str | None = None, *, confine: bool | None = None,
                scratch_root: str = SCRATCH_ROOT) -> Starlette:
    package = load_package(package_path or os.environ["VESSEL_PACKAGE"])
    secret = token or os.environ["VESSEL_SANDBOX_TOKEN"]
    if len(secret) < 16:
        raise ValueError("VESSEL_SANDBOX_TOKEN must be at least 16 characters")
    if confine is None:
        confine = os.environ.get("VESSEL_UNCONFINED") != "1"
    confinement = None
    if confine:
        # Without Landlock, concurrent calls for different users could see each other; refuse to start.
        if landlock.abi_version() < landlock.REQUIRED_ABI:
            raise SystemExit(f"this kernel lacks Landlock ABI {landlock.REQUIRED_ABI}; per-call isolation is "
                             "unavailable (set VESSEL_UNCONFINED=1 only for development)")
        os.makedirs(scratch_root, mode=0o711, exist_ok=True)
        confinement = confinement_for(package, scratch_root)
    turn_disk = int(os.environ.get("VESSEL_TURN_DISK") or 0)
    user_disk = int(os.environ.get("VESSEL_USER_DISK") or 0)
    if confinement is not None and turn_disk:
        confinement = replace(confinement, max_file_bytes=turn_disk)
    runner = LocalRunner(package, confinement)
    profile = os.environ.get("VESSEL_PROFILE_HASH", "")
    stale_after = package.manifest.sandbox.deadline_seconds + 60
    started: dict[str, float] = {}  # live turns and when each was first seen
    running: dict[str, set[asyncio.Task]] = {}
    ended: dict[str, None] = {}  # recently ended keys, oldest first; a late call for one is refused
    owners: dict[str, str] = {}  # live turn -> its user's digest

    measured: dict[str, tuple[float, int]] = {}  # workspace -> (when, bytes), shared by concurrent calls
    deleted_at: list[float] = [0.0]
    deleted: list[dict[str, int]] = [{}]

    def check_limits(workspace: str | None, fresh: bool = False) -> str:
        # A measurement that fails counts as over the limit, so an odd tree can never switch the limits off.
        try:
            return over_limit(workspace, fresh)
        except Exception:  # noqa: BLE001
            log.warning("measuring disk use failed", exc_info=True)
            return "this turn's"

    def over_limit(workspace: str | None, fresh: bool = False) -> str:
        # Which disk limit a turn has gone over, if any; a user's limit covers all their live turns together.
        if confinement is None or not workspace:
            return ""
        now = time.monotonic()
        if now - deleted_at[0] > 0.4 or fresh:
            deleted_at[0], deleted[0] = now, open_deleted_usage(os.path.join(confinement.scratch_root, "turn-"))
        def used(key: str) -> int:
            when, size = measured.get(key, (0.0, 0))
            if now - when > 0.4 or (fresh and key == workspace):
                size = disk_usage(confinement.for_turn(key).workspace, user_disk or turn_disk) + deleted[0].get(key, 0)
                measured[key] = (now, size)
            return size
        if turn_disk and used(workspace) > turn_disk:
            return f"this turn's {turn_disk // (1024 * 1024)} MB"
        owner = owners.get(workspace)
        if user_disk and owner and sum(used(k) for k, o in list(owners.items()) if o == owner) > user_disk:
            return f"your {user_disk // (1024 * 1024)} MB across turns"
        return ""

    stopped: dict[str, str] = {}  # turn -> why its calls were stopped, for the calls cancelled alongside

    def stop_note(limit: str) -> JSONResponse:
        return JSONResponse({"error": True, "files": [],
                             "output": f"[stopped: the files went over {limit} disk limit, so this turn's "
                                       "workspace was emptied; write less, or delete files you no longer need]"})

    removing: dict[str, asyncio.Event] = {}  # turns whose workspace is being emptied; new calls wait

    async def stop_over_limit(workspace: str, limit: str) -> JSONResponse:
        # Stop every call of the turn, not only this one, and hold new ones back, so no process of the turn is
        # alive while its workspace is removed.
        stopped[workspace] = limit
        gate = removing.setdefault(workspace, asyncio.Event())
        try:
            tasks = set(running.get(workspace, set()))
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            runner.files.pop(workspace, None)
            measured.pop(workspace, None)
            await asyncio.to_thread(end_workspace, confinement, workspace)
        finally:
            removing.pop(workspace, None)
            gate.set()
        return stop_note(limit)

    def authorized(request: Request) -> bool:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(supplied.encode(), secret.encode())

    def workspace_of(body: dict[str, Any]) -> str | None:
        workspace = body.get("workspace") if isinstance(body, dict) else ""
        if workspace is not None and not (isinstance(workspace, str) and WORKSPACE_KEY.fullmatch(workspace)):
            raise ValueError("invalid workspace")
        return workspace

    async def run(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)
        body = await request.json()
        name, args = str(body.get("name", "")), body.get("args") or {}
        if not isinstance(args, dict):
            return JSONResponse({"error": True, "output": "arguments must be an object"}, status_code=400)
        try:
            workspace = workspace_of(body)
        except ValueError:
            return JSONResponse({"error": True, "output": "invalid workspace"}, status_code=400)
        if workspace in ended:
            return JSONResponse({"error": True, "output": "this turn has ended"}, status_code=410)
        owner = body.get("owner")
        if owner is not None and not (isinstance(owner, str) and WORKSPACE_KEY.fullmatch(owner)):
            return JSONResponse({"error": True, "output": "invalid owner"}, status_code=400)
        # A turn keeps the user its first call named, or none; a call naming anyone else is refused.
        if workspace and owners.setdefault(workspace, owner or "") != (owner or ""):
            return JSONResponse({"error": True, "output": "this turn belongs to another user"}, status_code=403)
        now = time.monotonic()
        if workspace:
            started.setdefault(workspace, now)
        for key in [k for k, t in started.items() if now - t > stale_after]:
            del started[key]
            owners.pop(key, None)
            measured.pop(key, None)
            stopped.pop(key, None)
        await asyncio.to_thread(sweep_workspaces, confinement, set(started))
        reap_strays()
        # Check again with no await before the call starts: the turn may have ended or begun emptying meanwhile.
        while workspace in removing:
            await removing[workspace].wait()
        if workspace in ended:
            return JSONResponse({"error": True, "output": "this turn has ended"}, status_code=410)
        task = asyncio.create_task(runner.run(name, args, workspace))
        if workspace:
            running.setdefault(workspace, set()).add(task)
            task.add_done_callback(lambda t: running.get(workspace, set()).discard(t))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=0.5)
                if not done and await request.is_disconnected():
                    task.cancel()
                    return JSONResponse({"error": True, "output": "cancelled"}, status_code=499)
                if not done and (limit := await asyncio.to_thread(check_limits, workspace)):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return await stop_over_limit(workspace, limit)
            if task.cancelled():
                if workspace in stopped:
                    return stop_note(stopped[workspace])
                return JSONResponse({"error": True, "output": "this turn has ended"}, status_code=410)
            output, error = task.result()
            if limit := await asyncio.to_thread(check_limits, workspace, True):
                return await stop_over_limit(workspace, limit)
        finally:
            if not task.done():
                task.cancel()  # whatever went wrong here, the call does not run on unwatched
            reap_strays()
        files = [f.wire() for f in runner.take_files(workspace)] if workspace else []
        return JSONResponse({"output": output, "error": error, "files": files})

    async def end(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)
        try:
            workspace = workspace_of(await request.json())
        except ValueError:
            workspace = None
        if workspace is None:
            return JSONResponse({"error": True, "output": "invalid workspace"}, status_code=400)
        ended[workspace] = None
        while len(ended) > 4096:
            del ended[next(iter(ended))]
        started.pop(workspace, None)
        owners.pop(workspace, None)
        measured.pop(workspace, None)
        stopped.pop(workspace, None)
        # Stop the turn's calls still in flight first, so none writes into or recreates the workspace after it.
        tasks = running.pop(workspace, set())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.end_turn(workspace)
        return JSONResponse({"ok": True})

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "agent": package.manifest.agent.name, "profile": profile,
                             "loaded": package.loaded_digest(), "confined": confinement is not None})

    return Starlette(routes=[Route("/run", run, methods=["POST"]), Route("/end", end, methods=["POST"]),
                             Route("/health", health)])
