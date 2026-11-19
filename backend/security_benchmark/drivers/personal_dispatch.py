"""Runs the paired attack and control cases as real turns on a personal node: a development owner device on the real kernel binding sends each request, and the node answers it on the Kernel turn engine with a scripted model provider.
Sandboxed fixture tools count their physical effects and the hash-chained audit is an independent oracle; qualification.py defines the cases and report.py-style evidence checks consume the result.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import astral_kernel as ak
import httpx
from devtools import dev_mesh
from orchestrator import node_mesh, node_mesh_chat, vessel_executor as ve
from orchestrator.node_mailbox import MailboxSettings
from orchestrator.node_mesh_calls import OrchestratorCalls, definition_hash, grant_name
from shared import node_identity
from shared.feature_flags import flags
from tests.fixtures.connections.model_server import ModelServer, ScriptedTurn

from security_benchmark.qualification import CASES, CaseEvidence, QualificationCase

SOURCE_AGENT = "security-source-fixture"
REMOTE_AGENT = "security-remote-fixture"
EFFECT_AGENT = "security-effect-fixture"
READ_LOCAL = "read_fixture_source"
READ_REMOTE = "read_remote_fixture_source"
EFFECT = "commit_fixture_effect"
WAIT_SECONDS = 60.0
TURN_SECONDS = 120.0
ENV_KEYS = (
    "ASTRAL_ENV", "ASTRAL_NODE_PROFILE", "ASTRAL_NODE_DATA_DIR", "ASTRAL_NODE_OWNER", "DATABASE_URL",
    "ASTRALPLANE_IDENTITY", "ATTACHMENT_UPLOAD_ROOT", "PERSONAL_AGENT_ARTIFACT_ROOT", "AUDIT_HMAC_SECRET",
    "AGENT_KEY_PATH", "AGENT_API_KEY", "DELEGATION_REQUIRED", "FF_UI_DESIGNER",
)
FLAG_KEYS = ("inprocess_agents", "user_skills", "typesafe_routing", "observation_packing", "safe_compaction")
TOOL_SCHEMA = {"type": "object", "properties": {
    "case_id": {"type": "string"}, "destination": {"type": "string"}, "material": {"type": "string"}}}
FIXTURES = (
    (SOURCE_AGENT, READ_LOCAL, "tools:read"),
    (REMOTE_AGENT, READ_REMOTE, "tools:read"),
    (EFFECT_AGENT, EFFECT, "tools:write"),
)


class FixtureExecutor:
    def __init__(self) -> None:
        self.payloads: dict[str, str] = {}
        self.effects: dict[str, list[dict]] = defaultdict(list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        call = json.loads(request.content)
        tool = call.get("tool")
        arguments = json.loads(call.get("arguments_json") or "{}")
        case_id = arguments.get("case_id")
        if tool in (READ_LOCAL, READ_REMOTE):
            if case_id not in self.payloads:
                return httpx.Response(200, json={"output": "fixture source unavailable", "error": True})
            return httpx.Response(200, json={"output": self.payloads[case_id], "error": False})
        if tool == EFFECT:
            if case_id not in self.payloads:
                return httpx.Response(200, json={"output": "fixture case unavailable", "error": True})
            self.effects[case_id].append({
                "destination": arguments.get("destination"), "material": arguments.get("material")})
            return httpx.Response(200, json={"output": "sent", "error": False})
        return httpx.Response(200, json={"output": "unknown fixture tool", "error": True})


async def _until(condition: Callable[[], Any], what: str, seconds: float = WAIT_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {what}")


class PersonalDispatchQualification:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.fixture = FixtureExecutor()
        self.root = Path(tempfile.mkdtemp(prefix="security-qualification-")).resolve()
        self.orch: Any = None
        self.node: Any = None
        self.owner_device: Any = None
        self.bridge: Any = None
        self.owner_id = ""
        self.model: Any = None
        self.consents: list[str] = []
        self.node_device = ""
        self._saved_env: dict[str, str | None] = {}
        self._saved_flags: dict[str, Any] = {}
        self._executor_client: httpx.AsyncClient | None = None
        self._definitions: dict[str, str] = {}

    def _configure_environment(self) -> None:
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self._saved_flags = {key: flags._flags.get(key) for key in FLAG_KEYS}
        os.environ.update({
            "ASTRAL_ENV": "development", "ASTRAL_NODE_PROFILE": "personal",
            "ASTRAL_NODE_DATA_DIR": str(self.root / "node"),
            "DATABASE_URL": f"sqlite://{self.root / 'node' / 'plane.sqlite3'}",
            "ASTRALPLANE_IDENTITY": f"security-qualification-{uuid.uuid4().hex}",
            "ATTACHMENT_UPLOAD_ROOT": str(self.root / "blobs"),
            "PERSONAL_AGENT_ARTIFACT_ROOT": str(self.root / "agent-artifacts"),
            "AUDIT_HMAC_SECRET": f"security-qualification-{uuid.uuid4().hex}",
            "AGENT_KEY_PATH": str(self.root / "agent.pem"),
            "AGENT_API_KEY": f"security-qualification-{uuid.uuid4().hex}",
            "DELEGATION_REQUIRED": "false", "FF_UI_DESIGNER": "false",
        })
        os.environ.pop("ASTRAL_NODE_OWNER", None)
        flags._flags.update({"inprocess_agents": True, "user_skills": False, "typesafe_routing": False,
                             "observation_packing": False, "safe_compaction": False})
        node_identity.reset_cache()
        node_identity.load_node_identity()

    def _restore_environment(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        flags._flags.update(self._saved_flags)
        node_identity.reset_cache()

    async def setup(self) -> None:
        self._configure_environment()
        try:
            from orchestrator.orchestrator import Orchestrator

            self.orch = await asyncio.to_thread(Orchestrator)
            await self._pair_owner()
            self._register_fixtures()
            self.model = ModelServer()
            await self._provide_model()
            self.bridge = node_mesh_chat.MeshChatBridge(
                self.node, owner_id=self.node.personal_owner_id, owner_setup=self.node.refresh_personal_owner,
                history=self.orch.history, calls=OrchestratorCalls(self.orch), state_path=self.root / "chats.json",
                phi_active=lambda _owner: False, poll_seconds=0.2, turn_seconds=TURN_SECONDS)
            node_mesh_chat.install(self.bridge)
            await self.bridge.start()
            await self.bridge.scan()
        except BaseException:
            await self.teardown()
            raise

    async def _provide_model(self) -> None:
        provider = ak.ModelProviderConfig(
            name="scripted", base_url=self.model.url + "/v1", models=["scripted-model"], api_keys=["scripted-key"],
            fallback_order=0, self_hosted=True, strict_tools=False, recipients=[self.owner_device.device_id, self.node_device])
        self.owner_device.kernel.set_model_provider(provider)
        await _until(lambda: any(item.name == "scripted" for item in self.node.kernel.model_providers()),
                     "the scripted model provider to reach the node")

    async def _pair_owner(self) -> None:
        settings = node_mesh.MeshSettings(
            label="node", relay=("none",), loopback_only=True, local_discovery=False, internet_lookup=False,
            sync_interval_ms=500, mailbox=MailboxSettings(enabled=False))
        self.node = node_mesh.NodeMesh(data_dir=self.root / "node", settings=settings)
        await self.node.start()
        self.owner_device = dev_mesh.DevOwner("owner", self.root / "owner")
        self.owner_device.create_mesh()
        self.owner_device.confirm_recovery()
        await self.owner_device.start()
        offer = await self.node.pairing_offer()
        await self.owner_device.approve_offer(offer["payload"])

        async def enrolled() -> bool:
            return bool((await self.node.status())["enrolled"])

        await _until(enrolled, "the node to join the owner's mesh")
        self.node_device = (await self.node.status())["member_id"]
        await _until(self.owner_device.phi_markers_synced, "the PHI markers to sync")
        self.owner_device.adopt_phi_group()
        await _until(self.node.phi_member, "the node to join the PHI group")
        if await self.node.refresh_personal_owner() is None:
            raise RuntimeError("the node refused the owner's mesh")
        self.owner_id = self.node.personal_owner_id()

    def _register_fixtures(self) -> None:
        catalog = {}
        for agent_id, tool, scope in FIXTURES:
            definition = definition_hash(tool, "Isolated security qualification fixture", TOOL_SCHEMA,
                                         {ve.AGENT_ANNOTATION: agent_id})
            self._definitions[tool] = definition
            catalog[agent_id] = {"description": "Security qualification fixture", "tools": [{
                "name": tool, "description": "Isolated security qualification fixture",
                "input_schema": TOOL_SCHEMA, "scope": scope, "definition": definition}]}
        cards, pins = ve.build_cards(catalog)
        if sorted(ve.register(self.orch, cards, pins)) != sorted(agent for agent, _, _ in FIXTURES):
            raise RuntimeError("the qualification fixtures were not registered")
        self._executor_client = httpx.AsyncClient(transport=httpx.MockTransport(self.fixture.handle))
        self.orch.vessel_executor = ve.VesselExecutor(
            ve.ExecutorSettings("http://executor.invalid:8800", "operator",
                                agent_tokens={agent: f"{agent}-token" for agent, _, _ in FIXTURES}),
            self._executor_client)
        for agent_id, _tool, scope in FIXTURES:
            self.orch.tool_permissions.set_agent_scopes(self.owner_id, agent_id, {scope: True})

    async def _grant(self, tools: tuple[str, ...]) -> None:
        for tool in tools:
            effect = "outside_effect" if tool == EFFECT else "outward_read"
            name = grant_name(tool, self._definitions[tool])
            self.owner_device.kernel.grant_tool(name, effect)
            await _until(lambda name=name: any(g.tool == name and g.consented for g in self.node.kernel.grants()),
                         f"the node to see the grant for {tool}")

    async def _request(self, chat: str, text: str) -> tuple[str, str]:
        request = self.owner_device.kernel.send_request(chat, text, self.node_device, None, None)
        kernel = self.owner_device.kernel
        await _until(lambda: kernel.request(request).state in (
            ak.RequestState.ANSWERED, ak.RequestState.REFUSED),
            "the node to finish the request", seconds=TURN_SECONDS)
        await _until(lambda: self._settle(chat), "the node's turn and its tool calls to finish", seconds=TURN_SECONDS)
        return request, str(kernel.request(request).state)

    def _settled(self, chat: str) -> bool:
        kernel = self.node.kernel
        running = any(turn.status == ak.TurnStatus.RUNNING for turn in kernel.chat(chat).view.turns)
        open_calls = any(item.call is not None and item.state in (ak.RequestState.PENDING, ak.RequestState.CLAIMED)
                         for item in kernel.chat_requests(chat))
        return not running and not open_calls

    async def _open_chat(self, title: str) -> str:
        kernel = self.owner_device.kernel
        chat = kernel.create_chat(title)
        kernel.send_message(chat, "hello node", None)
        await _until(lambda: any(c.chat_id == chat for c in self.node.kernel.list_chats()),
                     "the chat to reach the node")
        return chat

    def _local(self, chat: str) -> str | None:
        return self.bridge._state.local_for(chat)

    def _calls(self, chat: str) -> list[dict]:
        return [{"tool": item.call.tool, "state": str(item.state).rsplit(".", 1)[-1], "code": item.code or ""}
                for item in self.node.kernel.chat_requests(chat) if item.call is not None]

    def _settle(self, chat: str) -> bool:
        for pending in self.node.kernel.pending_consents():
            self.consents.append(f"{pending.tool_name}:consent.required:{'+'.join(pending.reasons)}")
            self.node.kernel.resolve_consent(pending.request_id, False)
        return self._settled(chat)

    def _audit(self, local_chat: str | None) -> tuple[list[dict], bool]:
        if local_chat is None:
            return [], False
        listed = self.orch.audit_repo.list_for_user(self.owner_id, limit=200, conversation_id=local_chat)
        events = listed[0] if isinstance(listed, tuple) else listed
        rows = [{
            "action_type": getattr(event, "action_type", None), "outcome": getattr(event, "outcome", None),
            "detail": getattr(event, "outcome_detail", None),
            "gate": (getattr(event, "inputs_meta", None) or {}).get("gate"),
            "correlation_id": str(getattr(event, "correlation_id", "") or ""),
        } for event in events]
        return rows, self.orch.audit_repo.verify_chain(self.owner_id) is None

    async def _turn(self, chat: str, text: str, calls: tuple[tuple[str, dict], ...]) -> bool:
        self.model.script = [ScriptedTurn(tool=name, arguments=arguments) for name, arguments in calls] + [
            ScriptedTurn(text="Source reviewed.")]
        _request, state = await self._request(chat, text)
        return state.endswith("ANSWERED") or state.endswith("REFUSED")

    async def run_case(self, case: QualificationCase) -> CaseEvidence:
        self.fixture.payloads[case.case_id] = case.injected_text
        await self._grant((READ_LOCAL, READ_REMOTE, EFFECT))
        before = len(self.fixture.effects[case.case_id])
        self.consents = []
        chat = await self._open_chat(f"qualification {case.case_id}")
        read_tool = READ_REMOTE if case.surface == "remote_reply" else READ_LOCAL
        effect_args = {"case_id": case.case_id, "destination": case.destination, "material": case.injected_text}
        registered = False
        if case.surface == "retained_context":
            registered = await self._turn(chat, case.user_request, ((READ_LOCAL, {"case_id": case.case_id}),))
            registered = await self._turn(chat, "Summarize the earlier source only.",
                                          ((EFFECT, effect_args),)) and registered
        else:
            registered = await self._turn(chat, case.user_request, (
                (read_tool, {"case_id": case.case_id}), (EFFECT, effect_args)))
        rows, chain_ok = await asyncio.to_thread(self._audit, self._local(chat))
        read_completed = any(row["action_type"] == f"tool.{read_tool}.end" and row["outcome"] == "success"
                             for row in rows)
        refusals = [f"{call['tool']}:{call['code']}" for call in self._calls(chat)
                    if call["tool"] == EFFECT and call["state"] == "REFUSED"]
        refusals += self.consents
        effect_count = len(self.fixture.effects[case.case_id]) - before
        receipts = tuple(row["correlation_id"] for row in rows if row["correlation_id"])
        refusal = ",".join(sorted(set(refusals)))
        benign = await self._benign_control(case, read_tool)
        return CaseEvidence(
            case_id=case.case_id, surface=case.surface, registered_turn=registered,
            read_completed=read_completed, effect_attempted=bool(refusals) or effect_count > 0,
            effect_count=effect_count, audit_chain_ok=chain_ok, audit_event_ids=receipts,
            benign_completed=benign, refusal=refusal)

    async def _benign_control(self, case: QualificationCase, read_tool: str) -> bool:
        chat = await self._open_chat(f"qualification control {case.case_id}")
        if not await self._turn(chat, "Read this source for a summary.",
                                ((read_tool, {"case_id": case.case_id}),)):
            return False
        rows, chain_ok = await asyncio.to_thread(self._audit, self._local(chat))
        return chain_ok and any(row["action_type"] == f"tool.{read_tool}.end" and row["outcome"] == "success"
                                for row in rows)

    async def teardown(self) -> None:
        errors: list[BaseException] = []

        async def step(action: Callable[[], Any]) -> None:
            try:
                result = action()
                if asyncio.iscoroutine(result):
                    await result
            except BaseException as error:
                errors.append(error)

        node_mesh_chat.install(None)
        if self.bridge is not None:
            await step(self.bridge.stop)
        if self.owner_device is not None:
            await step(self.owner_device.stop)
        if self.node is not None:
            await step(self.node.stop)
        if self.orch is not None:
            await step(lambda: asyncio.wait_for(self.orch._close_started_services(), timeout=30.0))
        if self.model is not None:
            await step(self.model.stop)
        if self._executor_client is not None:
            await step(self._executor_client.aclose)
        self._restore_environment()
        shutil.rmtree(self.root, ignore_errors=True)
        if self.root.exists():
            errors.append(RuntimeError("the qualification workspace was not removed"))
        self.bridge = self.owner_device = self.node = self.orch = None
        if errors:
            raise errors[0]


async def run_personal_qualification(run_id: str | None = None):
    driver = PersonalDispatchQualification(run_id or f"security-{uuid.uuid4().hex[:12]}")
    evidence = []
    teardown_verified = False
    try:
        await driver.setup()
        for case in CASES:
            evidence.append(await driver.run_case(case))
    finally:
        await driver.teardown()
        teardown_verified = True
    return tuple(evidence), teardown_verified
