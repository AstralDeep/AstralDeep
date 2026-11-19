"""Reference for descriptor.md: validates an astral.node.descriptor/v2 document the way a client must.
The node's own shape lives in backend/orchestrator/node_api.py; this reference holds clients to the committed rules."""

from __future__ import annotations

from typing import Any

FORMAT = "astral.node.descriptor/v2"
BASE_CAPABILITIES = frozenset(
    {"chat", "tool-dispatch", "mesh-pairing", "delegation-eddsa", "emergency-stop", "mesh-peer", "mesh-mailbox"}
)


class DescriptorError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _string_field(document: dict[str, Any], name: str) -> None:
    if not isinstance(document.get(name), str) or not document[name]:
        raise DescriptorError("descriptor.malformed")


def validate(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise DescriptorError("descriptor.malformed")
    if document.get("format") != FORMAT:
        raise DescriptorError("descriptor.format")
    _string_field(document, "version")
    _string_field(document, "node_id")
    key = document.get("node_key")
    if not isinstance(key, dict) or key.get("kty") != "OKP" or key.get("crv") != "Ed25519":
        raise DescriptorError("descriptor.malformed")
    if "owner_hash" not in document:
        raise DescriptorError("descriptor.malformed")
    if document["owner_hash"] is not None and not (
        isinstance(document["owner_hash"], str) and document["owner_hash"].startswith("sha256:")
    ):
        raise DescriptorError("descriptor.malformed")
    capabilities = document.get("capabilities")
    if not isinstance(capabilities, list) or any(not isinstance(name, str) for name in capabilities):
        raise DescriptorError("descriptor.malformed")
    unknown = [name for name in capabilities if name not in BASE_CAPABILITIES]
    if unknown:
        raise DescriptorError("descriptor.capability")
    transports = document.get("transport_addresses")
    if not isinstance(transports, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("kind"), str) for entry in transports
    ):
        raise DescriptorError("descriptor.malformed")
    mesh = document.get("mesh")
    if mesh is not None and not isinstance(mesh, dict):
        raise DescriptorError("descriptor.malformed")
    mailbox = document.get("mailbox")
    if mailbox is not None and mailbox.get("kind") != "iroh-mailbox":
        raise DescriptorError("descriptor.malformed")
    return {"accepted": True, "capabilities": list(capabilities)}


def run(kind: str, case: dict[str, Any]) -> dict[str, Any]:
    if kind != "validate":
        raise ValueError(kind)
    try:
        return validate(case["document"])
    except DescriptorError as error:
        return {"code": error.code}
