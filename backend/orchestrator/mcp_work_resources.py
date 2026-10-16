"""Projects owner-scoped Work status through WorkService for the MCP resource endpoint.
Framework credential fences and the local sharing policy guard every read and delivery.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any

from orchestrator.framework_credentials import FrameworkCaller, FrameworkCredentialService
from orchestrator.framework_work_reads import FrameworkWorkReader
from orchestrator.mcp_authz import MCPAuthError
from orchestrator.work_operations import FrameworkWorkOperations
from persistent_agents.models import AssignmentError
from persistent_agents.store import AssignmentStore
from personalization.phi_gate import PHIGate

RESOURCE_SCOPES = ("mcp:tools:read",)
WORK_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
RESOURCE_URI = re.compile(rf"^astral://work/({WORK_UUID4})/(status)\Z")
RESOURCE_LIST_LIMIT = 50


def _refuse(status=403):
    return MCPAuthError(status, "resource_unavailable", "Work resource is unavailable", RESOURCE_SCOPES)


def _text(value, maximum):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise _refuse(503)
    return value


def bounded_status(work: dict) -> dict:
    if not isinstance(work, dict):
        raise _refuse(503)
    identity = work.get("id")
    if not isinstance(identity, str) or re.fullmatch(WORK_UUID4, identity) is None:
        raise _refuse(503)
    revision = work.get("revision")
    if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
        raise _refuse(503)
    kind = work.get("kind")
    if kind not in (None, "chat", "research"):
        raise _refuse(503)
    moments = {}
    for source, target in (("created_at", "createdAt"), ("updated_at", "updatedAt")):
        value = _text(work.get(source), 64)
        try:
            if datetime.fromisoformat(value).tzinfo is None:
                raise ValueError
        except ValueError:
            raise _refuse(503) from None
        moments[target] = value
    return {
        "id": identity, "title": _text(work.get("title"), 512), "kind": kind,
        "disposition": _text(work.get("disposition"), 64),
        "lifecycle": _text(work.get("lifecycle"), 64),
        "phase": _text(work.get("phase"), 128), "revision": revision, **moments,
    }


class WorkResourceAuthority:
    def __init__(self, orchestrator: Any, user_id: str, claims: dict):
        self.orchestrator = orchestrator
        self.ops = getattr(orchestrator, "framework_work_operations", None)
        self.caller = claims.get("_framework_caller") if isinstance(claims, dict) else None
        if (not isinstance(self.ops, FrameworkWorkOperations)
                or not isinstance(self.caller, FrameworkCaller)
                or user_id != self.caller.owner_id or claims.get("sub") != user_id
                or not self.caller.has_scope("operations.read")
                or any(claims.get(key) for key in
                       ("act", "delegated", "machine_class", "machine_turn_class", "_machine_turn"))):
            raise _refuse()
        self.credentials = getattr(orchestrator, "framework_credentials", None)
        self.assignments = self.ops.assignments
        self.store = self.ops.store
        self.gate = getattr(self.assignments, "phi_gate", None)
        self._bound()
        self.reader = FrameworkWorkReader(self.ops, self.caller, "operations.read")
        self.service = self.reader.service

    def _bound(self):
        if (getattr(self.orchestrator, "framework_work_operations", None) is not self.ops
                or getattr(self.orchestrator, "framework_credentials", None) is not self.credentials
                or not isinstance(self.credentials, FrameworkCredentialService)
                or self.ops.credentials is not self.credentials
                or self.ops.assignments is not self.assignments
                or self.assignments.store is not self.store or self.ops.store is not self.store
                or not isinstance(self.store, AssignmentStore)
                or self.credentials.plane_runtime is not self.store.plane_runtime
                or self.store.repository is not self.store.plane_runtime.repositories.assignments
                or getattr(self.assignments, "phi_gate", None) is not self.gate
                or not isinstance(self.gate, PHIGate) or not self.gate.available):
            raise _refuse()

    async def recheck(self):
        self._bound()
        try:
            await self.reader.recheck()
        except AssignmentError as exc:
            raise _refuse(exc.status_code) from None

    async def _share(self, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 262_144:
            raise _refuse(503)
        try:
            unsafe = await asyncio.to_thread(self.gate.contains_phi_for_sharing, payload)
        except Exception:
            raise _refuse() from None
        if unsafe is not False:
            raise _refuse()
        await self.recheck()
        return value

    async def list(self):
        try:
            page = await self.service.list(
                self.caller.owner_id, {"sub": self.caller.owner_id}, limit=RESOURCE_LIST_LIMIT,
            )
        except AssignmentError as exc:
            raise _refuse(exc.status_code) from None
        records = page.get("operations") if isinstance(page, dict) else None
        if not isinstance(records, list) or len(records) > RESOURCE_LIST_LIMIT:
            raise _refuse(503)
        resources = []
        for record in records:
            view = bounded_status(record)
            resources.append({
                "uri": f"astral://work/{view['id']}/status", "name": f"Status: {view['title']}",
                "mimeType": "application/json", "description": f"Status for work item {view['id']}",
            })
        return await self._share(resources)

    async def read(self, uri):
        match = RESOURCE_URI.fullmatch(uri) if isinstance(uri, str) else None
        if match is None:
            raise MCPAuthError(400, "invalid_uri", "URI must be astral://work/{uuid4}/status", RESOURCE_SCOPES)
        try:
            record = await self.service.get(
                self.caller.owner_id, {"sub": self.caller.owner_id}, match.group(1),
            )
        except AssignmentError as exc:
            raise _refuse(exc.status_code) from None
        view = bounded_status(record)
        if view["id"] != match.group(1):
            raise _refuse(503)
        return await self._share({
            "uri": uri, "name": f"Status: {view['title']}", "mimeType": "application/json",
            "boundedViewModel": view,
        })
