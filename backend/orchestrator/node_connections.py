"""Reports the personal node's connections to outside MCP servers as its kernel folds them: each connection with its tools and the node's own sign-in state, plus a count of legacy connector rows read from their key names only.
node_mesh.py supplies the kernel and its keystore, and the node's connection routes and surfaces call status() and never see a token, which the kernel keeps in the node keystore.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from orchestrator.node_mesh import NodeMesh, NodeMeshError

LEGACY_AGENT_ID = "__remote_mcp_connections__"
LEGACY_KEY_PREFIX = "remote_mcp_"
LEGACY_CODE = "connection.legacy_connector"
UNAVAILABLE = {"available": False, "connections": [], "this_node": None, "legacy_connectors": {"count": 0, "code": None}}


def _device(state: Any) -> Dict[str, Any]:
    return {
        "device": state.device,
        "label": state.label,
        "state": state.state,
        "reason": state.reason,
        "issuer": state.issuer,
        "ms": int(state.ms),
    }


def _connection(info: Any) -> Dict[str, Any]:
    return {
        "id": info.id,
        "name": info.name,
        "kind": info.kind,
        "endpoint": info.endpoint,
        "local": bool(info.local),
        "scheme": info.scheme,
        "issuer": info.issuer,
        "scopes": list(info.scopes),
        "flow": info.flow,
        "egress": list(info.egress),
        "tools": [tool.name for tool in info.tools],
        "pending_tools": None if info.pending_tools is None else [tool.name for tool in info.pending_tools],
        "approved_by": info.approved_by,
        "approved_by_removed_device": bool(info.approved_by_removed_device),
        "supported_here": bool(info.supported_here),
        "unsupported": info.unsupported,
        "attention": info.attention,
        "this_device": _device(info.this_device),
        "other_devices": [_device(state) for state in info.other_devices],
    }


def legacy_count(keys: Sequence[str]) -> int:
    return sum(1 for key in keys if isinstance(key, str) and key.startswith(LEGACY_KEY_PREFIX))


def legacy_keys_of(credentials: Any, owner_id: str) -> Callable[[], Sequence[str]]:
    return lambda: credentials.list_credential_keys(owner_id, LEGACY_AGENT_ID)


async def status(
    mesh: Optional[NodeMesh],
    *,
    legacy_keys: Optional[Callable[[], Sequence[str]]] = None,
) -> Dict[str, Any]:
    if mesh is None or not mesh.started:
        return dict(UNAVAILABLE)
    try:
        listed = await mesh.request_call(mesh.kernel.connections)
    except NodeMeshError as exc:
        return {**UNAVAILABLE, "code": exc.code}
    connections: List[Dict[str, Any]] = [_connection(info) for info in listed]
    states: Dict[str, int] = {}
    for item in connections:
        key = item["this_device"]["state"]
        states[key] = states.get(key, 0) + 1
    legacy = legacy_count(legacy_keys()) if legacy_keys is not None else 0
    return {
        "available": True,
        "connections": connections,
        "this_node": {"connections": len(connections), "states": states},
        "legacy_connectors": {"count": legacy, "code": LEGACY_CODE if legacy else None},
    }
