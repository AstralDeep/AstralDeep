"""In-process driver: the deterministic CI merge gate that drives a real Orchestrator
with a scripted LLM (backend/verification/drivers/scripted_llm.py) and a loopback MCP
agent, running the product's real gates, audit, workspace, and web render.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from verification.config import RunConfig
from verification.evidence import CapturedEvidence, flatten_components
from verification.isolation import Principal, teardown
from verification.personas import Fixture, materialize
from verification.scenarios import Scenario
from verification.drivers.scripted_llm import scripted_llm_for

logger = logging.getLogger("verification.in_process")

AGENT_ID = "general-verif-1"
READ_SCOPES = {"tools:read": True, "tools:search": True, "tools:files": True}


class CaptureSocket:
    def __init__(self, label: str = "capture") -> None:
        self.label = label
        self.outputs: List[Dict[str, Any]] = []
        self._closed = False

    async def send_text(self, data: str) -> None:
        if self._closed:
            return
        try:
            self.outputs.append(json.loads(data))
        except (json.JSONDecodeError, TypeError):
            self.outputs.append({"type": "raw", "data": data})

    async def send_json(self, data: Any, mode: str = "text") -> None:
        if self._closed:
            return
        if isinstance(data, dict):
            self.outputs.append(data)
        else:
            await self.send_text(str(data))

    async def receive_text(self) -> str:
        return ""

    async def close(self, code: int = 1000) -> None:
        self._closed = True

    @property
    def client(self):
        return ("verif", self.label)


class LoopbackAgent:
    def __init__(self, orch: Any, server: Any) -> None:
        self.orch = orch
        self.server = server

    async def send(self, data: str) -> None:
        from shared.protocol import MCPRequest

        obj = json.loads(data)
        req = MCPRequest(
            request_id=obj.get("request_id", ""),
            method=obj.get("method", ""),
            params=obj.get("params", {}) or {},
        )
        resp = await _run_sync(self.server.process_request, req)
        fut = self.orch.pending_requests.get(req.request_id)
        if fut is not None and not fut.done():
            fut.set_result(resp)

    async def close(self, *a, **k) -> None:
        return None


class InProcessDriver:
    mode = "in_process"
    auth_mode = "mock_inprocess"

    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.orch: Any = None
        self.agent_id = AGENT_ID
        self._tmp = os.path.join(config.run_dir, "fixtures")
        self._uploaded_blob_owners: set[str] = set()
        self._execution_nonce = uuid.uuid4().hex[:16]
        self._execution_principals: dict[str, Principal] = {}
        self._teardown_task: asyncio.Task[None] | None = None
        self._fixture_identity = None

    async def setup(self) -> None:
        os.environ["FF_UI_DESIGNER"] = "false"

        from verification.drivers.fixture_identity import FixtureIdentity

        if self.config.mode != "in_process":
            raise ValueError("in-process driver requires the in_process configuration")
        self._fixture_identity = FixtureIdentity(self.config.run_id)

        from orchestrator.orchestrator import Orchestrator

        # Never cancel to_thread directly — it can abandon a live graph.
        construction = asyncio.create_task(
            asyncio.to_thread(Orchestrator),
            name="in-process-verification-construction",
        )
        error, cancellation = await _observe_task_through_cancellation(construction)
        if error is not None:
            self._fixture_identity.close()
            raise error
        self.orch = construction.result()
        try:
            if cancellation is not None:
                raise cancellation
            from pathlib import Path
            from orchestrator.knowledge_synthesis import KnowledgeIndex

            knowledge = Path(self._tmp) / "knowledge"
            await asyncio.to_thread(knowledge.mkdir, parents=True, exist_ok=True)
            self.orch.knowledge_index = await asyncio.to_thread(KnowledgeIndex, str(knowledge))
            self._register_general_agent()
            self.orch.runtime_composition.start()
        except BaseException:
            orch = self.orch
            self.orch = None
            try:
                await _close_owned_orchestrator_graph(orch)
            except BaseException:
                logger.exception("verification setup rollback failed")
            finally:
                self._fixture_identity.close()
            raise

    def _plane_dependencies(self, orchestrator: Any | None = None) -> tuple[Any, Any, Any]:
        target = self.orch if orchestrator is None else orchestrator
        composition = getattr(target, "runtime_composition", None)
        plane = getattr(composition, "plane", None)
        runtime = getattr(plane, "runtime", None)
        repositories = getattr(plane, "repositories", None)
        blobs = getattr(plane, "blobs", None)
        if (
            not callable(getattr(runtime, "transaction", None))
            or repositories is None
            or blobs is None
        ):
            raise RuntimeError(
                "in-process verification requires the orchestrator application Plane"
            )
        return runtime, repositories, blobs

    def _execution_principal(self, principal: Principal) -> Principal:
        existing = self._execution_principals.get(principal.user_id)
        if existing is not None:
            if tuple(existing.roles) != tuple(principal.roles):
                raise RuntimeError("verification principal roles changed within one run")
            return existing
        expected_prefix = f"{self.config.run_id}_"
        if not principal.user_id.startswith(expected_prefix):
            raise ValueError("verification principal is outside the configured run namespace")
        identity_digest = hashlib.sha256(principal.user_id.encode("utf-8")).hexdigest()[:12]
        suffix = f"_exec_{self._execution_nonce}_{identity_digest}"
        maximum_base = 255 - len(suffix)
        if len(expected_prefix) > maximum_base:
            raise ValueError("verification run namespace is too long for blob ownership")
        scoped_id = f"{principal.user_id[:maximum_base]}{suffix}"
        scoped = Principal(user_id=scoped_id, roles=list(principal.roles))
        self._execution_principals[principal.user_id] = scoped
        self._execution_principals[scoped_id] = scoped
        return scoped

    async def _seed_llm_config(self, user_id: str) -> None:
        store = getattr(self.orch, "_llm_store", None)
        if store is None:  # pragma: no cover
            return
        try:
            await _run_sync(
                store.set_sync,
                user_id,
                provider="custom",
                base_url="http://verif.invalid/v1",
                model="verif-scripted-model",
                api_key="verif-scripted-key",
            )
        except Exception:
            logger.warning("verification: llm config seeding failed", exc_info=True)

        # Dev-only stub: delegation calls fail open here, unlike production.
        async def _no_delegation(*_a, **_k):
            return None

        self.orch._get_delegation_token = _no_delegation  # type: ignore[assignment]

    def _register_general_agent(self) -> None:
        from agents.general.mcp_server import MCPServer
        from agents.general.mcp_tools import TOOL_REGISTRY
        from shared.protocol import AgentCard, AgentSkill

        server = MCPServer()
        skills: List[AgentSkill] = []
        scope_map: Dict[str, str] = {}
        for name, info in TOOL_REGISTRY.items():
            skills.append(
                AgentSkill(
                    name=name,
                    description=info.get("description", ""),
                    id=name,
                    input_schema=info.get("input_schema", {"type": "object", "properties": {}}),
                )
            )
            scope_map[name] = info.get("scope", "tools:read")

        self.orch.agent_cards[self.agent_id] = AgentCard(
            name="General (verification loopback)",
            description="In-process loopback of the general agent for the harness.",
            agent_id=self.agent_id,
            skills=skills,
        )
        self.orch.agents[self.agent_id] = LoopbackAgent(self.orch, server)
        self.orch.tool_permissions.register_tool_scopes(self.agent_id, scope_map)

    async def upload_as(self, principal: Principal, fixture: Fixture) -> Dict[str, Any]:
        from orchestrator.attachments import content_type as ct
        from orchestrator.attachments.materialization import (
            materialization_service_from_orchestrator,
        )

        principal = self._execution_principal(principal)
        path = await asyncio.to_thread(
            materialize,
            fixture,
            os.path.join(self._tmp, principal.user_id),
        )
        ext = ct.normalise_extension(fixture.filename)
        category = ct.category_for_extension(ext) or fixture.category
        attachment_id = str(uuid.uuid4())
        try:
            max_bytes = ct.max_bytes_for_category(category)
        except Exception:
            max_bytes = 100 * 1024 * 1024

        async def _chunks():
            fh = await asyncio.to_thread(open, path, "rb")
            try:
                while True:
                    buf = await asyncio.to_thread(fh.read, 262144)
                    if not buf:
                        break
                    yield buf
            finally:
                await asyncio.to_thread(fh.close)

        materializations = materialization_service_from_orchestrator(self.orch)
        owners = getattr(self, "_uploaded_blob_owners", None)
        if owners is None:
            owners = self._uploaded_blob_owners = set()
        owners.add(principal.user_id)
        record = await materializations.materialize_stream(
            owner_id=principal.user_id,
            attachment_id=attachment_id,
            filename=fixture.filename,
            category=category,
            extension=ext,
            chunks=_chunks(),
            max_bytes=max_bytes,
            resolve_content_type=ct.sniff_content_type,
        )
        if record.attachment_id != attachment_id:
            raise RuntimeError("Plane returned a different verification attachment identity")
        return {
            "attachment_id": attachment_id,
            "filename": fixture.filename,
            "category": category,
            "path": path,
        }

    async def _register_session(self, principal: Principal, chat_id: Optional[str] = None) -> CaptureSocket:
        principal = self._execution_principal(principal)
        await self._seed_llm_config(principal.user_id)
        ws = CaptureSocket(label=principal.user_id)
        claims, token = self._fixture_identity.claims_and_token(principal.claims())
        self.orch.ui_sessions[ws] = claims | {"_raw_token": token}
        ws.scope = {"type": "websocket", "headers": [], "query_string": b""}
        context = self.orch._new_connection_context(ws)
        context.registered = True
        context.connection_generation = uuid.uuid4()
        self.orch.ui_clients.append(ws)
        if chat_id is not None:
            self.orch._ws_active_chat[id(ws)] = chat_id
        return ws

    def _drop_session(self, ws: CaptureSocket) -> None:
        try:
            self.orch.ui_clients.remove(ws)
        except ValueError:
            pass
        self.orch.ui_sessions.pop(ws, None)
        self.orch._ws_active_chat.pop(id(ws), None)
        context = self.orch._connection_contexts.pop(id(ws), None)
        if context is not None:
            context.closing = True

    async def _send_registered_turn(self, ws, message, chat_id, *, user_id, attachments):
        from orchestrator.human_request_authority import capture_human_socket_request
        from verification.drivers.fixture_admission import admitted_registered_turn

        context = self.orch._connection_contexts[id(ws)]
        frame = {"type": "ui_event", "action": "chat_message",
                 "submission_id": str(uuid.uuid4()), "request_generation": str(uuid.uuid4()),
                 "connection_generation": str(context.connection_generation),
                 "message": message, "chat_id": chat_id}
        pending = capture_human_socket_request(
            self.orch.human_request_boundary, websocket=ws, context=context,
            message=frame, purpose="skill_lookup",
        )
        try:
            async with admitted_registered_turn(self.orch, ws, frame=frame, human_request=pending) as context:
                return await self.orch.handle_chat_message(
                    ws, message, chat_id, user_id=user_id, attachments=attachments,
                    operation_context=context,
                )
        finally:
            pending.close()

    async def grant_default_scopes(self, principal: Principal) -> None:
        principal = self._execution_principal(principal)
        await _run_sync(
            self.orch.tool_permissions.set_agent_scopes,
            principal.user_id, self.agent_id, dict(READ_SCOPES)
        )

    async def set_scope(self, principal: Principal, agent_id: str, scope: str, enabled: bool) -> None:
        principal = self._execution_principal(principal)
        await _run_sync(
            self.orch.tool_permissions.set_agent_scopes,
            principal.user_id, agent_id or self.agent_id, {scope: enabled}
        )

    async def run_scenario(self, scenario: Scenario) -> CapturedEvidence:
        p = self._execution_principal(scenario.principal)
        persona = scenario.persona
        await self.grant_default_scopes(p)
        att = await self.upload_as(p, persona.fixture)
        chat_id = await _run_sync(self.orch.history.create_chat, user_id=p.user_id)
        ws = await self._register_session(p, chat_id)
        self.orch._call_llm = scripted_llm_for(persona, att["attachment_id"], att["path"])
        attachments = [
            {
                "attachment_id": att["attachment_id"],
                "filename": att["filename"],
                "category": att["category"],
            }
        ]
        try:
            await self._send_registered_turn(
                ws, persona.query, chat_id, user_id=p.user_id, attachments=attachments
            )
            messages = list(ws.outputs)
            components = flatten_components(messages)
            workspace_state = await _run_sync(
                self.orch.workspace.live_components, chat_id, p.user_id
            )
            audit_rows, chain_ok = await _run_sync(self._read_audit, p.user_id)
            return CapturedEvidence(
                evidence_id=f"{scenario.scenario_id}:ev",
                scenario_id=scenario.scenario_id,
                run_mode=self.auth_mode,
                messages=messages,
                components=components,
                workspace_state=workspace_state,
                audit_rows=audit_rows,
                audit_chain_ok=chain_ok,
                extra={
                    "attachment_id": att["attachment_id"],
                    "chat_id": chat_id,
                    "file_category": att["category"],
                    "synthetic_only": True,
                },
            )
        finally:
            self._drop_session(ws)

    async def reference_attachment_as(
        self, principal: Principal, attachment_id: str, filename: str
    ) -> CapturedEvidence:
        principal = self._execution_principal(principal)
        await self.grant_default_scopes(principal)
        chat_id = await _run_sync(self.orch.history.create_chat, user_id=principal.user_id)
        ws = await self._register_session(principal, chat_id)
        import types as _types

        async def _no_tools(websocket, messages, tools_desc=None, temperature=None,
                            feature="tool_dispatch"):
            return _types.SimpleNamespace(content="ack", tool_calls=None,
                                          reasoning_content=None), _types.SimpleNamespace(
                total_tokens=0, prompt_tokens=0, completion_tokens=0)

        self.orch._call_llm = _no_tools
        attachments = [{"attachment_id": attachment_id, "filename": filename,
                        "category": "spreadsheet"}]
        try:
            await self._send_registered_turn(
                ws, "Use the attached file.", chat_id,
                user_id=principal.user_id, attachments=attachments,
            )
            messages = list(ws.outputs)
            audit_rows, chain_ok = await _run_sync(self._read_audit, principal.user_id)
            return CapturedEvidence(
                evidence_id=f"xuser:{principal.user_id}:ev",
                scenario_id=f"xuser:{principal.user_id}",
                run_mode=self.auth_mode,
                messages=messages,
                components=flatten_components(messages),
                audit_rows=audit_rows,
                audit_chain_ok=chain_ok,
                extra={"referenced_attachment_id": attachment_id, "chat_id": chat_id},
            )
        finally:
            self._drop_session(ws)

    async def probe_cross_user(self, run_id: str) -> CapturedEvidence:
        from verification.isolation import make_principal
        from verification.personas import get_persona

        a = self._execution_principal(make_principal(run_id, "xuserA"))
        b = self._execution_principal(make_principal(run_id, "xuserB"))
        await self.grant_default_scopes(a)
        await self.grant_default_scopes(b)
        persona = get_persona("everyday")
        att = await self.upload_as(a, persona.fixture)
        ev = await self.reference_attachment_as(b, att["attachment_id"], att["filename"])
        leaked = any(m in json.dumps(ev.messages) for m in persona.fixture.known_markers)
        b_ws = await _run_sync(
            self.orch.workspace.live_components, (ev.extra or {}).get("chat_id", ""), b.user_id
        )
        ev.extra.update(
            {
                "victim": a.user_id,
                "attacker": b.user_id,
                "attachment_id": att["attachment_id"],
                "leaked_markers": leaked,
                "attacker_workspace_size": len(b_ws),
            }
        )
        return ev

    async def probe_scope_withheld(self, run_id: str) -> CapturedEvidence:
        from verification.isolation import make_principal
        from verification.personas import get_persona

        c = self._execution_principal(make_principal(run_id, "scopeC"))
        await _run_sync(
            self.orch.tool_permissions.set_agent_scopes,
            c.user_id, self.agent_id,
            {"tools:read": False, "tools:search": False, "tools:files": False},
        )
        persona = get_persona("everyday")
        att = await self.upload_as(c, persona.fixture)
        chat_id = await _run_sync(self.orch.history.create_chat, user_id=c.user_id)
        ws = await self._register_session(c, chat_id)
        self.orch._call_llm = scripted_llm_for(persona, att["attachment_id"], att["path"])
        try:
            await self._send_registered_turn(
                ws, persona.query, chat_id, user_id=c.user_id,
                attachments=[{"attachment_id": att["attachment_id"],
                              "filename": att["filename"], "category": att["category"]}],
            )
            audit_rows, chain = await _run_sync(self._read_audit, c.user_id)
            read_ok = any(
                r.get("event_class") == "agent_tool_call" and r.get("outcome") == "success"
                and str(r.get("action_type") or "").startswith("tool.read_")
                for r in audit_rows
            )
            return CapturedEvidence(
                evidence_id=f"scope:{c.user_id}", scenario_id="authz:scope_withheld",
                run_mode=self.auth_mode, messages=list(ws.outputs),
                components=flatten_components(ws.outputs), audit_rows=audit_rows,
                audit_chain_ok=chain, extra={"read_success": read_ok, "withheld": not read_ok},
            )
        finally:
            self._drop_session(ws)

    def probe_delegation(self, run_id: str) -> CapturedEvidence:
        from audit.hooks import actor_principal_from_claims
        from verification.isolation import make_principal

        d = self._execution_principal(make_principal(run_id, "delegD"))
        claims: Dict[str, Any]
        try:
            from jose import jwt

            tok = self.orch.delegation._create_mock_delegation_token(
                self.agent_id, ["read_spreadsheet"], d.user_id, ["tools:read"]
            )
            claims = jwt.get_unverified_claims(tok["access_token"])
        except Exception:
            logger.debug("mock delegation minting unavailable; using claim shape", exc_info=True)
            claims = {"sub": d.user_id, "act": {"sub": f"agent:{self.agent_id}"},
                      "scope": "tools:read"}
        actor, principal = actor_principal_from_claims(claims)
        return CapturedEvidence(
            evidence_id=f"deleg:{d.user_id}", scenario_id="authz:delegation",
            run_mode=self.auth_mode,
            extra={
                "sub": claims.get("sub"),
                "act_sub": (claims.get("act") or {}).get("sub"),
                "actor_user_id": actor,
                "auth_principal": principal,
                "scope": claims.get("scope"),
            },
        )

    async def probe_admin_approval(self, run_id: str) -> CapturedEvidence:
        import uuid as _uuid

        from orchestrator import agentic_creation
        from verification.isolation import make_principal

        owner = self._execution_principal(make_principal(run_id, "apprOwner"))
        other = self._execution_principal(make_principal(run_id, "apprOther"))
        draft_id = str(_uuid.uuid4())
        draft_store = agentic_creation._draft_store(self.orch)
        payload = {"draft_id": draft_id}
        ws_owner = None
        ws_other = None
        try:
            await _run_sync(
                draft_store.create_draft_agent,
                draft_id, owner.user_id, "ZZV Parser",
                f"zzv_parser_{_uuid.uuid4().hex[:6]}", "Synthetic verification parser draft",
                origin="auto_attachment",
            )
            ws_owner = await self._register_session(owner)
            ws_other = await self._register_session(other)
            r_owner = await agentic_creation._h_draft_approve(
                self.orch, ws_owner, owner.user_id, ["user"], payload
            )
            r_other = await agentic_creation._h_draft_approve(
                self.orch, ws_other, other.user_id, ["user"], payload
            )
            audit_rows, chain = await _run_sync(self._read_audit, owner.user_id)
            rejected_audited = any(
                r.get("action_type") == "lifecycle.rejected" for r in audit_rows
            )
            return CapturedEvidence(
                evidence_id=f"appr:{owner.user_id}", scenario_id="authz:admin_approval",
                run_mode=self.auth_mode, messages=list(ws_owner.outputs),
                audit_rows=audit_rows, audit_chain_ok=chain,
                extra={
                    "owner_refused": r_owner is None,
                    "other_refused": r_other is None,
                    "rejected_audited": rejected_audited,
                    "draft_id": draft_id,
                },
            )
        finally:
            if ws_owner is not None:
                self._drop_session(ws_owner)
            if ws_other is not None:
                self._drop_session(ws_other)
            await _run_sync(draft_store.delete_draft_agent, draft_id)

    def enrich_thin_client(self, ev: CapturedEvidence) -> CapturedEvidence:
        from verification.checks.thin_client import inspect_client_surface

        ev.client_inspection = inspect_client_surface()
        comps = [c for c in ev.components if isinstance(c, dict)]
        try:
            from rote.adapter import ComponentAdapter
            from rote.capabilities import DeviceProfile

            try:
                browser = DeviceProfile.default()
            except Exception:
                browser = DeviceProfile.from_dict({"device_type": "browser"})
            mobile = DeviceProfile.from_dict(
                {"device_type": "mobile", "viewport_width": 375, "viewport_height": 667}
            )
            b = ComponentAdapter.adapt(comps, browser)
            m = ComponentAdapter.adapt(comps, mobile)
            ev.device_diff = {
                "backend_adapted": True,
                "browser_types": sorted({c.get("type") for c in b if isinstance(c, dict)}),
                "mobile_types": sorted({c.get("type") for c in m if isinstance(c, dict)}),
            }
        except Exception:
            logger.debug("device adaptation comparison failed", exc_info=True)
            ev.device_diff = {"backend_adapted": True}
        return ev

    def _read_audit(self, user_id: str):
        rows: List[Dict[str, Any]] = []
        try:
            result = self.orch.audit_repo.list_for_user(user_id, limit=200)
            dtos = result[0] if isinstance(result, tuple) else result
            for d in dtos:
                rows.append(
                    {
                        "action_type": getattr(d, "action_type", None),
                        "event_class": getattr(d, "event_class", None),
                        "actor_user_id": getattr(d, "actor_user_id", None),
                        "auth_principal": getattr(d, "auth_principal", None),
                        "outcome": getattr(d, "outcome", None),
                        "correlation_id": str(getattr(d, "correlation_id", "") or ""),
                        "agent_id": getattr(d, "agent_id", None),
                    }
                )
        except Exception:
            logger.exception("audit read failed for %s", user_id)
        chain_ok: Any = True
        try:
            bad = self.orch.audit_repo.verify_chain(user_id)
            chain_ok = True if bad is None else str(bad)
        except Exception:
            logger.exception("audit chain verify failed for %s", user_id)
            chain_ok = "verify_error"
        return rows, chain_ok

    async def teardown(self) -> None:
        task = getattr(self, "_teardown_task", None)
        if task is None:
            orch = self.orch
            if orch is None:
                return
            self.orch = None
            task = asyncio.create_task(
                self._teardown_owned_graph(orch),
                name="in-process-verification-teardown",
            )
            self._teardown_task = task
        error, cancellation = await _observe_task_through_cancellation(task)
        if getattr(self, "_fixture_identity", None) is not None:
            self._fixture_identity.close()
        if error is not None:
            raise error
        if cancellation is not None:
            raise cancellation

    async def _teardown_owned_graph(self, orch: Any) -> None:
        errors: list[BaseException] = []
        plane_runtime = None
        plane_repositories = None
        purges = None
        try:
            plane_runtime, plane_repositories, _blobs = self._plane_dependencies(orch)
            from orchestrator.attachments.purge import (
                purge_coordinator_from_orchestrator,
            )

            purges = purge_coordinator_from_orchestrator(orch)
        except BaseException as error:
            errors.append(error)

        accepted_owners = 0
        if purges is not None:
            owners = tuple(sorted(getattr(self, "_uploaded_blob_owners", set())))
            for owner_id in owners:
                try:
                    await purges.aschedule_owner(owner_id=owner_id)
                    accepted_owners += 1
                except BaseException as error:
                    errors.append(error)

            for _index in range(accepted_owners):
                try:
                    await purges.areconcile_once(fail_on_incomplete=True)
                except BaseException as error:
                    errors.append(error)

        if not errors and purges is not None and plane_runtime is not None:
            try:
                await asyncio.to_thread(
                    _assert_verification_purge_ready,
                    plane_runtime,
                    purges,
                )
            except BaseException as error:
                errors.append(error)

        if not errors and plane_runtime is not None and plane_repositories is not None:
            try:
                await asyncio.to_thread(
                    teardown,
                    plane_runtime=plane_runtime,
                    plane_repositories=plane_repositories,
                    run_id=self.config.run_id,
                )
            except BaseException as error:
                errors.append(error)

        try:
            await _close_owned_orchestrator_graph(orch)
        except BaseException as close_error:
            errors.append(close_error)

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(
                "verification cleanup and graph close reported failures",
                errors,
            )


async def _close_owned_orchestrator_graph(orchestrator: Any) -> None:
    unified_close = getattr(orchestrator, "_close_started_services", None)
    if callable(unified_close):
        task = asyncio.create_task(unified_close())
        error, cancellation = await _observe_task_through_cancellation(task)
        if error is not None:
            raise error
        if cancellation is not None:
            raise cancellation
        return

    cancellation: asyncio.CancelledError | None = None
    errors: list[BaseException] = []
    voice_services = getattr(orchestrator, "voice_services", None)
    runtime_composition = getattr(orchestrator, "runtime_composition", None)
    for component in (voice_services, runtime_composition):
        close = getattr(component, "close", None)
        if not callable(close):
            continue
        task = asyncio.create_task(close())
        error, observed_cancellation = await _observe_task_through_cancellation(task)
        cancellation = cancellation or observed_cancellation
        if error is not None:
            errors.append(error)
    if errors:
        for secondary in errors[1:]:
            logger.error(
                "verification graph close reported an additional failure",
                extra={"close_error_type": type(secondary).__name__},
            )
        raise errors[0]
    if cancellation is not None:
        raise cancellation


def _assert_verification_purge_ready(plane_runtime: Any, purges: Any) -> None:
    with plane_runtime.transaction() as transaction:
        purges.assert_globally_ready(transaction)


async def _run_sync(callback, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    error, cancellation = await _observe_task_through_cancellation(task)
    if error is not None:
        raise error
    if cancellation is not None:
        raise cancellation
    return task.result()


async def _observe_task_through_cancellation(
    task: asyncio.Task[Any],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except BaseException:
            break
    try:
        task.result()
    except BaseException as error:
        return error, cancellation
    return None, cancellation
