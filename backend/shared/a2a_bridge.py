"""Bidirectional conversion between AstralDeep's custom protocol types
(shared/protocol.py) and the official a2a-sdk's AgentCard/AgentSkill/Message types;
used by shared/a2a_executor.py and orchestrator/a2a_orchestrator_executor.py.
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
    Task as A2ATask,
    TaskState as A2ATaskState,
    TaskStatus as A2ATaskStatus,
)

from shared.protocol import (
    AgentCard as CustomAgentCard,
    AgentSkill as CustomAgentSkill,
    MCP_PROTOCOL_VERSION,
    MCPRequest,
    MCPResponse,
)

logger = logging.getLogger("A2ABridge")


async def ensure_task_created(context, event_queue) -> None:
    if getattr(context, "current_task", None) is not None:
        return
    message = getattr(context, "message", None)
    await event_queue.enqueue_event(A2ATask(
        id=context.task_id,
        context_id=context.context_id,
        status=A2ATaskStatus(state=A2ATaskState.TASK_STATE_SUBMITTED),
        history=[message] if message is not None else [],
    ))


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


def custom_skill_to_a2a(skill: CustomAgentSkill) -> A2AAgentSkill:
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


# Must match the real agent id or permission rows go phantom
def _slugify_agent_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "agent").strip().lower()).strip("-")
    return slug or "agent"


def a2a_card_to_custom(a2a_card: A2AAgentCard, agent_id: str = "") -> CustomAgentCard:
    skills = [a2a_skill_to_custom(s) for s in a2a_card.skills]

    iface_url = ""
    if a2a_card.supported_interfaces:
        iface_url = a2a_card.supported_interfaces[0].url or ""
    if not agent_id:
        candidate = iface_url.rstrip("/").split("/")[-1] if iface_url else ""
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


def mcp_response_to_a2a_message(resp: MCPResponse, task_id: str) -> A2AMessage:
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
                protocol_version=data.get("protocol_version", MCP_PROTOCOL_VERSION),
                caller_capabilities=data.get("caller_capabilities", {}),
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
