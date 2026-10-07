"""Version-aware A2A JSON-RPC codec for AstralDeep's outbound agent calls.

Selects the request version from a peer's advertised agent card, encodes
SendMessage envelopes, and decodes responses into the a2a-sdk 1.0 response
union; 0.3 traffic is carried only through the SDK's named v0.3 compatibility
adapter. Orchestrator._execute_via_a2a is the sole caller.
"""

from typing import Any, Dict, Optional

from google.protobuf.json_format import MessageToDict, ParseDict
from packaging.version import InvalidVersion, Version

from a2a.compat.v0_3 import conversions
from a2a.compat.v0_3 import types as v0_3_types
from a2a.compat.v0_3.versions import is_legacy_version
from a2a.types import SendMessageRequest, SendMessageResponse
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


def _interface_version(interface) -> str:
    return str(getattr(interface, "protocol_version", "") or "").strip()


def _interface_binding(interface) -> str:
    return str(getattr(interface, "protocol_binding", "") or "").strip().upper()


def _parsed_version(raw: str) -> Optional[Version]:
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def _is_v1_0(raw: str) -> bool:
    parsed = _parsed_version(raw)
    return parsed is not None and parsed.release[:2] == (1, 0)


def select_request_version(card) -> str:
    interfaces = list(getattr(card, "supported_interfaces", None) or []) if card is not None else []
    if not interfaces:
        return PROTOCOL_VERSION_CURRENT
    candidates = [
        interface
        for interface in interfaces
        if _interface_binding(interface) in ("", JSONRPC_BINDING)
    ]
    if not candidates:
        bindings = sorted({_interface_binding(interface) for interface in interfaces})
        raise A2ANegotiationError(
            f"A2A peer advertises no JSONRPC interface (bindings: {bindings})"
        )
    for interface in candidates:
        if _is_v1_0(_interface_version(interface)):
            return PROTOCOL_VERSION_1_0
    for interface in candidates:
        if is_legacy_version(_interface_version(interface)):
            return PROTOCOL_VERSION_0_3
    for interface in candidates:
        if not _interface_version(interface):
            return PROTOCOL_VERSION_CURRENT
    advertised = sorted({_interface_version(interface) or "(none)" for interface in candidates})
    raise A2ANegotiationError(
        f"A2A peer advertises unsupported protocol version(s): {', '.join(advertised)}",
        advertised,
    )


def outbound_headers(version: str) -> Dict[str, str]:
    return {VERSION_HEADER: version}


def build_send_message(version: str, message, request_id: str) -> Dict[str, Any]:
    if version == PROTOCOL_VERSION_1_0:
        method = "SendMessage"
        params = MessageToDict(
            SendMessageRequest(message=message),
            preserving_proto_field_name=True,
        )
    elif version == PROTOCOL_VERSION_0_3:
        method = "message/send"
        compat_request = conversions.to_compat_send_message_request(
            SendMessageRequest(message=message),
            request_id=request_id,
        )
        params = compat_request.params.model_dump(
            by_alias=True, exclude_none=True, mode="json"
        )
    else:
        raise A2ANegotiationError(f"A2A request version {version} has no encoder")
    return {
        "jsonrpc": "2.0",
        "method": method,
        "id": request_id,
        "params": params,
    }


def decode_send_message_result(version: str, result: Any) -> SendMessageResponse:
    if version == PROTOCOL_VERSION_1_0:
        try:
            return ParseDict(result, SendMessageResponse())
        except Exception as exc:
            raise A2ANegotiationError(
                f"A2A peer returned a non-conformant v1 result: {exc}"
            ) from exc
    if version == PROTOCOL_VERSION_0_3:
        return _decode_v0_3_result(result)
    raise A2ANegotiationError(f"A2A response version {version} has no decoder")


def _decode_v0_3_result(result: Any) -> SendMessageResponse:
    if not isinstance(result, dict):
        return SendMessageResponse()
    kind = result.get("kind")
    if not kind:
        # Older 0.3 servers may omit the kind discriminator
        if "messageId" in result:
            kind = "message"
        elif "id" in result:
            kind = "task"
    try:
        if kind == "task":
            task = conversions.to_core_task(v0_3_types.Task.model_validate(result))
            return SendMessageResponse(task=task)
        if kind == "message":
            message = conversions.to_core_message(
                v0_3_types.Message.model_validate(result)
            )
            return SendMessageResponse(message=message)
    except Exception as exc:
        raise A2ANegotiationError(
            f"A2A peer returned a non-conformant 0.3 result: {exc}"
        ) from exc
    return SendMessageResponse()
