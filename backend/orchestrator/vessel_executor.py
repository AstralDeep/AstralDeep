"""Sends a sandboxed agent's tool call, with its signed action and ticket, to the AstralVessel executor and maps the answer to a tool response.
orchestrator.py dispatches here for agents whose card names a Vessel sandbox, node_mesh_calls.py supplies the ticket, tool_visibility.py lists the tools this module registers, and node_mesh_chat.py publishes them to the mesh catalog.
session_store.py asks configuration_problems() so a production node refuses to start on a broken executor setting."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from shared.protocol import AgentCard, AgentSkill, MCPResponse

logger = logging.getLogger("Orchestrator.VesselExecutor")

URL_ENV = "ASTRAL_VESSEL_EXECUTOR_URL"
TOKEN_ENV = "ASTRAL_VESSEL_EXECUTOR_TOKEN"
TIMEOUT_ENV = "ASTRAL_VESSEL_EXECUTOR_TIMEOUT_SECONDS"
AGENT_TOKENS_ENV = "ASTRAL_VESSEL_EXECUTOR_AGENT_TOKENS_FILE"
MAX_TOKENS_FILE_BYTES = 65536
DEFAULT_TIMEOUT = 120.0
WORKSPACE_IDLE_SECONDS = 900.0
SWEEP_SECONDS = 60.0
AGENT_ANNOTATION = "agent"
CONNECT_TIMEOUT = 5.0
FOREIGN_METADATA_KEYS = ("sandbox", "vessel_definition")
POPULATION = "sandboxed"
READ_SCOPES = frozenset({"tools:read", "tools:search"})
VALID_SCOPES = frozenset(
    {"tools:read", "tools:write", "tools:search", "tools:system", "tools:files", "tools:execute"}
)
PINNED_ACTION_FIELDS = ("agent", "mesh")
UNAVAILABLE = "executor.unavailable"
OUTCOME_UNKNOWN = "executor.outcome_unknown"
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)


@dataclass(frozen=True)
class ExecutorSettings:
    url: str
    token: str
    timeout: float = DEFAULT_TIMEOUT
    agent_tokens: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> Optional["ExecutorSettings"]:
        settings, problems = _read_settings(os.environ if env is None else env)
        for problem in problems:
            logger.error("vessel_executor_misconfigured: %s", problem)
        return settings


def configuration_problems(env: Optional[Dict[str, str]] = None) -> List[str]:
    return _read_settings(os.environ if env is None else env)[1]


def _read_tokens(path: str) -> Tuple[Dict[str, str], Optional[str]]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_TOKENS_FILE_BYTES + 1)
    except OSError:
        return {}, f"{AGENT_TOKENS_ENV} names a file that cannot be read."
    if len(raw) > MAX_TOKENS_FILE_BYTES:
        return {}, f"{AGENT_TOKENS_ENV} names a file that is too large."
    try:
        document = json.loads(raw)
    except ValueError:
        document = None
    valid = (
        isinstance(document, dict)
        and bool(document)
        and all(isinstance(k, str) and k and isinstance(v, str) and v.strip() for k, v in document.items())
    )
    if not valid:
        return {}, f"{AGENT_TOKENS_ENV} must name a JSON object of agent ids and tokens."
    return {k: v.strip() for k, v in document.items()}, None


def _read_settings(source: Mapping[str, str]) -> Tuple[Optional[ExecutorSettings], List[str]]:
    url = (source.get(URL_ENV) or "").strip().rstrip("/")
    token = (source.get(TOKEN_ENV) or "").strip()
    tokens_path = (source.get(AGENT_TOKENS_ENV) or "").strip()
    raw_timeout = (source.get(TIMEOUT_ENV) or "").strip()
    if not (url or token or tokens_path):
        return None, []
    problems: List[str] = []
    parts = urlsplit(url)
    if not url or parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        problems.append(f"{URL_ENV} must be an http or https address without credentials.")
    if not token:
        problems.append(f"{TOKEN_ENV} is unset.")
    tokens: Dict[str, str] = {}
    if not tokens_path:
        problems.append(f"{AGENT_TOKENS_ENV} is unset.")
    else:
        tokens, problem = _read_tokens(tokens_path)
        if problem:
            problems.append(problem)
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT
    except ValueError:
        timeout = 0.0
    if not 0 < timeout <= 3600:
        problems.append(f"{TIMEOUT_ENV} must be a number of seconds above 0 and at most 3600.")
    if problems:
        return None, problems
    return ExecutorSettings(url, token, timeout, tokens), []


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
    def __init__(
        self,
        settings: ExecutorSettings,
        client: Optional[httpx.AsyncClient] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout, connect=CONNECT_TIMEOUT), follow_redirects=False
        )
        self._owned = client is None
        self._clock = clock
        self._open: Dict[Tuple[str, str], float] = {}
        self._sweeper: Optional[asyncio.Task] = None

    def start_sweeping(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_forever(), name="vessel-workspace-sweep")

    async def close(self) -> None:
        sweeper, self._sweeper = self._sweeper, None
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
        await self.end_all()
        if self._owned:
            await self._client.aclose()

    @staticmethod
    def _bearer(token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def catalog(self) -> Dict[str, Any]:
        response = await self._client.get(f"{self._settings.url}/tools", headers=self._bearer(self._settings.token))
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
        agent_token = self._settings.agent_tokens.get(agent_id)
        if not agent_token:
            return _failure("executor.unauthorized", "The node holds no executor token for this agent.")
        try:
            action = json.loads(ticket.action_json)
        except ValueError:
            action = None
        refusal = self._mismatch(action, tool, owner)
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
            "owner": owner,
        }
        limit = min(timeout, self._settings.timeout) if timeout else self._settings.timeout
        try:
            response = await self._client.post(
                f"{self._settings.url}/run",
                json=body,
                headers=self._bearer(agent_token),
                timeout=httpx.Timeout(limit, connect=CONNECT_TIMEOUT),
            )
        except _NOT_SENT:
            return _failure(UNAVAILABLE, "The sandbox executor could not be reached. The tool did not run.")
        except httpx.HTTPError as exc:
            logger.warning("vessel_executor_outcome_unknown: %s", type(exc).__name__)
            return _failure(OUTCOME_UNKNOWN, "The sandbox executor did not answer. The tool may or may not have run.")
        self._note_workspace(agent_id, response)
        return self._response(response)

    def _note_workspace(self, agent_id: str, response: httpx.Response) -> None:
        try:
            workspace = response.json().get("workspace")
        except (ValueError, AttributeError):
            return
        if response.status_code == 200 and isinstance(workspace, str) and workspace:
            self._open[(agent_id, workspace)] = self._clock()

    async def end(self, agent_id: str, workspace: str) -> bool:
        token = self._settings.agent_tokens.get(agent_id)
        if not token:
            return False
        try:
            response = await self._client.post(
                f"{self._settings.url}/end",
                json={"agent": agent_id, "workspace": workspace},
                headers=self._bearer(token),
                timeout=httpx.Timeout(CONNECT_TIMEOUT * 2, connect=CONNECT_TIMEOUT),
            )
        except httpx.HTTPError as exc:
            logger.warning("vessel_executor_end_failed: %s", type(exc).__name__)
            return False
        if response.status_code != 200:
            logger.warning("vessel_executor_end_refused: %s", response.status_code)
            return False
        self._open.pop((agent_id, workspace), None)
        return True

    async def end_all(self) -> None:
        for agent_id, workspace in list(self._open):
            await self.end(agent_id, workspace)

    async def sweep(self) -> int:
        cutoff = self._clock() - WORKSPACE_IDLE_SECONDS
        idle = [key for key, used in self._open.items() if used <= cutoff]
        ended = 0
        for agent_id, workspace in idle:
            ended += await self.end(agent_id, workspace)
        return ended

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                await self.sweep()
            except Exception:
                logger.exception("vessel_workspace_sweep_failed")

    @staticmethod
    def _mismatch(action: Any, tool: str, owner: Optional[str]) -> Optional[MCPResponse]:
        if not isinstance(action, dict):
            return _failure("request.malformed", "The signed action is not an object.")
        if any(not isinstance(action.get(key), str) or not action[key] for key in PINNED_ACTION_FIELDS):
            return _failure("request.call_mismatch", "The signed action must name the agent and the mesh.")
        if action.get("tool") != tool or not isinstance(owner, str) or not owner:
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


def published_tools(orch: Any) -> List[Dict[str, Any]]:
    from orchestrator.node_mesh_calls import definition_hash

    found: List[Dict[str, Any]] = []
    for (agent_id, name), pinned in sorted(definitions(orch).items()):
        card = getattr(orch, "agent_cards", {}).get(agent_id)
        skill = next((s for s in getattr(card, "skills", None) or [] if s.id == name), None)
        if skill is None:
            continue
        annotations = {AGENT_ANNOTATION: agent_id}
        schema = skill.input_schema if isinstance(skill.input_schema, dict) else {}
        if definition_hash(name, skill.description or "", schema, annotations) != pinned:
            logger.error("vessel_tool_definition_unpublishable: %s.%s", agent_id, name)
            continue
        found.append(
            {
                "name": name,
                "description": skill.description or "",
                "input_schema": schema,
                "annotations": annotations,
                "effect": "outward_read" if skill.scope in READ_SCOPES else "outside_effect",
            }
        )
    return found


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
    executor.start_sweeping()
    return executor


async def stop_for(orch: Any) -> None:
    executor = getattr(orch, "vessel_executor", None)
    orch.vessel_executor = None
    if executor is not None:
        await executor.close()
