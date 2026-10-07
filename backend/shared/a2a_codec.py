"""Version-aware A2A wire codec: selects an outbound protocol version from a peer
card's advertised supported_interfaces, builds the matching JSON-RPC envelope and
headers, and decodes the response union into a Task or Message object. Used by
orchestrator._execute_via_a2a and by the card builders in shared/a2a_bridge.py."""

from __future__ import annotations

from typing import Any, Dict, List

from google.protobuf.json_format import ParseDict

from a2a.types import (
    Message as A2AMessage,
    SendMessageResponse,
    Task as A2ATask,
)

A2A_PROTOCOL_V1 = "1.0"
A2A_PROTOCOL_V03 = "0.3"
A2A_VERSION_HEADER = "A2A-Version"

SUPPORTED_PROTOCOL_VERSIONS = (A2A_PROTOCOL_V1, A2A_PROTOCOL_V03)


class UnsupportedProtocolVersion(ValueError):
    pass


def advertised_protocol_versions(agent_card: Any) -> List[str]:
    versions: List[str] = []
    for iface in getattr(agent_card, "supported_interfaces", None) or []:
        version = (getattr(iface, "protocol_version", None) or "").strip()
        if version and version not in versions:
            versions.append(version)
    return versions


def select_outbound_version(agent_card: Any = None) -> str:
    advertised = advertised_protocol_versions(agent_card)
    if A2A_PROTOCOL_V1 in advertised or not advertised:
        return A2A_PROTOCOL_V1
    if A2A_PROTOCOL_V03 in advertised:
        return A2A_PROTOCOL_V03
    raise UnsupportedProtocolVersion(
        f"Peer advertises unsupported A2A protocol versions: {advertised}"
    )


def build_send_headers(version: str) -> Dict[str, str]:
    if version == A2A_PROTOCOL_V03:
        return {}
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise UnsupportedProtocolVersion(f"Unsupported A2A protocol version: {version}")
    return {A2A_VERSION_HEADER: version}


def build_send_payload(
    message_dict: Dict[str, Any], request_id: str, version: str
) -> Dict[str, Any]:
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise UnsupportedProtocolVersion(f"Unsupported A2A protocol version: {version}")
    method = "message/send" if version == A2A_PROTOCOL_V03 else "SendMessage"
    return {
        "jsonrpc": "2.0",
        "method": method,
        "id": request_id,
        "params": {"message": message_dict},
    }


def decode_send_result(result: Any, version: str):
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise UnsupportedProtocolVersion(f"Unsupported A2A protocol version: {version}")
    if not isinstance(result, dict):
        raise UnsupportedProtocolVersion("A2A response result is not an object")
    if version == A2A_PROTOCOL_V1:
        try:
            union = ParseDict(result, SendMessageResponse())
        except Exception:
            union = None
        if union is not None:
            branch = union.WhichOneof("payload")
            if branch == "task":
                return union.task
            if branch == "message":
                return union.message
    return _decode_legacy_result(result)


def _decode_legacy_result(result: Dict[str, Any]):
    try:
        return ParseDict(result, A2ATask())
    except Exception:
        pass
    try:
        return ParseDict(result, A2AMessage())
    except Exception:
        pass
    raise UnsupportedProtocolVersion("A2A response result is not a Task or Message")
