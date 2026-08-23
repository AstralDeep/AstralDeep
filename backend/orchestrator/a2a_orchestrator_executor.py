"""
Orchestrator A2A Executor — Exposes the orchestrator as an A2A-compliant agent.

External A2A clients can discover the orchestrator at /a2a/.well-known/agent-card.json
and send messages via JSON-RPC at /a2a. The orchestrator routes messages through its
LLM-powered tool selection and returns aggregated results.

Posture (FF_A2A_SERVER, default off):

* ``GET /a2a/.well-known/agent-card.json`` is anonymous, as the A2A discovery
  contract requires, and therefore advertises ONLY the public, owner-safe
  built-in catalog as generic per-agent skills — never the per-tool inventory
  of user drafts, BYO agents or external peers.
* ``POST /a2a`` refuses every request without a valid first-party bearer BEFORE
  the SDK dispatches anything, so ``tasks/list``/``tasks/get``/``tasks/cancel``
  cannot run under the SDK's default unauthenticated user. The authenticated
  subject becomes the task-store owner, so callers only ever see their own tasks.
* Tool discovery and ``tools/call`` resolution are projected through the SAME
  per-user visibility predicate chat and the MCP endpoint use
  (``mcp_projection.project_tools`` → ``tool_visibility.eligible_tool_pairs``),
  and dispatch runs through ``execute_authorized_tool`` (full gate stack).
"""
import asyncio
import os
import uuid
import logging
from typing import Any, Dict, List, Optional, Tuple

from a2a.auth.user import UnauthenticatedUser, User
from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.routes.common import DefaultServerCallContextBuilder
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
    ensure_task_created,
    extract_text_from_a2a_message,
    a2a_message_to_mcp_request,
    make_text_part,
    make_data_part,
)
from shared.a2a_security import A2ASecurityValidator
from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS

logger = logging.getLogger("OrchestratorA2AExecutor")

#: ``request.scope`` key under which the HTTP auth gate hands the verified
#: principal to the SDK context builder.
PRINCIPAL_SCOPE_KEY = "astral_a2a_principal"
#: ``ServerCallContext.state`` keys the context builder populates.
STATE_CLAIMS = "astral_claims"
STATE_SUBJECT_TOKEN = "astral_subject_token"

TOOL_UNAVAILABLE = "Tool is unavailable or not authorized"
BEARER_REQUIRED = "A valid bearer token is required"


class A2APrincipal(User):
    """The authenticated caller of the orchestrator's inbound A2A server."""

    def __init__(self, claims: Dict[str, Any], subject_token: str):
        self.claims = claims
        self.subject_token = subject_token

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def sub(self) -> str:
        return str(self.claims["sub"])

    @property
    def user_name(self) -> str:
        # The task-store owner key. Namespaced so it can never collide with the
        # SDK's unauthenticated '' owner or a raw display name.
        return f"sub:{self.sub}"


def a2a_task_owner(context: ServerCallContext) -> str:
    """Task-store owner resolver: the verified subject, or an EMPTY scope.

    The SDK default keys every unauthenticated request under ``''`` — one
    shared bucket. An unauthenticated context here resolves to a fresh,
    unguessable per-call scope instead, so it can neither list, read nor
    cancel anything (the HTTP gate already refuses such requests; this is the
    structural backstop for the store itself).
    """
    user = getattr(context, "user", None)
    if isinstance(user, A2APrincipal):
        return user.user_name
    return f"anon:{uuid.uuid4().hex}"


class AuthenticatedA2AContextBuilder(DefaultServerCallContextBuilder):
    """Populate the SDK call context from the HTTP gate's verified principal.

    The SDK's ``build`` is synchronous, so the (async) bearer validation runs in
    the route wrapper installed by :func:`setup_orchestrator_a2a`; this builder
    only carries its result across. Without a principal the user is the SDK's
    ``UnauthenticatedUser`` — never a StarletteUser from some unrelated
    middleware — and no claims reach the executor.
    """

    def build(self, request) -> ServerCallContext:
        context = super().build(request)
        principal = request.scope.get(PRINCIPAL_SCOPE_KEY)
        if isinstance(principal, A2APrincipal):
            context.user = principal
            context.state[STATE_CLAIMS] = principal.claims
            context.state[STATE_SUBJECT_TOKEN] = principal.subject_token
        else:
            context.user = UnauthenticatedUser()
            context.state.pop(STATE_CLAIMS, None)
            context.state.pop(STATE_SUBJECT_TOKEN, None)
        return context


