"""Bind outbound A2A interfaces to their version, endpoint, and tenant.
The orchestrator uses this codec for v1 JSON-RPC and explicitly advertised
SDK v0.3 compatibility traffic before projecting replies through a2a_bridge.
"""

import re
from typing import Any, Dict, NamedTuple, Optional
from urllib.parse import urlsplit

from google.protobuf.json_format import MessageToDict, ParseDict

from a2a.compat.v0_3 import conversions
from a2a.compat.v0_3 import types as v0_3_types
from a2a.types import Message, Role, SendMessageRequest, SendMessageResponse, TaskState
from a2a.utils.constants import (
    PROTOCOL_VERSION_0_3,
    PROTOCOL_VERSION_1_0,
    PROTOCOL_VERSION_CURRENT,
    VERSION_HEADER,
    TransportProtocol,
)

JSONRPC_BINDING = TransportProtocol.JSONRPC.value


class A2ANegotiationError(Exception):
    def __init__(self, message: str, advertised_versions=()):
        super().__init__(message)
        self.advertised_versions = tuple(advertised_versions)


class A2ARequestInterface(NamedTuple):
    version: str
    url: str
    tenant: str = ""


def select_request_interface(card) -> Optional[A2ARequestInterface]:
    interfaces = list(getattr(card, "supported_interfaces", None) or [])
    if not interfaces:
        return None
    candidates = [
        interface
        for interface in interfaces
        if str(interface.protocol_binding).strip().upper() == JSONRPC_BINDING
    ]
    if not candidates:
        raise A2ANegotiationError("A2A peer advertises no JSONRPC interface")
    advertised = []
    for interface in candidates:
        raw = str(interface.protocol_version).strip()
        advertised.append(raw or "(none)")
        match = re.fullmatch(r"(1\.0|0\.3)(?:\.[0-9]+)?", raw)
        if raw and match is None:
            continue
        version = match.group(1) if match else PROTOCOL_VERSION_CURRENT
        tenant = str(interface.tenant)
        if tenant and version == PROTOCOL_VERSION_0_3:
            raise A2ANegotiationError("A2A 0.3 compatibility cannot preserve interface tenant routing")
        return A2ARequestInterface(version, str(interface.url), tenant)
    raise A2ANegotiationError(
        f"A2A peer advertises unsupported protocol version(s): {', '.join(advertised)}",
        advertised,
    )


def select_request_version(card) -> str:
    interface = select_request_interface(card)
    return interface.version if interface else PROTOCOL_VERSION_CURRENT


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("invalid interface URL")
        port = parsed.port
        return parsed.scheme, parsed.hostname, port if port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise A2ANegotiationError("A2A interface requires an absolute HTTP(S) URL without userinfo or fragments") from exc


def resolve_request_interface(card, registered_url: str) -> A2ARequestInterface:
    interface = select_request_interface(card)
    if interface is None:
        endpoint = registered_url if registered_url.rstrip("/").endswith("/a2a") else f"{registered_url.rstrip('/')}/a2a"
        interface = A2ARequestInterface(PROTOCOL_VERSION_CURRENT, endpoint)
    if _origin(interface.url) != _origin(registered_url):
        raise A2ANegotiationError("A2A interface must retain the registered endpoint origin")
    return interface


def outbound_headers(version: str) -> Dict[str, str]:
    return {VERSION_HEADER: version}


def build_send_message(version: str, message: Message, request_id: str, *, tenant: str = "") -> Dict[str, Any]:
    request = SendMessageRequest(message=message, tenant=tenant)
    if version == PROTOCOL_VERSION_1_0:
        method = "SendMessage"
        params = MessageToDict(request)
    elif version == PROTOCOL_VERSION_0_3:
        if tenant:
            raise A2ANegotiationError("A2A 0.3 compatibility cannot preserve interface tenant routing")
        method = "message/send"
        compat_request = conversions.to_compat_send_message_request(request, request_id=request_id)
        params = compat_request.params.model_dump(by_alias=True, exclude_none=True, mode="json")
    else:
        raise A2ANegotiationError(f"A2A request version {version} has no encoder")
    return {"jsonrpc": "2.0", "method": method, "id": request_id, "params": params}


def validate_jsonrpc_response(response: Any, request_id: str) -> None:
    if (
        not isinstance(response, dict)
        or response.get("jsonrpc") != "2.0"
        or response.get("id") != request_id
        or ("result" in response) == ("error" in response)
    ):
        raise A2ANegotiationError("A2A peer returned a non-conformant JSON-RPC response")
    if "error" in response:
        error = response["error"]
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise A2ANegotiationError("A2A peer returned a non-conformant JSON-RPC error")


def _validate_agent_message(message: Message) -> None:
    if not message.message_id or message.role != Role.ROLE_AGENT or not message.parts:
        raise A2ANegotiationError("A2A peer returned a non-conformant agent message")
    if any(part.WhichOneof("content") is None for part in message.parts):
        raise A2ANegotiationError("A2A peer returned a non-conformant empty part")


def decode_send_message_result(version: str, result: Any) -> SendMessageResponse:
    if version == PROTOCOL_VERSION_1_0:
        try:
            if not isinstance(result, dict):
                raise ValueError("result must be an object")
            response = ParseDict(result, SendMessageResponse())
        except Exception as exc:
            raise A2ANegotiationError(f"A2A peer returned a non-conformant v1 result: {exc}") from exc
    elif version == PROTOCOL_VERSION_0_3:
        response = _decode_v0_3_result(result)
    else:
        raise A2ANegotiationError(f"A2A response version {version} has no decoder")
    branch = response.WhichOneof("payload")
    if branch == "task":
        task = response.task
        if not task.id or not task.HasField("status") or task.status.state == TaskState.TASK_STATE_UNSPECIFIED:
            raise A2ANegotiationError("A2A peer returned a non-conformant task identity or status")
        artifact_ids = set()
        for artifact in task.artifacts:
            if not artifact.artifact_id or not artifact.parts or artifact.artifact_id in artifact_ids:
                raise A2ANegotiationError("A2A peer returned a non-conformant artifact identity or parts")
            artifact_ids.add(artifact.artifact_id)
        if task.status.HasField("message"):
            _validate_agent_message(task.status.message)
        parts = [part for artifact in task.artifacts for part in artifact.parts]
    elif branch == "message":
        _validate_agent_message(response.message)
        return response
    else:
        return response
    if any(part.WhichOneof("content") is None for part in parts):
        raise A2ANegotiationError("A2A peer returned a non-conformant empty part")
    return response


def _decode_v0_3_result(result: Any) -> SendMessageResponse:
    if not isinstance(result, dict):
        return SendMessageResponse()
    kind = result.get("kind")
    if not kind:
        kind = "message" if "messageId" in result else "task" if "id" in result else None
    try:
        if kind == "task":
            task = conversions.to_core_task(v0_3_types.Task.model_validate(result))
            return SendMessageResponse(task=task)
        if kind == "message":
            message = conversions.to_core_message(v0_3_types.Message.model_validate(result))
            return SendMessageResponse(message=message)
    except Exception as exc:
        raise A2ANegotiationError(f"A2A peer returned a non-conformant 0.3 result: {exc}") from exc
    return SendMessageResponse()
