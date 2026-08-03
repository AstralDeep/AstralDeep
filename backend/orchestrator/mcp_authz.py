"""MCP-only OAuth resource-server boundary for feature 064.

This module is deliberately not shared with the web/native bearer dependency:
MCP tokens have a distinct audience and scopes, and the inbound bearer value is
discarded after validation rather than being stored on an orchestrator session.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from jose import jwt as jose_jwt

from shared.jwks_cache import get_jwks


MCP_AUDIENCE = "astral-mcp"
MCP_SCOPES = (
    "mcp:discover",
    "mcp:tools:read",
    "mcp:tools:invoke",
)

_SCOPE_IMPLICATIONS = {
    "mcp:discover": frozenset({"mcp:discover"}),
    "mcp:tools:read": frozenset({"mcp:discover", "mcp:tools:read"}),
    "mcp:tools:invoke": frozenset(MCP_SCOPES),
}


@dataclass(frozen=True)
class MCPAuthError(Exception):
    status_code: int
    error: str
    description: str
    required_scopes: tuple[str, ...]


def canonical_public_base_url(value: str | None = None) -> str:
    """Return the configured canonical origin, never a request-derived host."""

    raw = (value if value is not None else os.getenv("PUBLIC_BASE_URL", "")).strip()
    if not raw:
        raise RuntimeError("PUBLIC_BASE_URL is required when FF_MCP_SERVER is enabled")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise RuntimeError("PUBLIC_BASE_URL must be a credential-free canonical origin")
    if parsed.path not in {"", "/"}:
        raise RuntimeError("PUBLIC_BASE_URL must not contain a path")
    if (
        os.getenv("ASTRAL_ENV", "").strip().lower() != "development"
        and parsed.scheme != "https"
    ):
        raise RuntimeError("PUBLIC_BASE_URL must use HTTPS outside development")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", "")).rstrip("/")


def resource_identifier(public_base_url: str) -> str:
    return f"{canonical_public_base_url(public_base_url)}/mcp"


def resource_metadata_url(public_base_url: str) -> str:
    return (
        f"{canonical_public_base_url(public_base_url)}"
        "/.well-known/oauth-protected-resource/mcp"
    )


def protected_resource_metadata(public_base_url: str) -> dict[str, Any]:
    authority = os.getenv("KEYCLOAK_AUTHORITY", "").strip().rstrip("/")
    if not authority:
        raise RuntimeError("KEYCLOAK_AUTHORITY is required when FF_MCP_SERVER is enabled")
    return {
        "resource": resource_identifier(public_base_url),
        "authorization_servers": [authority],
        "scopes_supported": list(MCP_SCOPES),
        "bearer_methods_supported": ["header"],
    }


def _scope_values(payload: Mapping[str, Any]) -> frozenset[str]:
    raw = payload.get("scope", "")
    if isinstance(raw, str):
        values = raw.split()
    elif isinstance(raw, (list, tuple, set)):
        values = [str(value) for value in raw]
    else:
        values = []
    # offline_access is intentionally neither published nor authoritative here.
    return frozenset(value for value in values if value != "offline_access")


def effective_mcp_scopes(payload: Mapping[str, Any]) -> frozenset[str]:
    effective: set[str] = set()
    for scope in _scope_values(payload):
        effective.update(_SCOPE_IMPLICATIONS.get(scope, (scope,)))
    return frozenset(effective)


def challenge_header(
    public_base_url: str,
    *,
    error: str,
    required_scopes: Iterable[str],
) -> str:
    scopes = " ".join(dict.fromkeys(required_scopes))
    return (
        f'Bearer resource_metadata="{resource_metadata_url(public_base_url)}", '
        f'scope="{scopes}", error="{error}"'
    )


def bearer_from_headers(
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
    cookies: Mapping[str, str],
    *,
    required_scopes: Iterable[str],
) -> str:
    required = tuple(required_scopes)
    if any(key.lower() in {"token", "access_token"} for key in query_params):
        raise MCPAuthError(401, "invalid_token", "URI bearer tokens are not accepted", required)
    auth = headers.get("authorization", "")
    if not auth:
        # Cookie-only requests are refused exactly like missing credentials. We
        # never inspect cookie values, so session material cannot become a token.
        description = (
            "cookie credentials are not accepted"
            if cookies
            else "missing bearer token"
        )
        raise MCPAuthError(401, "invalid_token", description, required)
    scheme, separator, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip() or " " in token.strip():
        raise MCPAuthError(401, "invalid_token", "malformed bearer token", required)
    return token.strip()


async def decode_mcp_token(token: str) -> dict[str, Any]:
    """Validate one MCP bearer and return claims without retaining token bytes."""

    if os.getenv("USE_MOCK_AUTH", "").strip().lower() == "true":
        # Deliberately unlike the legacy dependency: only the MCP-specific dev
        # credential is accepted. The web ``dev-token`` must fail this audience.
        if token != "dev-mcp-token":
            raise ValueError("invalid MCP development token")
        return {
            "sub": "test_user",
            "iss": "mock-astral-mcp",
            "aud": MCP_AUDIENCE,
            "scope": " ".join(MCP_SCOPES),
            "realm_access": {"roles": ["user"]},
        }

    authority = os.getenv("KEYCLOAK_AUTHORITY", "").strip().rstrip("/")
    if not authority:
        raise RuntimeError("KEYCLOAK_AUTHORITY is not configured")
    jwks = await get_jwks(
        f"{authority}/protocol/openid-connect/certs",
        token=token,
    )
    payload = jose_jwt.decode(
        token,
        jwks,
        algorithms=["RS256"],
        audience=MCP_AUDIENCE,
        issuer=authority,
        options={"verify_at_hash": False},
    )
    if not isinstance(payload, dict) or not payload.get("sub"):
        raise ValueError("MCP token has no subject")
    return payload


async def authorize_mcp_request(
    *,
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
    cookies: Mapping[str, str],
    required_scopes: Iterable[str],
) -> dict[str, Any]:
    required = tuple(required_scopes)
    token = bearer_from_headers(
        headers,
        query_params,
        cookies,
        required_scopes=required,
    )
    try:
        payload = await decode_mcp_token(token)
    except MCPAuthError:
        raise
    except Exception as exc:
        raise MCPAuthError(401, "invalid_token", "invalid bearer token", required) from exc
    missing = tuple(scope for scope in required if scope not in effective_mcp_scopes(payload))
    if missing:
        raise MCPAuthError(403, "insufficient_scope", "required MCP scope is missing", missing)
    # Return a detached claims mapping only. The raw bearer is intentionally not
    # part of this result, request.state, an orchestrator session, or any log.
    return dict(payload)


__all__ = [
    "MCP_AUDIENCE",
    "MCP_SCOPES",
    "MCPAuthError",
    "authorize_mcp_request",
    "canonical_public_base_url",
    "challenge_header",
    "decode_mcp_token",
    "effective_mcp_scopes",
    "protected_resource_metadata",
    "resource_identifier",
    "resource_metadata_url",
]