def bearer_from_headers(headers) -> str:
    """The bearer credential from a header mapping, or ''."""
    authorization = ""
    if headers is not None:
        try:
            items = headers.items()
        except Exception:
            items = []
        authorization = next(
            (str(value) for key, value in items if str(key).lower() == "authorization"),
            "",
        )
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


async def authenticate_a2a_request(
    validator: A2ASecurityValidator, headers
) -> Optional[A2APrincipal]:
    """Validate the request bearer with the entry-gate validator."""
    token = bearer_from_headers(headers)
    if not token:
        return None
    claims = await validator.validate_token(token)
    if not isinstance(claims, dict) or not isinstance(claims.get("sub"), str):
        return None
    return A2APrincipal(claims, token)


class OrchestratorA2AExecutor(AgentExecutor):
    """Wraps the orchestrator's routing logic as an A2A AgentExecutor.

    External A2A clients can send natural language messages which get routed
    through the orchestrator's LLM tool selection, or direct tool calls
    via a data Part.
    """

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.security_validator = A2ASecurityValidator(require_first_party_user=True)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        try:
            await ensure_task_created(context, event_queue)
            await updater.start_work()

            message = context.message
            if not message:
                await updater.failed(
                    message=self._msg("No message provided", context.task_id)
                )
                return

            # Every path — discovery included — needs a verified principal:
            # the tool inventory is per-user.
            identity = await self._validated_invocation_identity(context)
            if identity is None:
                await updater.failed(
                    message=self._msg(BEARER_REQUIRED, context.task_id)
                )
                return
            claims, subject_token = identity

            mcp_request = a2a_message_to_mcp_request(message)
            if mcp_request is not None and mcp_request.method == "tools/call":
                await self._execute_direct_tool(
                    mcp_request,
                    updater,
                    context,
                    claims=claims,
                    subject_token=subject_token,
                )
                return
            if mcp_request is not None:
                await self._list_user_tools(updater, context, claims)
                return

            text = extract_text_from_a2a_message(message)
            if not text.strip():
                await self._list_user_tools(updater, context, claims)
                return

            await self._execute_natural_language(text, updater, context, claims)

        except Exception as e:
            logger.error(f"Orchestrator A2A execute error: {e}", exc_info=True)
            await updater.failed(
                message=self._msg(str(e), context.task_id)
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()

    async def _validated_invocation_identity(
        self, context
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        """The verified caller: the gate's principal, else the actual HTTP bearer."""

        call_context = getattr(context, "call_context", None)
        state = getattr(call_context, "state", {}) or {}
        if not isinstance(state, dict):
            state = {}
        claims = state.get(STATE_CLAIMS)
        subject_token = state.get(STATE_SUBJECT_TOKEN)
        if (
            isinstance(claims, dict)
            and isinstance(claims.get("sub"), str)
            and isinstance(subject_token, str)
            and subject_token
        ):
            return claims, subject_token

        subject_token = bearer_from_headers(state.get("headers", {}))
        if not subject_token:
            return None
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

        from orchestrator.mcp_projection import resolve_projected_tool

        projected = await asyncio.to_thread(
            resolve_projected_tool,
            self.orchestrator,
            claims["sub"],
            tool_name,
            claims,
        )
        if projected is None:
            # One non-disclosing answer for unknown, hidden and unauthorized.
            await updater.failed(
                message=self._msg(TOOL_UNAVAILABLE, context.task_id)
            )
            return

        result = await self.orchestrator.execute_authorized_tool(
            claims=claims,
            user_id=claims["sub"],
            agent_id=projected.agent_id,
            tool_name=projected.skill_id,
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

    async def _execute_natural_language(self, text, updater, context, claims):
        """Route a natural language message through the LLM for tool selection."""
        await self._list_user_tools(
            updater, context, claims,
            intro_text=f"Received: {text}\n\nAvailable tools:",
        )

    async def _list_user_tools(self, updater, context, claims, intro_text="Available tools:"):
        """Return the CALLER's tools — the same set chat and /mcp would offer."""
        from orchestrator.mcp_projection import project_tools

        projected = await asyncio.to_thread(
            project_tools, self.orchestrator, claims["sub"], claims
        )
        tools = []
        for tool in projected:
            card = self.orchestrator.agent_cards.get(tool.agent_id)
            tools.append({
                "agent_id": tool.agent_id,
                "agent_name": card.name if card else tool.agent_id,
                "name": tool.name,
                "description": tool.descriptor.get("description", ""),
                "input_schema": tool.descriptor.get("inputSchema", {}),
            })

        parts = [
            make_text_part(intro_text),
            make_data_part({"tools": tools}),
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


# ------------------------------------------------------------------ card


def _is_safe_marked(orchestrator, agent_id: str) -> bool:
    """Feature 040 owner-safe marker, read through the permission layer; fail closed."""
    try:
        return bool(orchestrator.tool_permissions._is_safe_agent(agent_id))
    except Exception:  # noqa: BLE001 — a lookup failure never widens the card
        return False


def public_catalog_agent_ids(orchestrator) -> List[str]:
    """Agents the ANONYMOUS card may name: public first-party ∩ safe ∩ connected."""
    connected = set(getattr(orchestrator, "agents", {}) or {}) | set(
        getattr(orchestrator, "local_agents", {}) or {}
    )
    cards = getattr(orchestrator, "agent_cards", {}) or {}
    return [
        agent_id
        for agent_id in FIRST_PARTY_PUBLIC_AGENT_IDS
        if agent_id in cards
        and agent_id in connected
        and _is_safe_marked(orchestrator, agent_id)
    ]


def _public_url() -> str:
    base = (os.getenv("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return f"{base}/a2a"
    port = int(os.getenv("ORCHESTRATOR_PORT", 9001))
    host = os.getenv("HOST", "0.0.0.0")
    return f"http://{host}:{port}/a2a"


def build_orchestrator_a2a_card(orchestrator) -> A2AAgentCard:
    """Build the PUBLIC A2A AgentCard for the orchestrator.

    The card is served without authentication, so it names only the public,
    owner-safe built-in agents as generic skills (one per agent, no per-tool
    inventory). The caller's actual tool set is discovered after
    authentication via ``message/send`` with a ``{"method": "tools/list"}``
    data part and is projected per user.
    """
    authority = os.getenv("KEYCLOAK_AUTHORITY", "")

    skills: List[A2AAgentSkill] = []
    for agent_id in public_catalog_agent_ids(orchestrator):
        card = orchestrator.agent_cards.get(agent_id)
        skills.append(A2AAgentSkill(
            id=agent_id,
            name=str(getattr(card, "name", None) or agent_id),
            description=str(getattr(card, "description", None) or ""),
            tags=["agent:" + agent_id],
        ))
    skills.append(A2AAgentSkill(
        id="chat", name="chat",
        description="Send a natural language message for LLM-powered routing "
                    "(authenticated; tools are listed per caller)",
        tags=["routing"],
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
        skills=skills,
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["application/json"],
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=_public_url()),
        ],
        provider=AgentProvider(
            organization="AstralDeep",
            url=os.getenv("PUBLIC_BASE_URL", "http://localhost:5173"),
        ),
        security_schemes=security_schemes or None,
        security_requirements=security_requirements,
    )


# ----------------------------------------------------------------- mount


def _unauthenticated_response():
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32001, "message": "Authentication required"},
        },
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="astral-a2a"'},
    )


def setup_orchestrator_a2a(app, orchestrator):
    """Mount the A2A JSON-RPC endpoint on the orchestrator's FastAPI app."""
    from starlette.routing import Route
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from a2a.server.routes import create_jsonrpc_routes, create_agent_card_routes

    initial_card = build_orchestrator_a2a_card(orchestrator)

    executor = OrchestratorA2AExecutor(orchestrator)
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(owner_resolver=a2a_task_owner),
        agent_card=initial_card,
    )
    validator = executor.security_validator

    async def refresh_card(_card):
        """Rebuild the orchestrator's public A2A card from the current catalog."""
        return build_orchestrator_a2a_card(orchestrator)

    def _gated(endpoint):
        async def gated_endpoint(request):
            # Authenticate BEFORE the SDK parses a method: tasks/list,
            # tasks/get and tasks/cancel are served by the request handler
            # without ever reaching the executor.
            principal = await authenticate_a2a_request(validator, request.headers)
            if principal is None:
                return _unauthenticated_response()
            request.scope[PRINCIPAL_SCOPE_KEY] = principal
            return await endpoint(request)

        return gated_endpoint

    rpc_routes = create_jsonrpc_routes(
        handler,
        rpc_url="/a2a",
        context_builder=AuthenticatedA2AContextBuilder(),
        enable_v0_3_compat=True,
    )
    for route in rpc_routes:
        app.router.routes.append(Route(
            route.path,
            endpoint=_gated(route.endpoint),
            methods=sorted(route.methods or {"POST"}),
        ))
    for route in create_agent_card_routes(
        initial_card,
        card_modifier=refresh_card,
        card_url="/a2a/.well-known/agent-card.json",
    ):
        app.router.routes.append(route)
