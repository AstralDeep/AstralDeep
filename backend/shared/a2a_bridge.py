"""
A2A Bridge — Type conversion between custom protocol types and official a2a-sdk types.

Maps:
- Custom AgentCard/AgentSkill <-> a2a.types.AgentCard/AgentSkill (a2a-sdk v1.0+ proto-generated)
- MCPRequest/MCPResponse <-> a2a.types.Message with text/data Parts
"""
import os
import re
import uuid
import logging
from typing import Optional, Dict, Any, List

from google.protobuf.json_format import ParseDict, MessageToDict
from google.protobuf.struct_pb2 import Value

from a2a.types import (
    AgentCard as A2AAgentCard,
    AgentSkill as A2AAgentSkill,
    AgentCapabilities,
    AgentInterface,
    AgentProvider,
    Message as A2AMessage,
    Part,
    Role,
    SecurityScheme,
    SecurityRequirement,
    StringList,
    OpenIdConnectSecurityScheme,
)

from shared.protocol import (
    AgentCard as CustomAgentCard,
    AgentSkill as CustomAgentSkill,
    MCP_PROTOCOL_VERSION,
    MCPRequest,
    MCPResponse,
)

logger = logging.getLogger("A2ABridge")


# ----- Part helpers (v1.0 proto Part uses a `content` oneof) -----

def make_text_part(text: str) -> Part:
    return Part(text=text)


def make_data_part(data: dict, metadata: Optional[Dict[str, str]] = None) -> Part:
    proto_value = ParseDict(data, Value())
    if metadata:
        return Part(data=proto_value, metadata=metadata)
    return Part(data=proto_value)


def part_text(part: Part) -> Optional[str]:
    return part.text if part.WhichOneof("content") == "text" else None


def part_data(part: Part) -> Optional[dict]:
    if part.WhichOneof("content") != "data":
        return None
    val = MessageToDict(part.data)
    return val if isinstance(val, dict) else None


# ----- Skill / Card conversions -----

def custom_skill_to_a2a(skill: CustomAgentSkill) -> A2AAgentSkill:
    """Convert a custom AgentSkill to an official A2A AgentSkill."""
    tags = list(skill.tags) if skill.tags else []
    if skill.scope:
        tags.append(f"scope:{skill.scope}")

    return A2AAgentSkill(
        id=skill.id or skill.name,
        name=skill.name,
        description=skill.description,
        tags=tags,
        input_modes=["application/json"],
        output_modes=["application/json"],
    )


def custom_card_to_a2a(card: CustomAgentCard, base_url: str) -> A2AAgentCard:
    """Convert a custom AgentCard to an official A2A AgentCard.

    Args:
        card: Our custom AgentCard dataclass.
        base_url: The agent's HTTP base URL (e.g. "http://localhost:9003").
    """
    skills = [custom_skill_to_a2a(s) for s in card.skills]

    authority = os.getenv("KEYCLOAK_AUTHORITY", "")
    security_schemes: Dict[str, SecurityScheme] = {}
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
        name=card.name,
        description=card.description,
        version=card.version or "1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        skills=skills,
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=base_url),
        ],
        provider=AgentProvider(
            organization="AstralDeep",
            url=os.getenv("PUBLIC_BASE_URL", "http://localhost:5173"),
        ),
        security_schemes=security_schemes or None,
        security_requirements=security_requirements,
    )


def a2a_skill_to_custom(skill: A2AAgentSkill) -> CustomAgentSkill:
    """Convert an official A2A AgentSkill to our custom AgentSkill."""
    scope = "tools:read"
    tags: List[str] = []
    for tag in (skill.tags or []):
        if tag.startswith("scope:"):
            scope = tag[len("scope:"):]
        else:
            tags.append(tag)

    return CustomAgentSkill(
        name=skill.name,
        description=skill.description,
        id=skill.id,
        tags=tags,
        scope=scope,
    )


