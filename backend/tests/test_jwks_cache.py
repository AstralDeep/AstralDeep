"""Tests for shared/jwks_cache.py: TTL cache hits and expiry, the kid-miss rotation
refetch, clear() semantics, per-URL keying, and that orchestrator.auth and
orchestrator.orchestrator both route through the shared cache.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import sys
import types
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared import jwks_cache


def _url() -> str:
    return f"https://idp.example/realms/{uuid.uuid4()}/protocol/openid-connect/certs"


def _make_token(kid: str) -> str:
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "kid": kid}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.e30.sig"


@pytest.fixture(autouse=True)
def _clean_cache():
    jwks_cache.clear()
    yield
    jwks_cache.clear()


@pytest.fixture
def fetch_counter(monkeypatch):
    state = {"calls": 0, "jwks": {"keys": [{"kid": "k1", "kty": "RSA"}]}}

    async def _fake_fetch(jwks_url):
        state["calls"] += 1
        jwks = state["jwks"]
        jwks_cache._cache[jwks_url] = {
            "jwks": jwks,
            "fetched_at": jwks_cache.time.time(),
        }
        return jwks

    monkeypatch.setattr(jwks_cache, "_fetch", _fake_fetch)
    return state


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"now": 1_000_000.0}
    monkeypatch.setattr(
        jwks_cache, "time", types.SimpleNamespace(time=lambda: state["now"])
    )
    return state


def test_second_call_within_ttl_does_not_refetch(fetch_counter):
    url = _url()

    async def run():
        first = await jwks_cache.get_jwks(url)
        second = await jwks_cache.get_jwks(url)
        return first, second

    first, second = asyncio.run(run())
    assert fetch_counter["calls"] == 1
    assert first == second == fetch_counter["jwks"]


def test_within_ttl_with_known_kid_does_not_refetch(fetch_counter):
    url = _url()

    async def run():
        await jwks_cache.get_jwks(url)
        return await jwks_cache.get_jwks(url, token=_make_token("k1"))

    jwks = asyncio.run(run())
    assert fetch_counter["calls"] == 1
    assert jwks == fetch_counter["jwks"]


def test_malformed_token_does_not_defeat_cache(fetch_counter):
    url = _url()

    async def run():
        await jwks_cache.get_jwks(url)
        return await jwks_cache.get_jwks(url, token="not-a-jwt")

    jwks = asyncio.run(run())
    assert fetch_counter["calls"] == 1
    assert jwks == fetch_counter["jwks"]


def test_cache_is_keyed_per_url(fetch_counter):
    url_a, url_b = _url(), _url()

    async def run():
        await jwks_cache.get_jwks(url_a)
        await jwks_cache.get_jwks(url_b)
        await jwks_cache.get_jwks(url_a)
        await jwks_cache.get_jwks(url_b)

    asyncio.run(run())
    assert fetch_counter["calls"] == 2


def test_ttl_expiry_refetches(fetch_counter, fake_clock):
    url = _url()

    asyncio.run(jwks_cache.get_jwks(url))
    assert fetch_counter["calls"] == 1

    fake_clock["now"] += jwks_cache._TTL_SECONDS + 1
    asyncio.run(jwks_cache.get_jwks(url))
    assert fetch_counter["calls"] == 2


def test_just_under_ttl_still_cached(fetch_counter, fake_clock):
    url = _url()

    asyncio.run(jwks_cache.get_jwks(url))
    fake_clock["now"] += jwks_cache._TTL_SECONDS - 1
    asyncio.run(jwks_cache.get_jwks(url))
    assert fetch_counter["calls"] == 1


def test_exactly_at_ttl_refetches(fetch_counter, fake_clock):
    url = _url()

    asyncio.run(jwks_cache.get_jwks(url))
    fake_clock["now"] += jwks_cache._TTL_SECONDS
    asyncio.run(jwks_cache.get_jwks(url))
    assert fetch_counter["calls"] == 2


def test_kid_miss_refetches_once_and_returns_rotated_key(fetch_counter):
    url = _url()

    async def run():
        await jwks_cache.get_jwks(url)
        fetch_counter["jwks"] = {
            "keys": [{"kid": "k1", "kty": "RSA"}, {"kid": "k2", "kty": "RSA"}]
        }
        return await jwks_cache.get_jwks(url, token=_make_token("k2"))

    jwks = asyncio.run(run())
    assert fetch_counter["calls"] == 2
    assert "k2" in jwks_cache._kids(jwks)


@pytest.mark.asyncio
async def test_kid_miss_refetch_updates_cache_for_subsequent_calls():
    url = _url()
    calls = {"n": 0}
    docs = [
        {"keys": [{"kid": "k1", "kty": "RSA"}]},
        {"keys": [{"kid": "k2", "kty": "RSA"}]},
    ]

    async def _fake_fetch(jwks_url):
        jwks = docs[min(calls["n"], len(docs) - 1)]
        calls["n"] += 1
        jwks_cache._cache[jwks_url] = {
            "jwks": jwks,
            "fetched_at": jwks_cache.time.time(),
        }
        return jwks

    original = jwks_cache._fetch
    jwks_cache._fetch = _fake_fetch
    try:
        await jwks_cache.get_jwks(url)
        rotated = await jwks_cache.get_jwks(url, token=_make_token("k2"))
        again = await jwks_cache.get_jwks(url, token=_make_token("k2"))
    finally:
        jwks_cache._fetch = original

    assert calls["n"] == 2
    assert rotated == again == docs[1]


def test_clear_empties_cache_and_forces_refetch(fetch_counter):
    url = _url()

    asyncio.run(jwks_cache.get_jwks(url))
    assert url in jwks_cache._cache

    jwks_cache.clear()
    assert jwks_cache._cache == {}

    asyncio.run(jwks_cache.get_jwks(url))
    assert fetch_counter["calls"] == 2


def test_orchestrator_validate_token_uses_shared_jwks_cache():
    from orchestrator.orchestrator import Orchestrator

    src = inspect.getsource(Orchestrator.validate_token)
    assert "shared.jwks_cache" in src
    assert "get_jwks" in src


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_orchestrator_auth_module_uses_shared_jwks_cache(monkeypatch, invalid):
    from unittest.mock import AsyncMock, Mock

    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    import orchestrator.auth as auth_mod

    authority = "https://idp.example/realms/test"
    keys = {"keys": [{"kid": "test-key"}]}
    payload = {"sub": "owner", "iss": authority, "azp": "astral-frontend"}
    cache = AsyncMock(return_value=keys)
    decode = Mock(side_effect=ValueError("invalid signature") if invalid else None,
                  return_value=payload)
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setattr(auth_mod, "_get_keycloak_config", lambda: (authority, "astral-frontend", ""))
    monkeypatch.setattr(jwks_cache, "get_jwks", cache)
    monkeypatch.setattr(auth_mod.jose_jwt, "decode", decode)
    request = types.SimpleNamespace(method="GET", query_params={}, state=types.SimpleNamespace())
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="synthetic-token")
    if invalid:
        with pytest.raises(HTTPException) as error:
            await auth_mod.get_current_user_payload(request, credentials)
        assert error.value.status_code == 401
        assert not hasattr(request.state, "audit_claims")
    else:
        assert await auth_mod.get_current_user_payload(request, credentials) == payload
        assert request.state.audit_claims == payload
    cache.assert_awaited_once_with(f"{authority}/protocol/openid-connect/certs", token="synthetic-token")
    decode.assert_called_once_with("synthetic-token", keys, algorithms=["RS256"],
                                   options={"verify_aud": False, "verify_at_hash": False})
