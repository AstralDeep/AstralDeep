"""Node-signed in-process agent authority: scoped, depth-bounded delegation tokens bound to a request's verified machine authority, with child attenuation, actor-chain checks and chained tool authorization.
The orchestrator, chain_authority.py, evidence_context.py, mesh_enrollment.py and the benchmark and verification drivers mint and check these tokens, signed with the node key from shared/node_identity.py."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Dict, List, Optional

from shared import node_identity

AGENT_AUDIENCE = "astral-agent-service"


def is_tool_in_scope(tool_name: str, scopes: List[str], required_scope: str = "") -> bool:
    tool_scopes = [s for s in scopes if s.startswith("tool:")]

    # No fallthrough: a hop could launder unheld authority
    if required_scope:
        if required_scope not in scopes:
            return False
        return (not tool_scopes) or (f"tool:{tool_name}" in tool_scopes)

    if not tool_scopes:
        return True
    return f"tool:{tool_name}" in tool_scopes


DEFAULT_MAX_DELEGATION_DEPTH = 3

DELEGATION_DEPTH_CLAIM = "delegation_depth"
MAX_DEPTH_CLAIM = "max_delegation_depth"

_DELEGATION_CLOCK_SKEW_SECONDS = 60

_ACTOR_CHAIN_WALK_CAP = 64


class RecursiveDelegationError(Exception):
    pass


class DelegationDepthExceeded(RecursiveDelegationError):
    pass


class DelegationConfigError(RecursiveDelegationError):
    pass


def recursive_delegation_enabled() -> bool:
    try:
        from shared.feature_flags import flags
        return bool(flags.is_enabled("recursive_delegation"))
    except Exception:  # pragma: no cover
        return False


_DELEGATION_ISSUER = "astral-delegation"
_process_dev_key: Optional[bytes] = None
_NODE_HS256_INFO = "astral.delegation.hs256/v1"


def _node_identity_or_config_error():
    try:
        return node_identity.load_node_identity()
    except node_identity.NodeIdentityError as exc:
        raise DelegationConfigError(f"node signing key unavailable: {exc}") from exc


def _child_signing_key() -> bytes:
    if node_identity.is_personal():
        return _node_identity_or_config_error().derive_secret(_NODE_HS256_INFO)
    key = os.getenv("DELEGATION_CHILD_SIGNING_KEY") or os.getenv("MEMORY_HMAC_KEY")
    if key:
        return key.encode("utf-8")
    from orchestrator.boot_posture import is_dev_mode
    if not is_dev_mode():
        raise DelegationConfigError(
            "child delegation minting requires DELEGATION_CHILD_SIGNING_KEY "
            "(or MEMORY_HMAC_KEY) to be set in production posture — refusing "
            "to sign with a derived or committed key.")
    global _process_dev_key
    if _process_dev_key is None:
        _process_dev_key = secrets.token_bytes(32)
    return _process_dev_key


def _audience_list(value) -> List[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str) and item]
    return []


def encode_delegation_payload(payload: dict) -> str:
    if node_identity.is_personal():
        now = payload.get("iat") if type(payload.get("iat")) is int else int(time.time())
        exp = payload.get("exp") if type(payload.get("exp")) is int else now + node_identity.MAX_TOKEN_SECONDS
        return _node_identity_or_config_error().mint_token(
            payload,
            audience=_audience_list(payload.get("aud")),
            lifetime=max(1, exp - now),
            now=now,
        )
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = (
        base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode())
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode())
    signing_input = f"{header_b64}.{payload_b64}"
    signature = hmac.new(
        _child_signing_key(), signing_input.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{signing_input}.{sig_b64}"


def verify_signed_delegation(token: str, *, audience: Optional[str] = None,
                             issuer: Optional[str] = None) -> dict:
    alg = node_identity.peek_algorithm(token)
    if alg == "EdDSA":
        if not node_identity.is_personal():
            raise RecursiveDelegationError("node-signed delegation is not accepted here")
        try:
            claims = node_identity.load_node_identity(create=False).verify_token(
                token, audience=audience)
        except node_identity.NodeIdentityError as exc:
            raise RecursiveDelegationError(f"delegation signature refused: {exc}") from exc
    elif alg == "HS256" and not node_identity.is_personal():
        from jose import jwt as jose_jwt
        try:
            claims = jose_jwt.decode(
                token, _child_signing_key(), algorithms=["HS256"], audience=audience,
                issuer=issuer, options={"verify_aud": audience is not None,
                                        "require_exp": True})
        except Exception as exc:
            raise RecursiveDelegationError("delegation signature refused") from exc
    else:
        raise RecursiveDelegationError("delegation signature algorithm refused")
    if issuer is not None and claims.get("iss") != issuer:
        raise RecursiveDelegationError("delegation issuer refused")
    return claims


def decode_token_payload(token: str) -> Optional[dict]:
    try:
        if not token or token.count(".") != 2:
            return None
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
        return decoded if isinstance(decoded, dict) else None
    except Exception:
        return None


def attenuate_scopes(parent_scopes, requested_scopes) -> List[str]:
    parent_set = set(parent_scopes or [])
    requested_set = set(requested_scopes or [])
    return sorted(parent_set & requested_set)


def _token_scopes(token: dict) -> List[str]:
    return (token.get("scope", "") or "").split()


def _token_depth(token: dict) -> int:
    try:
        return int(token.get(DELEGATION_DEPTH_CLAIM, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _walk_actor_chain(token: dict):
    actors: List[str] = []
    node = token.get("act")
    steps = 0
    while isinstance(node, dict) and "sub" in node:
        actors.append(node["sub"])
        steps += 1
        if steps > _ACTOR_CHAIN_WALK_CAP:
            return actors, False
        if "act" in node:
            nxt = node["act"]
            if not isinstance(nxt, dict) or "sub" not in nxt:
                return actors, False
            node = nxt
        else:
            return actors, True
    return actors, bool(actors)


def actor_chain(token: dict) -> List[str]:
    return _walk_actor_chain(token)[0]


def normalize_hop_parent(payload: dict, initiating_agent_id: str) -> dict:
    if not isinstance(payload, dict):
        return payload
    act = payload.get("act")
    if isinstance(act, dict) and isinstance(act.get("sub"), str) and act["sub"]:
        return payload
    normalized = dict(payload)
    normalized["act"] = {"sub": f"agent:{initiating_agent_id}"}
    return normalized


def mint_child_delegation(parent: dict, child_agent_id: str,
                          requested_scopes, now: Optional[int] = None) -> dict:
    now = int(now if now is not None else time.time())

    child_depth = _token_depth(parent) + 1
    max_depth = min(
        int(parent.get(MAX_DEPTH_CLAIM, DEFAULT_MAX_DELEGATION_DEPTH)),
        DEFAULT_MAX_DELEGATION_DEPTH,
    )
    if child_depth > max_depth:
        raise DelegationDepthExceeded(
            f"minting at depth {child_depth} exceeds maximum {max_depth}"
        )

    child_scopes = attenuate_scopes(_token_scopes(parent), requested_scopes)

    child_act = {"sub": f"agent:{child_agent_id}"}
    parent_act = parent.get("act")
    if isinstance(parent_act, dict):
        child_act["act"] = parent_act

    parent_exp = int(parent.get("exp", now))
    child: Dict = {
        "sub": parent.get("sub"),
        "act": child_act,
        "scope": " ".join(child_scopes),
        "iss": parent.get("iss", _DELEGATION_ISSUER),
        "aud": parent.get("aud"),
        "iat": now,
        "exp": parent_exp,
        "delegation": True,
        DELEGATION_DEPTH_CLAIM: child_depth,
        MAX_DEPTH_CLAIM: max_depth,
    }
    if "astral_mesh" in parent:
        if type(parent["astral_mesh"]) is not dict or type(parent.get("cnf")) is not dict:
            raise RecursiveDelegationError("mesh parent identity is incomplete")
        child["astral_mesh"] = dict(parent["astral_mesh"])
        child["cnf"] = dict(parent["cnf"])
    return child


def verify_delegation_chain(token: dict, now: Optional[int] = None,
                            expected_human_sub: Optional[str] = None,
                            raw_token: Optional[str] = None):
    now = int(now if now is not None else time.time())

    if raw_token is not None:
        try:
            signed = verify_signed_delegation(raw_token)
        except RecursiveDelegationError as exc:
            return False, str(exc)
        if signed != token:
            return False, "claims do not match the signed delegation"

    depth = _token_depth(token)
    try:
        recorded_max = int(token.get(MAX_DEPTH_CLAIM, DEFAULT_MAX_DELEGATION_DEPTH))
    except (TypeError, ValueError):
        recorded_max = DEFAULT_MAX_DELEGATION_DEPTH
    max_depth = min(recorded_max, DEFAULT_MAX_DELEGATION_DEPTH)
    if depth < 0:
        return False, "negative delegation depth"
    if depth > max_depth:
        return False, f"delegation depth {depth} exceeds maximum {max_depth}"

    human_sub = token.get("sub")
    if not human_sub or (isinstance(human_sub, str) and human_sub.startswith("agent:")):
        return False, "chain does not terminate at a human principal"
    actors, complete = _walk_actor_chain(token)
    if not complete:
        return False, "actor chain is broken or incomplete"
    if len(actors) != depth + 1:
        return False, (
            f"actor-chain length {len(actors)} inconsistent with depth {depth}"
        )
    if expected_human_sub is not None and human_sub != expected_human_sub:
        return False, "human principal does not match expected authorizer"

    exp = token.get("exp")
    if exp is not None:
        try:
            if now > int(exp) + _DELEGATION_CLOCK_SKEW_SECONDS:
                return False, "delegation token expired"
        except (TypeError, ValueError):
            return False, "malformed expiry"

    return True, ""


def authorize_chained_tool_call(token: dict, tool_name: str,
                                required_scope: str = "",
                                now: Optional[int] = None):
    ok, reason = verify_delegation_chain(token, now=now)
    if not ok:
        return False, reason
    scopes = _token_scopes(token)
    if not is_tool_in_scope(tool_name, scopes, required_scope):
        acting = actor_chain(token)[:1]
        return False, f"tool '{tool_name}' outside delegated scope for {acting}"
    return True, ""


def delegation_chain_audit_record(parent: dict, child: dict,
                                  operation: str = "", tool: str = "",
                                  now: Optional[int] = None) -> dict:
    now = int(now if now is not None else time.time())
    child_act = child.get("act") or {}
    parent_act = parent.get("act") or {}
    return {
        "event": "delegation_chain_hop",
        "acting_agent": child_act.get("sub"),
        "parent_actor": parent_act.get("sub"),
        "human_authorizer": child.get("sub") or parent.get("sub"),
        "operation": operation or tool,
        "tool": tool or operation,
        "scope": child.get("scope", ""),
        "delegation_depth": _token_depth(child),
        "actor_chain": actor_chain(child),
        "timestamp": now,
    }
