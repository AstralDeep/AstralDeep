"""Resolves persisted project membership into immutable current-host folder authority.
Async dispatch scopes supply short-lived trusted witnesses to synchronous consent predicates.
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from orchestrator.computer_hosts import ComputerHostError
from orchestrator.projects import ProjectError, ProjectService
from shared.project_workspaces import ProjectContext, ProjectManifest

FILE_VERBS = frozenset({"read_file", "list_dir", "write_file", "delete_path", "run_command", "read_output", "interrupt_command"})
PROJECT_CONSENT_VERBS = FILE_VERBS | {"start_session", "end_session", "resume_session"}
_CURRENT = contextvars.ContextVar("project_dispatch_origin", default=None)
_ACTIVE = {}


def _refusal(code="project_grant_required", message="Choose folders for this project on this Mac."):
    return ComputerHostError(code, message, effect_state="not_started")


def _host_current(orch, host, connection, generation):
    claims = getattr(orch, "ui_sessions", {}).get(host.websocket) or {}
    return (orch.computer_hosts.get(host.owner_sub, host.host_id) is host
            and claims.get("sub") == host.owner_sub and claims.get("_project_workspaces_v1") is True
            and getattr(orch, "_connection_contexts", {}).get(id(host.websocket)) is connection
            and connection is not None and connection.websocket is host.websocket
            and connection.registered and not connection.closing and connection.connection_generation == generation)


@dataclass(frozen=True)
class ProjectOrigin:
    orchestrator: object
    owner: str
    chat: str
    host: object
    connection: object
    connection_generation: object
    context: ProjectContext
    entry: object

    @property
    def roots(self):
        return [root.to_dict() for root in self.entry.roots] if self.entry else []

    @property
    def primary(self):
        return self.entry.roots[0].path if self.entry and self.entry.roots else None

    def require_grant(self):
        if not self.entry or not self.entry.roots:
            raise _refusal()
        return self


def origin_current(orch, origin):
    if not isinstance(origin, ProjectOrigin) or origin.orchestrator is not orch:
        return False
    host = origin.host
    manifest = getattr(host, "project_manifest", None)
    return (host.owner_sub == origin.owner and getattr(host, "project_mode", False)
            and _host_current(orch, host, origin.connection, origin.connection_generation)
            and manifest is not None and manifest.generation >= origin.context.manifest_generation
            and manifest.project(origin.context.project_id) == origin.entry)


@dataclass(frozen=True)
class ProjectSessionWitness:
    manager: object
    session: object
    generation: int
    state: str
    origin: object
    controller: int
    chat: str

    @classmethod
    def capture(cls, orch, session):
        return cls(orch.computer_sessions, session, session.authority_generation, session.state,
                   session.project_origin, session.controller_ws_id, session.chat_id)

    def current(self, orch):
        return (orch.computer_sessions is self.manager and self.manager.get(self.session.session_id) is self.session
                and self.session.state != "ended" and self.session.state == self.state
                and self.session.authority_generation == self.generation
                and self.session.project_origin is self.origin and self.session.controller_ws_id == self.controller
                and self.session.chat_id == self.chat)


async def resolve_project_origin(orch, owner, chat, host, *, session=None):
    if not getattr(host, "project_mode", False):
        return None
    connection = getattr(orch, "_connection_contexts", {}).get(id(host.websocket))
    generation = getattr(connection, "connection_generation", None)
    manifest = getattr(host, "project_manifest", None)
    if owner != host.owner_sub or not _host_current(orch, host, connection, generation) or manifest is None:
        raise _refusal("project_authority_unavailable", "This Mac's project catalog and grants are not ready.")
    witness = ProjectSessionWitness.capture(orch, session) if session is not None else None
    if witness is not None and not witness.current(orch):
        raise _refusal("stale_project_grant", "The project session is no longer current.")
    try:
        project = await asyncio.to_thread(ProjectService(orch.history).require_chat, owner, chat)
    except ProjectError as exc:
        raise _refusal(exc.code, exc.message) from None
    if (not _host_current(orch, host, connection, generation) or host.project_manifest is not manifest
            or witness is not None and not witness.current(orch)):
        raise _refusal("stale_project_grant", "The project host changed while resolving this request.")
    entry = manifest.project(project)
    origin = ProjectOrigin(orch, owner, chat, host, connection, generation,
        ProjectContext(chat, project, entry.grant_revision if entry else None, manifest.generation), entry)
    if session is not None:
        previous = getattr(session, "project_origin", None)
        if (previous is None or not origin_current(orch, previous)
                or previous.context.project_id != project or previous.chat != chat
                or previous.owner != owner or previous.host is not host
                or session.chat_id != chat or session.owner_sub != owner):
            raise _refusal("stale_project_grant", "Start a fresh session for this project's current folder grant.")
        return previous
    return origin


def current_origin(orch, owner, chat, host):
    scope = _CURRENT.get()
    if scope is None or _ACTIVE.get(scope[0]) is not scope:
        return None
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None and task is not scope[2]:
        return None
    for origin in scope[1]:
        if (origin.owner, origin.chat) == (owner, chat) and origin.host is host and origin_current(orch, origin):
            return origin
    return None


@dataclass(frozen=True)
class ProjectBridgeLease:
    scope: object
    origin: ProjectOrigin
    session: ProjectSessionWitness | None
    controller: object
    connection: object
    connection_generation: object
    manager: object

    def verify(self, orch, *, session_transition=False):
        claims = getattr(orch, "ui_sessions", {}).get(self.controller) or {}
        task = self.scope[2]
        if (_ACTIVE.get(self.scope[0]) is not self.scope or task is None or task.done() or task.cancelling()
                or not origin_current(orch, self.origin) or orch.computer_sessions is not self.manager
                or not session_transition and (not self.session.current(orch) if self.session is not None
                    else self.manager.live_for_host(self.origin.owner, self.origin.host.host_id) is not None)
                or self.session is not None and id(self.controller) != self.session.controller
                or claims.get("sub") != self.origin.owner
                or getattr(orch, "_connection_contexts", {}).get(id(self.controller)) is not self.connection
                or self.connection is None or not self.connection.registered or self.connection.closing
                or self.connection.connection_generation != self.connection_generation
                or self.controller not in getattr(orch, "ui_clients", ())):
            raise _refusal("stale_project_grant", "The original project call was cancelled or superseded.")


def capture_project_bridge(orch, owner, chat, host, session=None, *, allow_rebind=False):
    origin = current_origin(orch, owner, chat, host)
    scope = _CURRENT.get()
    if origin is None or scope is None:
        raise _refusal("stale_project_grant", "The original project dispatch is no longer active.")
    controller = scope[3]
    connection = getattr(orch, "_connection_contexts", {}).get(id(controller))
    if session is not None and not allow_rebind and (session.chat_id != chat or session.project_origin is None
            or session.project_origin.context.project_id != origin.context.project_id
            or session.project_origin.entry != origin.entry):
        raise _refusal("stale_project_grant", "The session belongs to a different project call.")
    lease = ProjectBridgeLease(scope, origin, ProjectSessionWitness.capture(orch, session) if session else None,
        controller, connection, getattr(connection, "connection_generation", None), orch.computer_sessions)
    lease.verify(orch)
    return lease


@asynccontextmanager
async def project_dispatch_scope(orch, websocket, owner, chat, agent, tool, args):
    origins = []
    if agent == "computer-use-1":
        registry = getattr(orch, "computer_hosts", None)
        if registry is not None and any(getattr(host, "project_mode", False) for host in getattr(registry, "online_for_owner", lambda _: [])(owner)):
            if tool == "list_computers":
                hosts = registry.online_for_owner(owner)
            else:
                sessions = getattr(orch, "computer_sessions", None)
                hosts = [sessions.resolve_host(owner, chat, args.get("computer")) if sessions is not None
                         else registry.resolve(owner, args.get("computer"))]
            for host in hosts:
                if getattr(host, "project_mode", False):
                    if (getattr(orch, "ui_sessions", {}).get(websocket) or {}).get("sub") != owner:
                        raise _refusal("project_authority_unavailable", "Sign in to the origin project's account.")
                    origin = await resolve_project_origin(orch, owner, chat, host)
                    normalize_project_arguments(origin, tool, args)
                    origins.append(origin)
    scope = (uuid.uuid4().hex, tuple(origins), asyncio.current_task(), websocket)
    _ACTIVE[scope[0]] = scope
    token = _CURRENT.set(scope)
    try:
        yield
    finally:
        _CURRENT.reset(token)
        _ACTIVE.pop(scope[0], None)


def project_roots(orch, owner, chat, host, *, required=False):
    if not getattr(host, "project_mode", False):
        return host.workspace_roots
    origin = current_origin(orch, owner, chat, host)
    if origin is None:
        raise _refusal("project_authority_unavailable", "The origin project's authority is no longer current.")
    if required:
        origin.require_grant()
    return origin.roots


async def admit_project_manifest(orch, websocket, host, manifest: ProjectManifest):
    connection = getattr(orch, "_connection_contexts", {}).get(id(websocket))
    generation = getattr(connection, "connection_generation", None)
    if websocket is not host.websocket or not _host_current(orch, host, connection, generation):
        raise _refusal("project_authority_unavailable", "Reconnect this Mac before updating its folders.")
    previous = getattr(host, "project_manifest", None)
    highwater = getattr(host, "project_manifest_highwater", 0)
    if manifest.generation < highwater or previous is not None and manifest.generation < previous.generation:
        raise _refusal("stale_project_manifest", "A newer project manifest superseded this update.")
    if previous is not None and manifest.generation == previous.generation:
        if manifest.digest != previous.digest:
            raise _refusal("stale_project_manifest", "A manifest generation cannot change its contents.")
    host.project_manifest_highwater = max(highwater, manifest.generation)
    retirements = getattr(host, "project_retirements", {})
    for entry in manifest.projects:
        old = previous.project(entry.project_id) if previous else None
        if (entry.grant_revision <= retirements.get(entry.project_id, 0)
                or old is not None and entry.grant_revision <= old.grant_revision and entry != old):
            raise _refusal("stale_project_grant", "This folder grant revision has already been retired or changed.")
    try:
        for entry in manifest.projects:
            await asyncio.to_thread(ProjectService(orch.history).get, host.owner_sub, entry.project_id)
    except ProjectError as exc:
        raise _refusal(exc.code, exc.message) from None
    if (not _host_current(orch, host, connection, generation) or host.project_manifest is not previous
            or host.project_manifest_highwater != manifest.generation):
        raise _refusal("stale_project_manifest", "The host connection or a newer manifest superseded this update.")
    retired = dict(retirements)
    for entry in previous.projects if previous else ():
        if manifest.project(entry.project_id) != entry:
            retired[entry.project_id] = max(retired.get(entry.project_id, 0), entry.grant_revision)
    host.project_retirements = retired
    host.project_manifest = manifest
    return previous


async def prepare_project_advertisement(orch, websocket, owner, descriptor):
    claims = getattr(orch, "ui_sessions", {}).get(websocket) or {}
    mode = claims.get("_project_workspaces_v1") is True
    raw = descriptor.get("project_manifest")
    manifest = ProjectManifest.from_dict(raw) if raw is not None else None
    if not mode:
        if manifest is not None:
            raise _refusal("project_authority_unavailable", "Project workspaces were not negotiated.")
        return False, None
    if descriptor.get("workspace_roots"):
        raise _refusal("invalid_project_manifest", "Project hosts cannot advertise legacy folder grants.")
    connection = getattr(orch, "_connection_contexts", {}).get(id(websocket))
    generation = getattr(connection, "connection_generation", None)
    old = orch.computer_hosts.get(owner, descriptor["host_id"])
    old_socket = orch.computer_hosts.host_for_socket(websocket)
    try:
        for entry in manifest.projects if manifest else ():
            await asyncio.to_thread(ProjectService(orch.history).get, owner, entry.project_id)
    except ProjectError as exc:
        raise _refusal(exc.code, exc.message) from None
    if ((getattr(orch, "ui_sessions", {}).get(websocket) or {}) is not claims or claims.get("sub") != owner
            or connection is None or getattr(orch, "_connection_contexts", {}).get(id(websocket)) is not connection
            or connection.closing or not connection.registered or connection.connection_generation != generation
            or orch.computer_hosts.get(owner, descriptor["host_id"]) is not old
            or orch.computer_hosts.host_for_socket(websocket) is not old_socket):
        raise _refusal("project_authority_unavailable", "The host registration changed during project validation.")
    return True, manifest


async def acknowledge_project_manifest(orch, websocket, host_id, manifest, *, error=None):
    import json

    if manifest is None:
        return
    value = {"generation": manifest.generation, "digest": manifest.digest, "accepted": error is None}
    if error is not None:
        value["code"] = error.code
    await orch._safe_send(websocket, json.dumps({"type": "computer_host", "host_id": host_id,
        "state": "online", "project_manifest_ack": value}))


async def replace_project_manifest(orch, websocket, host, raw):
    manifest = ProjectManifest.from_dict(raw)
    try:
        await admit_project_manifest(orch, websocket, host, manifest)
    except ComputerHostError as exc:
        await acknowledge_project_manifest(orch, websocket, host.host_id, manifest, error=exc)
        return False
    sessions = getattr(orch, "computer_sessions", None)
    if sessions is not None:
        for session in list(sessions.live_for_owner(host.owner_sub)):
            origin = getattr(session, "project_origin", None)
            if session.host_id == host.host_id and (origin is None or not origin_current(orch, origin)):
                await sessions.end(session, "consent_revoked")
    if orch.computer_hosts.get(host.owner_sub, host.host_id) is host and host.project_manifest is manifest:
        await acknowledge_project_manifest(orch, websocket, host.host_id, manifest)
    return True


def normalize_project_arguments(origin, tool, args):
    from orchestrator.computer_use_policy import CwdRefusal, resolve_cwd, resolve_workspace_path

    if origin is None or tool not in FILE_VERBS:
        return
    origin.require_grant()
    try:
        if tool in {"read_file", "list_dir", "write_file", "delete_path"}:
            args["path"] = resolve_workspace_path(args.get("path"), origin.roots, origin.host.name, primary=origin.primary)
        elif tool == "run_command":
            args["cwd"] = resolve_cwd(args.get("cwd", origin.primary), origin.roots, origin.host.name)
    except CwdRefusal as exc:
        raise _refusal(exc.code, str(exc)) from None


async def invalidate_deleted_project(orch, owner, project):
    registry = getattr(orch, "computer_hosts", None)
    if registry is None:
        return
    for host in registry.online_for_owner(owner):
        manifest = getattr(host, "project_manifest", None)
        entry = manifest.project(project) if manifest is not None else None
        if entry is not None:
            host.project_retirements[project] = max(host.project_retirements.get(project, 0), entry.grant_revision)
            host.project_manifest = ProjectManifest(manifest.generation,
                tuple(item for item in manifest.projects if item.project_id != project))
    sessions = getattr(orch, "computer_sessions", None)
    if sessions is not None:
        for session in list(sessions.live_for_owner(owner)):
            origin = getattr(session, "project_origin", None)
            if origin is not None and origin.context.project_id == project:
                await sessions.end(session, "consent_revoked")
