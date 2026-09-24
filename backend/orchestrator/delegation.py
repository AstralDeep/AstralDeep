"""RFC 8693 token-exchange delegation service minting and verifying scoped,
depth-bounded tokens agents use to act for a user, including recursive
child-delegation chains; used by chain_authority.py and orchestrator.py.
"""

import os
import time
import json
import hmac
import hashlib
import base64
import logging
from typing import Optional, Dict, List

import aiohttp


logger = logging.getLogger("DelegationService")


GRANT_TYPE_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"


class DelegationService:
    def __init__(self):
        self.authority = os.getenv("KEYCLOAK_AUTHORITY", "")
        self.client_id = os.getenv("KEYCLOAK_CLIENT_ID", "")
        self.client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
        self.agent_service_client_id = os.getenv(
            "AGENT_SERVICE_CLIENT_ID", "astral-agent-service"
        )
        self.agent_service_client_secret = os.getenv(
            "AGENT_SERVICE_CLIENT_SECRET", ""
        )
        self.mock_auth = os.getenv("USE_MOCK_AUTH", "false").lower() == "true"

        if self.mock_auth:
            logger.info("DelegationService running in MOCK mode")
        else:
            logger.info(
                f"DelegationService configured for Keycloak: {self.authority}"
            )

    async def exchange_token_for_agent(
        self,
        user_token: str,
        agent_id: str,
        allowed_tools: List[str],
        user_id: Optional[str] = None,
        enabled_scopes: Optional[List[str]] = None,
    ) -> Dict:
        if not enabled_scopes:
            logger.error(
                f"Refusing token exchange for agent '{agent_id}': the user has "
                f"no effective tool scopes for this agent"
            )
            return {
                "error": "no_enabled_scopes",
                "error_description": (
                    "no enabled tool scopes for this user/agent pair — "
                    "delegation would carry no authority"
                ),
            }

        if self.mock_auth:
            return self._create_mock_delegation_token(
                agent_id, allowed_tools, user_id, enabled_scopes
            )

        return await self._exchange_via_keycloak(
            user_token, agent_id, allowed_tools, enabled_scopes
        )

    async def _exchange_via_keycloak(
        self,
        user_token: str,
        agent_id: str,
        allowed_tools: List[str],
        enabled_scopes: Optional[List[str]] = None,
    ) -> Dict:
        if not self.authority or not self.client_id or not self.client_secret:
            return {
                "error": "server_error",
                "error_description": "Keycloak not configured for delegation",
            }

        token_url = f"{self.authority}/protocol/openid-connect/token"

        combined_scopes = " ".join(enabled_scopes or [])

        form_data = {
            "grant_type": GRANT_TYPE_TOKEN_EXCHANGE,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "subject_token": user_token,
            "subject_token_type": TOKEN_TYPE_ACCESS,
            "requested_token_type": TOKEN_TYPE_ACCESS,
            "audience": self.agent_service_client_id,
            "scope": combined_scopes,
        }

        logger.info(
            f"Exchanging token for agent '{agent_id}' with "
            f"{len(allowed_tools)} allowed tool(s) (scope='{combined_scopes}')"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    token_url, data=form_data
                ) as resp:
                    body = await resp.json()
                    if resp.status != 200:
                        logger.error(
                            f"Token exchange failed: {resp.status} {body}"
                        )
                        return {
                            "error": body.get("error", "exchange_failed"),
                            "error_description": body.get(
                                "error_description",
                                f"Keycloak returned {resp.status}",
                            ),
                        }

                    logger.info(
                        f"Token exchange successful for agent '{agent_id}'"
                    )
                    return {
                        "access_token": body["access_token"],
                        "token_type": body.get("token_type", "Bearer"),
                        "expires_in": body.get("expires_in", 300),
                        "scope": body.get("scope", combined_scopes),
                        "issued_token_type": body.get(
                            "issued_token_type", TOKEN_TYPE_ACCESS
                        ),
                        "agent_id": agent_id,
                    }
        except Exception as e:
            logger.error(f"Token exchange error: {e}")
            return {
                "error": "exchange_error",
                "error_description": str(e),
            }

    def _create_mock_delegation_token(
        self,
        agent_id: str,
        allowed_tools: List[str],
        user_id: Optional[str] = None,
        enabled_scopes: Optional[List[str]] = None,
    ) -> Dict:
        now = int(time.time())
        scope_parts = list(enabled_scopes or [])
        scope_parts.extend(f"tool:{t}" for t in allowed_tools)
        combined_scopes = " ".join(scope_parts)

        payload = {
            "sub": user_id or "dev-user-id",
            "preferred_username": "DevUser",
            "act": {"sub": f"agent:{agent_id}"},
            "scope": combined_scopes,
            "iss": "mock-astral-delegation",
            "aud": self.agent_service_client_id,
            "iat": now,
            "exp": now + 300,
            "azp": self.client_id or "astral-frontend",
            "realm_access": {"roles": ["user"]},
            "delegation": True,
        }

        header = {"alg": "HS256", "typ": "JWT"}
        header_b64 = (
            base64.urlsafe_b64encode(json.dumps(header).encode())
            .rstrip(b"=")
            .decode()
        )
        payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(payload).encode())
            .rstrip(b"=")
            .decode()
        )
        signing_input = f"{header_b64}.{payload_b64}"
        signature = hmac.new(
            b"mock-delegation-secret", signing_input.encode(), hashlib.sha256
        ).digest()
        sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        mock_token = f"{signing_input}.{sig_b64}"

        logger.info(
            f"Mock delegation token created for agent '{agent_id}' "
            f"with {len(allowed_tools)} tools"
        )

        return {
            "access_token": mock_token,
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": combined_scopes,
            "issued_token_type": TOKEN_TYPE_ACCESS,
            "agent_id": agent_id,
        }

    @staticmethod
    def extract_delegation_info(token_payload: dict) -> Optional[Dict]:
        act_claim = token_payload.get("act")
        if not act_claim:
            return None

        return {
            "user_id": token_payload.get("sub"),
            "actor": act_claim.get("sub"),
            "scopes": (token_payload.get("scope", "")).split(),
            "is_delegation": True,
        }

    @staticmethod
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


def _child_signing_key() -> bytes:
    key = os.getenv("DELEGATION_CHILD_SIGNING_KEY") or os.getenv("MEMORY_HMAC_KEY")
    if key:
        return key.encode("utf-8")
    from orchestrator.session_store import is_dev_mode
    if not is_dev_mode():
        raise DelegationConfigError(
            "child delegation minting requires DELEGATION_CHILD_SIGNING_KEY "
            "(or MEMORY_HMAC_KEY) to be set in production posture — refusing "
            "to sign with the committed development constant.")
    return b"mock-delegation-secret"


def encode_delegation_payload(payload: dict) -> str:
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
        "iss": parent.get("iss", "mock-astral-delegation"),
        "aud": parent.get("aud"),
        "iat": now,
        "exp": parent_exp,
        "delegation": True,
        DELEGATION_DEPTH_CLAIM: child_depth,
        MAX_DEPTH_CLAIM: max_depth,
    }
    return child


def verify_delegation_chain(token: dict, now: Optional[int] = None,
                            expected_human_sub: Optional[str] = None):
    now = int(now if now is not None else time.time())

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
    if not DelegationService.is_tool_in_scope(tool_name, scopes, required_scope):
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
