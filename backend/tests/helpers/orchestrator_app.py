"""Builds the HTTP app through the orchestrator's own Orchestrator.build_http_app on a bare host with every flag-gated surface switched on, so node tests reach each route through the real route table.
test_node_route_census.py, test_node_bearer_refusal.py, test_node_mesh_offer.py and test_mesh_admission.py use it, and a PostgreSQL runtime from voice_plane_runtime.py backs the A2A publication store."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI

from audit.repository import AuditRepository
from orchestrator.orchestrator import Orchestrator
from orchestrator.session_store import WebSessionStore
from orchestrator.voice_bootstrap import VoiceSpeechBackend
from orchestrator.voice_coordinator import WorkerPool, WorkerPoolPolicy
from orchestrator.voice_worker_endpoint import WorkerControlSettings, install_router
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from personalization.phi_gate import PHIGate
from shared.feature_flags import flags

SURFACES = ("mcp_server", "a2a_server", "kiosk_login", "remote_mcp")
PUBLIC_BASE_URL = "https://node.invalid"


def _voice_services() -> SimpleNamespace:
    pool = WorkerPool(WorkerPoolPolicy(runtime_closure_sha256="a" * 64))
    settings = WorkerControlSettings(secret=b"w" * 32)
    return SimpleNamespace(
        speech_backend=VoiceSpeechBackend.LLM_FACTORY,
        install_worker_control=lambda app, environ=None: install_router(app, pool, settings=settings),
    )


class _Permissions:
    def list_disabled_agents(self, user_id):
        return ()

    def is_tool_allowed(self, user_id, agent_id, tool_name):
        return False

    def _is_safe_agent(self, agent_id):
        return False


def host(plane_runtime: Any = None, **attributes: Any) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    if plane_runtime is not None:
        orch.plane_repository_source = plane_runtime
        orch.audit_repo = AuditRepository(plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories)
        orch.runtime_composition = SimpleNamespace(
            plane=SimpleNamespace(runtime=plane_runtime, repositories=plane_runtime.repositories))
        orch.web_sessions = WebSessionStore(plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories)
        orch.persistent_assignments = AssignmentService(
            orch, AssignmentStore(plane_runtime=plane_runtime), enabled=True,
            phi_gate=PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])))
    orch.agent_cards = {}
    orch.agents = {}
    orch.local_agents = {}
    orch.a2a_agent_owners = {}
    orch.security_flags = {}
    orch.tool_permissions = _Permissions()
    orch._is_draft_agent = lambda agent_id: False
    orch.voice_services = _voice_services()
    orch.ui_clients = []
    orch.ui_sessions = {}
    orch._registered_events = {}
    orch._stream_tasks = {}
    orch._stream_subs = {}
    orch.stream_manager = None
    orch._ws_active_chat = {}
    orch._ws_timeline_mode = {}
    orch._ws_welcome = {}
    orch._chat_locks = {}
    orch.rote = SimpleNamespace(cleanup=lambda websocket: None)
    for name, value in attributes.items():
        setattr(orch, name, value)
    return orch


def build(monkeypatch: Any, orch: Orchestrator, surfaces: tuple = SURFACES) -> FastAPI:
    for name in SURFACES:
        monkeypatch.setitem(flags._flags, name, name in surfaces)
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "node-app-audit-key-with-32-characters")
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "node_app")
    app = orch.build_http_app()
    paths = {getattr(route, "path", None) for route in app.router.routes}
    if "a2a_server" in surfaces and not {"/a2a", "/a2a/.well-known/agent-card.json"} <= paths:
        raise AssertionError("the A2A surface did not mount")
    return app
