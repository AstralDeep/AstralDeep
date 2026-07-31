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
