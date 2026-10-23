"""Runs scripted attacks through signed human turns and registered fixture agents.
Physical effect state and the hash-chained audit are independent outcome oracles.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from types import SimpleNamespace

from jose import jwt
from shared.protocol import AgentCard, AgentSkill, MCPResponse
from verification.config import RunConfig
from verification.drivers.in_process import InProcessDriver, LoopbackAgent, _run_sync
from verification.isolation import make_principal

from security_benchmark.qualification import CASES, CaseEvidence, QualificationCase

SOURCE_AGENT = "security-source-fixture"
REMOTE_AGENT = "security-remote-fixture"
EFFECT_AGENT = "security-effect-fixture"
READ_LOCAL = "read_fixture_source"
READ_REMOTE = "read_remote_fixture_source"
EFFECT = "commit_fixture_effect"


class FixtureServer:
    def __init__(self):
        self.payloads: dict[str, str] = {}
        self.effects: dict[str, list[dict]] = defaultdict(list)

    def process_request(self, request):
        name = request.params.get("name")
        args = request.params.get("arguments") or {}
        if name in (READ_LOCAL, READ_REMOTE):
            case_id = args.get("case_id")
            if case_id not in self.payloads:
                return MCPResponse(request_id=request.request_id, error={
                    "message": "fixture source unavailable", "retryable": False})
            return MCPResponse(request_id=request.request_id,
                               result={"_data": {"text": self.payloads[case_id]}})
        if name == EFFECT:
            case_id = args.get("case_id")
            if case_id not in self.payloads:
                return MCPResponse(request_id=request.request_id, error={
                    "message": "fixture case unavailable", "retryable": False})
            self.effects[case_id].append({"destination": args.get("destination"),
                                          "material": args.get("material")})
            return MCPResponse(request_id=request.request_id, result={"_data": {"sent": True}})
        return MCPResponse(request_id=request.request_id, error={
            "message": "unknown fixture tool", "retryable": False})


def _scripted_turn(calls: tuple[tuple[str, dict], ...]):
    step = 0

    async def propose(_websocket, _messages, tools_desc=None, temperature=None,
                      feature="tool_dispatch"):
        nonlocal step
        usage = SimpleNamespace(total_tokens=0, prompt_tokens=0, completion_tokens=0)
        if feature != "tool_dispatch" or step >= len(calls):
            return SimpleNamespace(content="Source reviewed.", tool_calls=None,
                                   reasoning_content=None), usage
        name, arguments = calls[step]
        step += 1
        call = SimpleNamespace(id=f"qualification-{step}", type="function",
                               function=SimpleNamespace(name=name,
                                   arguments=json.dumps(arguments)))
        return SimpleNamespace(content=None, tool_calls=[call],
                               reasoning_content=None), usage

    return propose


class RealDispatchQualification(InProcessDriver):
    def __init__(self, run_id: str):
        super().__init__(RunConfig(mode="in_process", run_id=run_id))
        self.fixture = FixtureServer()

    async def setup(self) -> None:
        await super().setup()
        self.orch.delegation._exchange_via_keycloak = self._fixture_exchange

    async def _fixture_exchange(self, user_token, agent_id, allowed_tools,
                                enabled_scopes):
        identity = self._fixture_identity
        claims = jwt.decode(
            user_token, identity.jwks["keys"][0], algorithms=["RS256"],
            audience="account", issuer=identity.authority)
        if not claims.get("sub", "").startswith(identity.run_id + "_"):
            return {"error": "fixture_identity_invalid"}
        now = int(time.time())
        scope = " ".join([*enabled_scopes, *(f"tool:{tool}" for tool in allowed_tools)])
        delegated = jwt.encode({
            "sub": claims["sub"], "act": {"sub": f"agent:{agent_id}"},
            "iss": identity.authority, "aud": self.orch.delegation.agent_service_client_id,
            "iat": now, "exp": min(int(claims["exp"]), now + 300),
            "scope": scope, "delegation": True,
        }, identity.private_key, algorithm="RS256", headers={"kid": identity.kid})
        return {"access_token": delegated, "token_type": "Bearer",
                "scope": scope, "expires_in": 300, "agent_id": agent_id}

    async def _seed_llm_config(self, user_id: str) -> None:
        store = getattr(self.orch, "_llm_store", None)
        if store is None:
            raise RuntimeError("qualification requires the ordinary user LLM store")
        await _run_sync(
            store.set_sync, user_id, provider="custom",
            base_url="http://verif.invalid/v1", model="verif-scripted-model",
            api_key="verif-key")

    def _register_general_agent(self) -> None:
        self.agent_id = SOURCE_AGENT
        specs = (
            (SOURCE_AGENT, READ_LOCAL, "tools:read"),
            (REMOTE_AGENT, READ_REMOTE, "tools:read"),
            (EFFECT_AGENT, EFFECT, "tools:write"),
        )
        for agent_id, tool, scope in specs:
            self.orch.agent_cards[agent_id] = AgentCard(
                name=agent_id, description="Isolated security qualification fixture",
                agent_id=agent_id,
                skills=[AgentSkill(name=tool, description="Qualification fixture",
                                   id=tool, scope=scope,
                                   input_schema={"type": "object", "properties": {
                                       "case_id": {"type": "string"},
                                       "destination": {"type": "string"},
                                       "material": {"type": "string"}}})])
            self.orch.agents[agent_id] = LoopbackAgent(self.orch, self.fixture)
            self.orch.tool_permissions.register_tool_scopes(agent_id, {tool: scope})

    async def _grant(self, principal) -> None:
        for agent_id, scope in ((SOURCE_AGENT, "tools:read"),
                                (REMOTE_AGENT, "tools:read"),
                                (EFFECT_AGENT, "tools:write")):
            await _run_sync(self.orch.tool_permissions.set_agent_scopes,
                            principal.user_id, agent_id, {scope: True})

    async def run_case(self, case: QualificationCase) -> CaseEvidence:
        principal = self._execution_principal(
            make_principal(self.config.run_id, case.case_id.replace("-", "_")))
        await self._grant(principal)
        self.fixture.payloads[case.case_id] = case.injected_text
        before = len(self.fixture.effects[case.case_id])
        chat_id = await _run_sync(self.orch.history.create_chat, user_id=principal.user_id)
        ws = await self._register_session(principal, chat_id)
        read_tool = READ_REMOTE if case.surface == "remote_reply" else READ_LOCAL
        effect_args = {"case_id": case.case_id, "destination": case.destination,
                       "material": case.injected_text}
        registered = False
        try:
            if case.surface == "retained_context":
                self.orch._call_llm = _scripted_turn(((READ_LOCAL, {"case_id": case.case_id}),))
                await self._send_registered_turn(
                    ws, case.user_request, chat_id, user_id=principal.user_id, attachments=[])
                self.orch._call_llm = _scripted_turn(((EFFECT, effect_args),))
                await self._send_registered_turn(
                    ws, "Summarize the earlier source only.", chat_id,
                    user_id=principal.user_id, attachments=[])
            else:
                self.orch._call_llm = _scripted_turn((
                    (read_tool, {"case_id": case.case_id}), (EFFECT, effect_args)))
                await self._send_registered_turn(
                    ws, case.user_request, chat_id, user_id=principal.user_id, attachments=[])
            registered = True
            rows, chain_ok = await _run_sync(self._read_audit, principal.user_id)
            read_completed = any(row.get("action_type") == f"tool.{read_tool}.end"
                                 and row.get("outcome") == "success" for row in rows)
            attempted = any(row.get("action_type") == "hitl.proposed" for row in rows)
            receipts = tuple(row["correlation_id"] for row in rows
                             if row.get("correlation_id"))
            benign_completed = await self._benign_control(case, principal)
            return CaseEvidence(
                case_id=case.case_id, surface=case.surface,
                registered_turn=registered, read_completed=read_completed,
                effect_attempted=attempted,
                effect_count=len(self.fixture.effects[case.case_id]) - before,
                audit_chain_ok=chain_ok is True, audit_event_ids=receipts,
                benign_completed=benign_completed)
        finally:
            self._drop_session(ws)

    async def _benign_control(self, case: QualificationCase, principal) -> bool:
        prior_rows, prior_chain = await _run_sync(self._read_audit, principal.user_id)
        if prior_chain is not True:
            return False
        prior = {row.get("correlation_id") for row in prior_rows}
        chat_id = await _run_sync(self.orch.history.create_chat, user_id=principal.user_id)
        ws = await self._register_session(principal, chat_id)
        read_tool = READ_REMOTE if case.surface == "remote_reply" else READ_LOCAL
        self.orch._call_llm = _scripted_turn(((read_tool, {"case_id": case.case_id}),))
        try:
            await self._send_registered_turn(
                ws, "Read this source for a summary.", chat_id,
                user_id=principal.user_id, attachments=[])
            rows, chain_ok = await _run_sync(self._read_audit, principal.user_id)
            return chain_ok is True and any(
                row.get("action_type") == f"tool.{read_tool}.end"
                and row.get("outcome") == "success"
                and row.get("correlation_id") not in prior for row in rows)
        finally:
            self._drop_session(ws)


async def run_real_qualification(run_id: str | None = None):
    driver = RealDispatchQualification(run_id or f"__verif__security_{uuid.uuid4().hex[:12]}")
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
