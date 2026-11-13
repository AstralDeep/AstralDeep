"""Sends a sandboxed agent's tool call, with its signed action and ticket, to the AstralVessel executor and maps the answer to a tool response.
orchestrator.py dispatches here for agents whose card names a Vessel sandbox, node_mesh_calls.py supplies the ticket, and tool_visibility.py lists the tools this module registers."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from shared.protocol import AgentCard, AgentSkill, MCPResponse

logger = logging.getLogger("Orchestrator.VesselExecutor")

URL_ENV = "ASTRAL_VESSEL_EXECUTOR_URL"
TOKEN_ENV = "ASTRAL_VESSEL_EXECUTOR_TOKEN"
TIMEOUT_ENV = "ASTRAL_VESSEL_EXECUTOR_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT = 120.0
CONNECT_TIMEOUT = 5.0
FOREIGN_METADATA_KEYS = ("sandbox", "vessel_definition")
POPULATION = "sandboxed"
VALID_SCOPES = frozenset(
    {"tools:read", "tools:write", "tools:search", "tools:system", "tools:files", "tools:execute"}
)
PINNED_ACTION_FIELDS = ("agent", "mesh", "owner", "workspace")
UNAVAILABLE = "executor.unavailable"
OUTCOME_UNKNOWN = "executor.outcome_unknown"
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)


@dataclass(frozen=True)
class ExecutorSettings:
    url: str
    token: str
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> Optional["ExecutorSettings"]:
        source = os.environ if env is None else env
        url = (source.get(URL_ENV) or "").strip().rstrip("/")
        token = (source.get(TOKEN_ENV) or "").strip()
        if not url and not token:
            return None
        parts = urlsplit(url)
        if not token or parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            logger.error("vessel_executor_misconfigured")
            return None
        raw = (source.get(TIMEOUT_ENV) or "").strip()
        try:
            timeout = float(raw) if raw else DEFAULT_TIMEOUT
        except ValueError:
            timeout = 0.0
        if not 0 < timeout <= 3600:
            logger.error("vessel_executor_timeout_invalid")
            return None
        return cls(url, token, timeout)


@dataclass(frozen=True)
class CallTicket:
    ticket_json: str
    action_json: str
    arguments_json: str


_ticket: contextvars.ContextVar[Optional[CallTicket]] = contextvars.ContextVar("vessel_call_ticket", default=None)


@contextlib.contextmanager
def carrying(ticket: Optional[CallTicket]) -> Iterator[None]:
    token = _ticket.set(ticket)
    try:
        yield
    finally:
        _ticket.reset(token)


def current_ticket() -> Optional[CallTicket]:
    return _ticket.get()


def definitions(orch: Any) -> Dict[Tuple[str, str], str]:
    found = getattr(orch, "vessel_definitions", None)
    return found if isinstance(found, dict) else {}


def is_sandboxed(orch: Any, agent_id: str) -> bool:
    return any(agent == agent_id for agent, _ in definitions(orch))


def pinned_definition(orch: Any, agent_id: str, tool: str) -> Optional[str]:
    return definitions(orch).get((agent_id, tool))


def strip_foreign_metadata(card: Any) -> None:
    metadata = getattr(card, "metadata", None)
    if isinstance(metadata, dict):
        for key in FOREIGN_METADATA_KEYS:
            metadata.pop(key, None)
    for skill in getattr(card, "skills", None) or []:
        skill_metadata = getattr(skill, "metadata", None)
        if isinstance(skill_metadata, dict):
            for key in FOREIGN_METADATA_KEYS:
                skill_metadata.pop(key, None)


def _failure(code: str, message: str, *, retryable: bool = False) -> MCPResponse:
    return MCPResponse(error={"code": code, "message": message, "retryable": retryable})


def _dotted_code(value: Any, default: str) -> str:
    if isinstance(value, str) and 0 < len(value) <= 80 and "." in value:
        if all((c.islower() and c.isascii()) or c.isdigit() or c in "._" for c in value):
            return value
    return default


class VesselExecutor:
    def __init__(self, settings: ExecutorSettings, client: Optional[httpx.AsyncClient] = None):
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout, connect=CONNECT_TIMEOUT), follow_redirects=False
        )
        self._owned = client is None

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.token}"}

    async def catalog(self) -> Dict[str, Any]:
        response = await self._client.get(f"{self._settings.url}/tools", headers=self._headers())
        response.raise_for_status()
        document = response.json()
        agents = document.get("agents") if isinstance(document, dict) else None
        if not isinstance(agents, dict):
            raise ValueError("executor catalog is malformed")
        return agents

    async def invoke(
        self,
        agent_id: str,
        tool: str,
        arguments: Dict[str, Any],
        *,
        owner: Optional[str],
        timeout: Optional[float] = None,
    ) -> MCPResponse:
        ticket = current_ticket()
        if ticket is None:
            return _failure("executor.ticket_required", "A sandboxed tool needs a ticket for this exact call.")
        try:
            action = json.loads(ticket.action_json)
        except ValueError:
            action = None
        refusal = self._mismatch(action, agent_id, tool, owner)
        if refusal is not None:
            return refusal
        if not _covers(ticket.arguments_json, arguments, action):
            return _failure("request.call_mismatch", "The arguments are not the ones the action covers.")
        body = {
            "agent": agent_id,
            "tool": tool,
            "arguments_json": ticket.arguments_json,
            "action_json": ticket.action_json,
            "ticket_json": ticket.ticket_json,
            "workspace": action["workspace"],
            "owner": owner,
        }
        limit = min(timeout, self._settings.timeout) if timeout else self._settings.timeout
        try:
            response = await self._client.post(
                f"{self._settings.url}/run",
                json=body,
                headers=self._headers(),
                timeout=httpx.Timeout(limit, connect=CONNECT_TIMEOUT),
            )
        except _NOT_SENT:
            return _failure(UNAVAILABLE, "The sandbox executor could not be reached. The tool did not run.")
        except httpx.HTTPError as exc:
            logger.warning("vessel_executor_outcome_unknown: %s", type(exc).__name__)
            return _failure(OUTCOME_UNKNOWN, "The sandbox executor did not answer. The tool may or may not have run.")
        return self._response(response)

    @staticmethod
    def _mismatch(action: Any, agent_id: str, tool: str, owner: Optional[str]) -> Optional[MCPResponse]:
        if not isinstance(action, dict):
            return _failure("request.malformed", "The signed action is not an object.")
        if any(key not in action for key in PINNED_ACTION_FIELDS):
            return _failure("request.call_mismatch", "The signed action must name the agent, mesh, owner and workspace.")
        workspace = action["workspace"]
        valid = (
            isinstance(action["agent"], str)
            and bool(action["agent"])
            and action.get("tool") == tool
            and isinstance(owner, str)
            and bool(owner)
            and action["owner"] == owner
            and isinstance(action["mesh"], str)
            and bool(action["mesh"])
            and (workspace is None or isinstance(workspace, str))
        )
        if not valid:
            return _failure("request.call_mismatch", "The signed action is for a different call.")
        return None

    @staticmethod
    def _response(response: httpx.Response) -> MCPResponse:
        try:
            document = response.json()
        except ValueError:
            document = None
        if not isinstance(document, dict):
            if response.status_code >= 500:
                return _failure(OUTCOME_UNKNOWN, "The sandbox executor failed. The tool may or may not have run.")
            return _failure("executor.bad_response", "The sandbox executor sent an unreadable answer.")
        if response.status_code == 401:
            return _failure("executor.unauthorized", "The sandbox executor refused the node's token.")
        if response.status_code >= 500:
            return _failure(OUTCOME_UNKNOWN, "The sandbox executor failed. The tool may or may not have run.")
        if response.status_code >= 400:
            code = _dotted_code(document.get("code"), "executor.refused")
            return _failure(code, f"The executor refused the call: {code}")
        output = document.get("output")
        text = output if isinstance(output, str) else json.dumps(output, sort_keys=True, default=str)
        if document.get("error") is True:
            return _failure("executor.tool_error", text)
        files: List[Any] = document.get("files") if isinstance(document.get("files"), list) else []
        return MCPResponse(result={"output": text, "files": files} if files else text)


def _covers(arguments_json: str, arguments: Dict[str, Any], action: Dict[str, Any]) -> bool:
    try:
        sent = json.loads(arguments_json)
    except ValueError:
        return False
    digest = "sha256:" + hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
    return sent == arguments and action.get("args_digest") == digest


def build_cards(catalog: Dict[str, Any]) -> Tuple[List[AgentCard], Dict[Tuple[str, str], str]]:
    cards: List[AgentCard] = []
    pins: Dict[Tuple[str, str], str] = {}
    for agent_id, entry in sorted(catalog.items()):
        if not isinstance(entry, dict) or not isinstance(agent_id, str) or not agent_id:
            continue
        skills: List[AgentSkill] = []
        for tool in entry.get("tools") or []:
            if not isinstance(tool, dict) or tool.get("scope") not in VALID_SCOPES:
                continue
            name, definition = tool.get("name"), tool.get("definition")
            if not isinstance(name, str) or not name or not isinstance(definition, str) or not definition:
                continue
            schema = tool.get("input_schema")
            skills.append(
                AgentSkill(
                    name=name,
                    id=name,
                    description=str(tool.get("description") or ""),
                    input_schema=schema if isinstance(schema, dict) else {},
                    scope=tool["scope"],
                )
            )
            pins[(agent_id, name)] = definition
        if skills:
            cards.append(
                AgentCard(name=agent_id, description=str(entry.get("description") or ""), agent_id=agent_id, skills=skills)
            )
    return cards, pins


def register(orch: Any, cards: List[AgentCard], pins: Dict[Tuple[str, str], str]) -> List[str]:
    from orchestrator.governed_dispatch import DispatchRuntime

    registered: List[str] = []
    for card in cards:
        if card.agent_id in orch.agents or card.agent_id in orch.local_agents or is_sandboxed(orch, card.agent_id):
            logger.error("vessel_sandbox_agent_id_taken: %s", card.agent_id)
            continue
        orch.agent_cards[card.agent_id] = card
        orch.tool_permissions.register_tool_scopes(card.agent_id, {skill.id: skill.scope for skill in card.skills})
        orch.register_governed_dispatch_runtime(
            DispatchRuntime(None, card.agent_id, POPULATION, None, None, None, True, "protected_executor")
        )
        for skill in card.skills:
            orch.vessel_definitions[(card.agent_id, skill.id)] = pins[(card.agent_id, skill.id)]
        registered.append(card.agent_id)
    return registered


async def start_for(orch: Any) -> Optional[VesselExecutor]:
    settings = ExecutorSettings.from_env()
    if settings is None:
        return None
    executor = VesselExecutor(settings)
    try:
        cards, pins = build_cards(await executor.catalog())
    except (httpx.HTTPError, ValueError):
        logger.warning("vessel_executor_catalog_unavailable")
        await executor.close()
        return None
    orch.vessel_executor = executor
    register(orch, cards, pins)
    return executor


async def stop_for(orch: Any) -> None:
    executor = getattr(orch, "vessel_executor", None)
    orch.vessel_executor = None
    if executor is not None:
        await executor.close()