def _slugify_agent_id(name: str) -> str:
    """Sanitize a display name into a safe agent_id slug (``[a-z0-9-]`` only).

    Plain ``name.lower().replace(" ", "-")`` leaked punctuation into ids — e.g.
    "Windows Tools (code & system)" → ``windows-tools-(code-&-system)`` — which
    then diverged from the agent's real id and created phantom permission rows
    (C-2). Collapsing every non-alphanumeric run to a single hyphen keeps ids
    clean and stable.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "agent").strip().lower()).strip("-")
    return slug or "agent"


def a2a_card_to_custom(a2a_card: A2AAgentCard, agent_id: str = "") -> CustomAgentCard:
    """Convert an official A2A AgentCard to our custom AgentCard.

    ``agent_id`` — when the caller already knows the agent's real id (e.g. from
    the agent's own card JSON, which the A2A protobuf representation drops), pass
    it so the conversion does not invent a phantom id. Otherwise the id comes
    from the interface URL's last path segment (ignoring a bare port number such
    as ``8771``) or a sanitized slug of the display name — never the raw name.
    """
    skills = [a2a_skill_to_custom(s) for s in a2a_card.skills]

    iface_url = ""
    if a2a_card.supported_interfaces:
        iface_url = a2a_card.supported_interfaces[0].url or ""
    if not agent_id:
        candidate = iface_url.rstrip("/").split("/")[-1] if iface_url else ""
        # Accept the URL's last segment only when it's a clean slug id. A bare
        # base URL (``http://host:8771``) yields ``host:8771`` (or a port) as the
        # last segment — reject anything that isn't ``[a-z0-9][a-z0-9-]*`` and
        # slug the display name instead.
        if re.fullmatch(r"[a-z0-9][a-z0-9-]*", candidate or ""):
            agent_id = candidate
        else:
            agent_id = _slugify_agent_id(a2a_card.name)

    metadata: Dict[str, Any] = {}
    if a2a_card.HasField("provider"):
        metadata["provider"] = {
            "organization": a2a_card.provider.organization,
            "url": a2a_card.provider.url,
        }
    metadata["a2a_url"] = iface_url
    metadata["external"] = True

    return CustomAgentCard(
        name=a2a_card.name,
        description=a2a_card.description,
        agent_id=agent_id,
        version=a2a_card.version,
        skills=skills,
        metadata=metadata,
    )


# ----- Message <-> MCP conversions -----

def mcp_response_to_a2a_message(resp: MCPResponse, task_id: str) -> A2AMessage:
    """Convert an MCPResponse to an A2A Message with appropriate parts.

    - result -> data Part with the result data
    - ui_components -> data Part with {"_ui_components": [...]}
    - error -> text Part with error description
    """
    parts: List[Part] = []

    if resp.error:
        error_msg = (
            resp.error.get("message", "Unknown error")
            if isinstance(resp.error, dict)
            else str(resp.error)
        )
        parts.append(make_text_part(f"Error: {error_msg}"))
        if resp.ui_components:
            parts.append(make_data_part({"_ui_components": resp.ui_components}))
    else:
        if resp.result is not None:
            if isinstance(resp.result, dict):
                parts.append(make_data_part(resp.result))
            else:
                parts.append(make_text_part(str(resp.result)))

        if resp.ui_components:
            parts.append(make_data_part(
                {"_ui_components": resp.ui_components},
                metadata={"type": "ui_components"},
            ))

    if not parts:
        parts.append(make_text_part("OK"))

    return A2AMessage(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_AGENT,
        parts=parts,
        task_id=task_id,
    )


def a2a_message_to_mcp_request(msg: A2AMessage, request_id: Optional[str] = None) -> Optional[MCPRequest]:
    """Extract an MCPRequest from an incoming A2A Message.

    Looks for a data Part containing:
    {"method": "tools/call", "name": "tool_name", "arguments": {...}}

    If no such Part is found, returns None (the message may be natural language).
    """
    for part in msg.parts:
        data = part_data(part)
        if not isinstance(data, dict):
            continue
        if data.get("method") == "tools/call" and "name" in data:
            return MCPRequest(
                request_id=request_id or f"a2a_{uuid.uuid4().hex[:12]}",
                method="tools/call",
                params={
                    "name": data["name"],
                    "arguments": data.get("arguments", {}),
                },
                protocol_version=MCP_PROTOCOL_VERSION,
                caller_capabilities={},
                caller_info={"name": "AstralDeep A2A Bridge", "version": "1.0.0"},
            )
        if data.get("method") == "tools/list":
            return MCPRequest(
                request_id=request_id or f"a2a_{uuid.uuid4().hex[:12]}",
                method="tools/list",
                params={},
                protocol_version=MCP_PROTOCOL_VERSION,
                caller_capabilities={},
                caller_info={"name": "AstralDeep A2A Bridge", "version": "1.0.0"},
            )
    return None


def extract_text_from_a2a_message(msg: A2AMessage) -> str:
    """Extract plain text content from an A2A Message (for natural language routing)."""
    texts: List[str] = []
    for part in msg.parts:
        t = part_text(part)
        if t is not None:
            texts.append(t)
    return "\n".join(texts)


def a2a_response_to_mcp_response(
    task_or_message,
    request_id: str,
) -> MCPResponse:
    """Convert an A2A task/message response back to an MCPResponse.

    Handles both Task objects (with artifacts) and direct Message objects.
    """
    from a2a.types import Task, TaskState, Message as A2AMsg

    if isinstance(task_or_message, A2AMsg):
        return _message_to_mcp_response(task_or_message, request_id)

    if isinstance(task_or_message, Task):
        task = task_or_message
        if task.HasField("status") and task.status.state == TaskState.TASK_STATE_FAILED:
            error_msg = "Task failed"
            if task.status.HasField("message"):
                for p in task.status.message.parts:
                    t = part_text(p)
                    if t is not None:
                        error_msg = t
                        break
            return MCPResponse(
                request_id=request_id,
                error={"code": -32603, "message": error_msg, "retryable": False},
            )

        result = None
        ui_components = None
        for artifact in task.artifacts:
            for p in artifact.parts:
                d = part_data(p)
                if d is not None:
                    if "_ui_components" in d:
                        ui_components = d["_ui_components"]
                    else:
                        result = d
                    continue
                t = part_text(p)
                if t is not None and result is None:
                    result = t

        if task.HasField("status") and task.status.HasField("message"):
            msg_resp = _message_to_mcp_response(task.status.message, request_id)
            if result is None:
                result = msg_resp.result
            if ui_components is None:
                ui_components = msg_resp.ui_components

        return MCPResponse(
            request_id=request_id,
            result=result,
            ui_components=ui_components,
        )

    return MCPResponse(
        request_id=request_id,
        error={"code": -32603, "message": f"Unexpected response type: {type(task_or_message)}", "retryable": False},
    )


def _message_to_mcp_response(msg: A2AMessage, request_id: str) -> MCPResponse:
    """Convert a single A2A Message to MCPResponse."""
    result = None
    ui_components = None

    for p in msg.parts:
        d = part_data(p)
        if d is not None:
            if "_ui_components" in d:
                ui_components = d["_ui_components"]
            elif result is None:
                result = d
            continue
        t = part_text(p)
        if t is not None and result is None:
            result = t

    return MCPResponse(
        request_id=request_id,
        result=result,
        ui_components=ui_components,
    )
