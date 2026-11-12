"""Covers shared/jwks_cache.py when the issuer is unreachable: last good keys serve for a bounded grace, retries are paced, and malformed or failing responses are never cached."""

from __future__ import annotations

import types

import pytest

from shared import jwks_cache

URL = "https://idp.example/realms/a/protocol/openid-connect/certs"
GOOD = {"keys": [{"kid": "k1", "kty": "RSA"}]}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("JWKS_STALE_GRACE_SECONDS", raising=False)
    jwks_cache.clear()
    yield
    jwks_cache.clear()


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1_000_000.0}
    monkeypatch.setattr(jwks_cache, "time", types.SimpleNamespace(time=lambda: state["now"]))
    return state


@pytest.fixture
def issuer(monkeypatch, clock):
    state = {"up": True, "calls": 0}

    async def fetch(url):
        state["calls"] += 1
        if not state["up"]:
            raise OSError("unreachable")
        jwks_cache._cache[url] = {"jwks": GOOD, "fetched_at": clock["now"]}
        return GOOD

    monkeypatch.setattr(jwks_cache, "_fetch", fetch)
    return state


async def test_last_good_keys_serve_after_the_ttl_while_the_issuer_is_down(issuer, clock):
    await jwks_cache.get_jwks(URL)
    issuer["up"] = False
    clock["now"] += jwks_cache._TTL_SECONDS + 5
    assert await jwks_cache.get_jwks(URL) == GOOD


async def test_failed_refetches_are_paced(issuer, clock):
    await jwks_cache.get_jwks(URL)
    issuer["up"] = False
    clock["now"] += jwks_cache._TTL_SECONDS + 5
    await jwks_cache.get_jwks(URL)
    calls = issuer["calls"]
    await jwks_cache.get_jwks(URL)
    assert issuer["calls"] == calls
    clock["now"] += jwks_cache._RETRY_AFTER_FAILURE_SECONDS + 1
    await jwks_cache.get_jwks(URL)
    assert issuer["calls"] == calls + 1


async def test_recovery_replaces_the_stale_keys(issuer, clock):
    await jwks_cache.get_jwks(URL)
    issuer["up"] = False
    clock["now"] += jwks_cache._TTL_SECONDS + 5
    await jwks_cache.get_jwks(URL)
    issuer["up"] = True
    clock["now"] += jwks_cache._RETRY_AFTER_FAILURE_SECONDS + 1
    await jwks_cache.get_jwks(URL)
    assert "retry_at" not in jwks_cache._cache[URL]


async def test_grace_is_bounded(issuer, clock):
    await jwks_cache.get_jwks(URL)
    issuer["up"] = False
    clock["now"] += jwks_cache._TTL_SECONDS + jwks_cache._DEFAULT_GRACE_SECONDS + 1
    with pytest.raises(OSError):
        await jwks_cache.get_jwks(URL)


async def test_grace_is_configurable_and_can_be_switched_off(issuer, clock, monkeypatch):
    await jwks_cache.get_jwks(URL)
    issuer["up"] = False
    clock["now"] += jwks_cache._TTL_SECONDS + 5
    monkeypatch.setenv("JWKS_STALE_GRACE_SECONDS", "0")
    with pytest.raises(OSError):
        await jwks_cache.get_jwks(URL)
    monkeypatch.setenv("JWKS_STALE_GRACE_SECONDS", "3600")
    assert await jwks_cache.get_jwks(URL) == GOOD
    monkeypatch.setenv("JWKS_STALE_GRACE_SECONDS", "not-a-number")
    assert jwks_cache._grace_seconds() == jwks_cache._DEFAULT_GRACE_SECONDS


async def test_a_first_fetch_with_no_cache_still_fails(issuer):
    issuer["up"] = False
    with pytest.raises(OSError):
        await jwks_cache.get_jwks(URL)


async def test_unknown_kid_with_the_issuer_down_serves_the_cached_set(issuer, clock):
    import base64
    import json

    await jwks_cache.get_jwks(URL)
    issuer["up"] = False
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": "rotated"}).encode()).rstrip(b"=").decode()
    assert await jwks_cache.get_jwks(URL, token=f"{header}.e30.sig") == GOOD


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def raise_for_status(self):
        if self.status >= 400:
            raise OSError(f"status {self.status}")

    async def json(self):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def fake_session(response, seen):
    class Session:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url):
            return response

    return Session


async def test_fetch_stores_a_good_key_set_with_a_timeout(monkeypatch):
    seen = {}
    monkeypatch.setattr(jwks_cache.aiohttp, "ClientSession", fake_session(FakeResponse(200, GOOD), seen))
    assert await jwks_cache._fetch(URL) == GOOD
    assert seen["timeout"].total == jwks_cache._FETCH_TIMEOUT_SECONDS
    assert jwks_cache._cache[URL]["jwks"] == GOOD


@pytest.mark.parametrize("response", [FakeResponse(503, {"error": "down"}), FakeResponse(200, {"error": "x"}), FakeResponse(200, []), FakeResponse(200, {"keys": "no"})])
async def test_bad_responses_are_never_cached(monkeypatch, response):
    monkeypatch.setattr(jwks_cache.aiohttp, "ClientSession", fake_session(response, {}))
    with pytest.raises((OSError, ValueError)):
        await jwks_cache._fetch(URL)
    assert URL not in jwks_cache._cache
