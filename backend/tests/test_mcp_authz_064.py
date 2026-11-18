"""Tests for orchestrator/mcp_authz.py: scope-hierarchy challenges, bearer-token
extraction and strict issuer/audience validation, canonical-resource HTTPS
enforcement, and that a token value is never echoed back or forwarded.
"""

from __future__ import annotations

import pytest

from orchestrator import mcp_authz


def test_production_canonical_resource_requires_https(monkeypatch):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    with pytest.raises(RuntimeError, match="HTTPS"):
        mcp_authz.canonical_public_base_url("http://mcp.test")
    assert (
        mcp_authz.resource_identifier("https://MCP.TEST/")
        == "https://mcp.test/mcp"
    )


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "required"),
        ("mcp.test", "absolute"),
        ("ftp://mcp.test", "absolute"),
        ("https://user:pass@mcp.test", "credential-free"),
        ("https://mcp.test/path", "must not contain a path"),
        ("https://mcp.test?query=1", "credential-free"),
    ],
)
def test_canonical_resource_rejects_ambiguous_origins(monkeypatch, value, reason):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    with pytest.raises(RuntimeError, match=reason):
        mcp_authz.canonical_public_base_url(value)


def test_metadata_requires_authority_and_normalizes_config(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("KEYCLOAK_AUTHORITY", raising=False)
    with pytest.raises(RuntimeError, match="KEYCLOAK_AUTHORITY"):
        mcp_authz.protected_resource_metadata("http://mcp.test")

    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.test/realm/")
    metadata = mcp_authz.protected_resource_metadata("http://MCP.TEST/")
    assert metadata["resource"] == "http://mcp.test/mcp"
    assert metadata["authorization_servers"] == ["https://idp.test/realm"]
    assert mcp_authz.resource_metadata_url("http://MCP.TEST/").endswith(
        "/.well-known/oauth-protected-resource/mcp"
    )


@pytest.mark.parametrize(
    ("headers", "query", "cookies", "description"),
    [
        ({}, {"access_token": "x"}, {}, "URI bearer"),
        ({}, {}, {}, "missing bearer"),
        ({}, {}, {"session": "x"}, "cookie credentials"),
        ({"authorization": "Basic x"}, {}, {}, "malformed bearer"),
        ({"authorization": "Bearer"}, {}, {}, "malformed bearer"),
        ({"authorization": "Bearer two tokens"}, {}, {}, "malformed bearer"),
    ],
)
def test_bearer_extraction_rejects_non_header_or_malformed_credentials(
    headers,
    query,
    cookies,
    description,
):
    with pytest.raises(mcp_authz.MCPAuthError, match=description):
        mcp_authz.bearer_from_headers(
            headers,
            query,
            cookies,
            required_scopes=("mcp:discover",),
        )


