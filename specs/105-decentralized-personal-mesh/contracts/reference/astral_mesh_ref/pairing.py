"""Pairing messages: the invite an existing device shows and the request a joining device returns.
Both are signed under the pairing context by an identity key, and the approver turns an accepted request into a roster add entry.
"""

from __future__ import annotations

from typing import Any

import hashlib

from . import crypto
from .cj import b64url_decode
from .errors import MeshError
from .roster import check_device_record

INVITE_FIELDS = {"addr", "expires_s", "inviter", "kind", "mesh", "nonce", "sig", "v"}
REQUEST_FIELDS = {"addr", "device", "kind", "mesh", "nonce", "sig", "v"}


def build_invite(
    identity_scalar: int, inviter: str, mesh: str, addr: str, nonce: str, expires_s: int
) -> dict[str, Any]:
    invite: dict[str, Any] = {
        "addr": addr,
        "expires_s": expires_s,
        "inviter": inviter,
        "kind": "invite",
        "mesh": mesh,
        "nonce": nonce,
        "v": 1,
    }
    invite["sig"] = crypto.sign_object(
        "p256", identity_scalar, "astral.pairing/v1", invite
    )
    return invite


def verify_invite(invite: Any, inviter_identity: bytes, mesh: str, now_s: int) -> None:
    if (
        not isinstance(invite, dict)
        or set(invite) != INVITE_FIELDS
        or invite["kind"] != "invite"
        or invite["v"] != 1
    ):
        raise MeshError("pairing.malformed", "invite fields")
    if invite["mesh"] != mesh:
        raise MeshError("pairing.wrong_mesh", invite["mesh"])
    if crypto.device_id(inviter_identity) != invite["inviter"]:
        raise MeshError("pairing.wrong_signer", "inviter key")
    if not crypto.verify_object("p256", inviter_identity, "astral.pairing/v1", invite):
        raise MeshError("pairing.bad_signature", "invite signature")
    if type(invite["expires_s"]) is not int or now_s > invite["expires_s"]:
        raise MeshError("pairing.expired", "invite")


def build_request(
    identity_scalar: int,
    device_record: dict[str, Any],
    mesh: str,
    addr: str,
    nonce: str,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "addr": addr,
        "device": device_record,
        "kind": "request",
        "mesh": mesh,
        "nonce": nonce,
        "v": 1,
    }
    request["sig"] = crypto.sign_object(
        "p256", identity_scalar, "astral.pairing/v1", request
    )
    return request


def verify_request(request: Any, mesh: str, nonce: str) -> str:
    if (
        not isinstance(request, dict)
        or set(request) != REQUEST_FIELDS
        or request["kind"] != "request"
        or request["v"] != 1
    ):
        raise MeshError("pairing.malformed", "request fields")
    if request["mesh"] != mesh:
        raise MeshError("pairing.wrong_mesh", request["mesh"])
    if request["nonce"] != nonce:
        raise MeshError("pairing.wrong_nonce", "nonce differs from the invite")
    device = check_device_record(request["device"], mesh)
    if not crypto.verify_object(
        "p256",
        b64url_decode(request["device"]["identity"]),
        "astral.pairing/v1",
        request,
    ):
        raise MeshError("pairing.bad_signature", "request signature")
    return device


def _code(label: bytes, *parts: bytes) -> str:
    digest = hashlib.sha256(label + b"".join(parts)).digest()
    number = int.from_bytes(digest[:4], "big") % 100_000_000
    return f"{number // 10_000:04d} {number % 10_000:04d}"


def comparison_code(
    nonce: bytes, identity: bytes, ops: bytes, wrap: bytes
) -> str:
    return _code(b"astral.pairing.code/v1", nonce, identity, ops, wrap)


def invitation_code(nonce: bytes, inviter_identity: bytes) -> str:
    return _code(b"astral.pairing.invitation/v1", nonce, inviter_identity)
