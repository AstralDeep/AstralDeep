"""
Orchestrator A2A Executor — Exposes the orchestrator as an A2A-compliant agent.

External A2A clients can discover the orchestrator at /a2a/.well-known/agent-card.json
and send messages via JSON-RPC at /a2a. The orchestrator routes messages through its
LLM-powered tool selection and returns aggregated results.
"""
import os
import uuid
import logging
from typing import List

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCard as A2AAgentCard,
    AgentCapabilities,
    AgentInterface,
    AgentSkill as A2AAgentSkill,
    AgentProvider,
    Message as A2AMessage,
    Role,
    SecurityScheme,
    SecurityRequirement,
    StringList,
    OpenIdConnectSecurityScheme,
)

from shared.a2a_bridge import (
    extract_text_from_a2a_message,
    a2a_message_to_mcp_request,
    make_text_part,
    make_data_part,
)
from shared.a2a_security import A2ASecurityValidator

logger = logging.getLogger("OrchestratorA2AExecutor")


class OrchestratorA2AExecutor(AgentExecutor):
    """Wraps the orchestrator's routing logic as an A2A AgentExecutor.

    External A2A clients can send natural language messages which get routed
    through the orchestrator's LLM tool selection, or direct tool calls
    via a data Part.
    """

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.security_validator = A2ASecurityValidator()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        try:
            await updater.start_work()

            message = context.message
            if not message:
                await updater.failed(
                    message=self._msg("No message provided", context.task_id)
                )
                return

            mcp_request = a2a_message_to_mcp_request(message)
            if mcp_request:
                identity = await self._validated_invocation_identity(context)
                if identity is None:
                    await updater.failed(
                        message=self._msg(
                            "A valid bearer token is required for tool execution",
                            context.task_id,
                        )
                    )
                    return
                claims, subject_token = identity
                await self._execute_direct_tool(
                    mcp_request,
                    updater,
                    context,
                    claims=claims,
                    subject_token=subject_token,
                )
                return

            text = extract_text_from_a2a_message(message)
            if not text.strip():
                await self._list_all_tools(updater, context)
                return

            await self._execute_natural_language(text, updater, context)

        except Exception as e:
            logger.error(f"Orchestrator A2A execute error: {e}", exc_info=True)
            await updater.failed(
                message=self._msg(str(e), context.task_id)
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()

    async def _validated_invocation_identity(self, context):
        """Validate the actual A2A HTTP bearer from SDK server context."""

        call_context = getattr(context, "call_context", None)
        state = getattr(call_context, "state", {}) or {}
        headers = state.get("headers", {}) if isinstance(state, dict) else {}
        authorization = ""
        if isinstance(headers, dict):
            authorization = next(
                (
                    str(value)
                    for key, value in headers.items()
                    if str(key).lower() == "authorization"
                ),
                "",
            )
        scheme, separator, subject_token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not subject_token.strip():
            return None
        subject_token = subject_token.strip()
        claims = await self.security_validator.validate_token(subject_token)
        if not isinstance(claims, dict) or not isinstance(claims.get("sub"), str):
            return None
        return claims, subject_token

    async def _execute_direct_tool(
        self,
        mcp_request,
        updater,
        context,
        *,
        claims,
        subject_token,
    ):
        """Execute a direct tool call through Astral gates and final dispatch."""
        mcp_request.validate_protocol_metadata(allow_legacy=True)
        tool_name = mcp_request.params.get("name", "")
        arguments = mcp_request.params.get("arguments", {})

        tool_to_agent = {}
        for agent_id, caps in self.orchestrator.agent_capabilities.items():
            for cap in caps:
                tool_to_agent[cap["name"]] = agent_id

        agent_id = tool_to_agent.get(tool_name)
        if not agent_id:
            await updater.failed(
                message=self._msg(f"No agent found for tool '{tool_name}'", context.task_id)
            )
            return

        result = await self.orchestrator.execute_authorized_tool(
            claims=claims,
            user_id=claims["sub"],
            agent_id=agent_id,
            tool_name=tool_name,
            arguments=arguments,
            channel="a2a",
            delegation_subject_token=subject_token,
        )

        if result and result.error:
            error_msg = result.error.get("message", "Tool failed") if isinstance(result.error, dict) else str(result.error)
            parts = [make_text_part(f"Error: {error_msg}")]
            if result.ui_components:
                parts.append(make_data_part({"_ui_components": result.ui_components}))
            await updater.failed(message=A2AMessage(
                message_id=str(uuid.uuid4()), role=Role.ROLE_AGENT,
                parts=parts, task_id=context.task_id,
            ))
        else:
            parts = []
            if result and result.result is not None:
                if isinstance(result.result, dict):
                    parts.append(make_data_part(result.result))
                else:
                    parts.append(make_text_part(str(result.result)))
            if result and result.ui_components:
                parts.append(make_data_part(
                    {"_ui_components": result.ui_components},
                    metadata={"type": "ui_components"},
                ))
            if not parts:
                parts.append(make_text_part("OK"))

            msg = A2AMessage(
                message_id=str(uuid.uuid4()), role=Role.ROLE_AGENT,
                parts=parts, task_id=context.task_id,
            )
            await updater.complete(message=msg)

    async def _execute_natural_language(self, text, updater, context):
        """Route a natural language message through the LLM for tool selection."""
        await self._list_all_tools(updater, context, intro_text=f"Received: {text}\n\nAvailable tools:")

    async def _list_all_tools(self, updater, context, intro_text="Available tools:"):
        """Return all available tools across all agents."""
        all_tools = []
        for agent_id, caps in self.orchestrator.agent_capabilities.items():
            card = self.orchestrator.agent_cards.get(agent_id)
            agent_name = card.name if card else agent_id
            for cap in caps:
                all_tools.append({
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "name": cap["name"],
                    "description": cap.get("description", ""),
                })

        parts = [
            make_text_part(intro_text),
            make_data_part({"tools": all_tools}),
        ]
        msg = A2AMessage(
            message_id=str(uuid.uuid4()), role=Role.ROLE_AGENT,
            parts=parts, task_id=context.task_id,
        )
        await updater.complete(message=msg)

    @staticmethod
    def _msg(text: str, task_id: str) -> A2AMessage:
        return A2AMessage(
            message_id=str(uuid.uuid4()), role=Role.ROLE_AGENT,
            parts=[make_text_part(text)], task_id=task_id,
        )


def build_orchestrator_a2a_card(orchestrator) -> A2AAgentCard:
    """Build an official A2A AgentCard for the orchestrator.

    Aggregates all connected agent skills into the orchestrator's card.
    """
    port = int(os.getenv("ORCHESTRATOR_PORT", 9001))
    host = os.getenv("HOST", "0.0.0.0")
    authority = os.getenv("KEYCLOAK_AUTHORITY", "")

    skills: List[A2AAgentSkill] = []
    for agent_id, caps in orchestrator.agent_capabilities.items():
        card = orchestrator.agent_cards.get(agent_id)
        for cap in caps:
            skill_tags = ["agent:" + agent_id]
            if card:
                for s in card.skills:
                    if s.id == cap["name"] and s.scope:
                        skill_tags.append(f"scope:{s.scope}")
            skills.append(A2AAgentSkill(
                id=cap["name"],
                name=cap["name"],
                description=cap.get("description", ""),
                tags=skill_tags,
            ))

    security_schemes = {}
    security_requirements: List[SecurityRequirement] = []
    if authority:
        security_schemes["keycloak_oidc"] = SecurityScheme(
            open_id_connect_security_scheme=OpenIdConnectSecurityScheme(
                open_id_connect_url=f"{authority}/.well-known/openid-configuration",
            )
        )
        security_requirements.append(SecurityRequirement(schemes={
            "keycloak_oidc": StringList(list=[
                "tools:read", "tools:write", "tools:search", "tools:system",
            ]),
        }))

    return A2AAgentCard(
        name="AstralDeep Orchestrator",
        description="Multi-agent orchestrator with LLM-powered tool routing. Routes requests to specialized agents.",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        skills=skills if skills else [A2AAgentSkill(
            id="chat", name="chat",
            description="Send a natural language message for LLM-powered routing",
            tags=["routing"],
        )],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["application/json"],
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=f"http://{host}:{port}/a2a"),
        ],
        provider=AgentProvider(
            organization="AstralDeep",
            url=os.getenv("PUBLIC_BASE_URL", "http://localhost:5173"),
        ),
        security_schemes=security_schemes or None,
        security_requirements=security_requirements,
    )


def setup_orchestrator_a2a(app, orchestrator):
    """Mount the A2A JSON-RPC endpoint on the orchestrator's FastAPI app."""
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from a2a.server.routes import create_jsonrpc_routes, create_agent_card_routes

    initial_card = build_orchestrator_a2a_card(orchestrator)

    executor = OrchestratorA2AExecutor(orchestrator)
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=initial_card,
    )

    async def refresh_card(_card):
        """Rebuild the orchestrator's A2A card with the current agent skills."""
        return build_orchestrator_a2a_card(orchestrator)

    for route in create_jsonrpc_routes(handler, rpc_url="/a2a", enable_v0_3_compat=True):
        app.router.routes.append(route)
    for route in create_agent_card_routes(
        initial_card,
        card_modifier=refresh_card,
        card_url="/a2a/.well-known/agent-card.json",
    ):
        app.router.routes.append(route)
