from __future__ import annotations

import pytest

from orchestrator import mcp_authz


@pytest.mark.asyncio
async def test_scope_hierarchy_and_insufficient_scope_challenge(monkeypatch):
    async def decode(_token):
        return {"sub": "u1", "scope": "mcp:tools:read offline_access"}

    monkeypatch.setattr(mcp_authz, "decode_mcp_token", decode)
    claims = await mcp_authz.authorize_mcp_request(
        headers={"authorization": "Bearer opaque"},
        query_params={},
        cookies={},
        required_scopes=("mcp:discover", "mcp:tools:read"),
    )
    assert claims["sub"] == "u1"
    assert "offline_access" not in mcp_authz.effective_mcp_scopes(claims)
    with pytest.raises(mcp_authz.MCPAuthError) as raised:
        await mcp_authz.authorize_mcp_request(
            headers={"authorization": "Bearer opaque"},
            query_params={},
            cookies={},
            required_scopes=("mcp:tools:invoke",),
        )
    assert raised.value.status_code == 403
    assert raised.value.required_scopes == ("mcp:tools:invoke",)

    async def decode_discover_only(_token):
        return {"sub": "u1", "scope": "mcp:discover"}

    monkeypatch.setattr(mcp_authz, "decode_mcp_token", decode_discover_only)
    with pytest.raises(mcp_authz.MCPAuthError) as multiple:
        await mcp_authz.authorize_mcp_request(
            headers={"authorization": "Bearer opaque"},
            query_params={},
            cookies={},
            required_scopes=("mcp:tools:read", "mcp:tools:invoke"),
        )
    challenge = mcp_authz.challenge_header(
        "https://mcp.test",
        error=multiple.value.error,
        required_scopes=multiple.value.required_scopes,
    )
    assert 'scope="mcp:tools:read mcp:tools:invoke"' in challenge


@pytest.mark.asyncio
async def test_token_value_is_not_returned_or_forwarded(monkeypatch):
    tripwire = "tripwire-inbound-bearer"

    async def decode(token):
        assert token == tripwire
        return {"sub": "u1", "scope": "mcp:tools:invoke"}

    monkeypatch.setattr(mcp_authz, "decode_mcp_token", decode)
    claims = await mcp_authz.authorize_mcp_request(
        headers={"authorization": f"Bearer {tripwire}"},
        query_params={},
        cookies={},
        required_scopes=("mcp:tools:invoke",),
    )
    assert tripwire not in repr(claims)
    assert "_raw_token" not in claims


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


@pytest.mark.asyncio
async def test_mock_token_is_mcp_specific(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    with pytest.raises(ValueError, match="development token"):
        await mcp_authz.decode_mcp_token("dev-token")
    claims = await mcp_authz.decode_mcp_token("dev-mcp-token")
    assert claims["aud"] == mcp_authz.MCP_AUDIENCE


@pytest.mark.asyncio
async def test_real_token_path_uses_strict_issuer_and_audience(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.test/realm/")
    observed = {}

    async def fake_jwks(url, *, token):
        observed["jwks"] = (url, token)
        return {"keys": []}

    def fake_decode(token, jwks, **kwargs):
        observed["decode"] = (token, jwks, kwargs)
        return {"sub": "u1"}

    monkeypatch.setattr(mcp_authz, "get_jwks", fake_jwks)
    monkeypatch.setattr(mcp_authz.jose_jwt, "decode", fake_decode)
    assert (await mcp_authz.decode_mcp_token("opaque"))["sub"] == "u1"
    assert observed["jwks"] == (
        "https://idp.test/realm/protocol/openid-connect/certs",
        "opaque",
    )
    assert observed["decode"][2]["audience"] == "astral-mcp"
    assert observed["decode"][2]["issuer"] == "https://idp.test/realm"

    monkeypatch.setattr(mcp_authz.jose_jwt, "decode", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="no subject"):
        await mcp_authz.decode_mcp_token("opaque")


@pytest.mark.asyncio
async def test_authorization_wraps_decoder_failures(monkeypatch):
    async def fail(_token):
        raise ValueError("secret decoder detail")

    monkeypatch.setattr(mcp_authz, "decode_mcp_token", fail)
    with pytest.raises(mcp_authz.MCPAuthError) as raised:
        await mcp_authz.authorize_mcp_request(
            headers={"authorization": "Bearer opaque"},
            query_params={},
            cookies={},
            required_scopes=("mcp:discover",),
        )
    assert raised.value.description == "invalid bearer token"
