"""Checks configured enrollment links and Projection's joining page security headers.
No device grants or signing credentials are placed in durable URLs or responses.
"""

import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator.mesh_links import MeshLinkUnavailable, enrollment_link
from orchestrator.mesh_web import mesh_web_router


@pytest.mark.parametrize("base", ["https://mesh.example", "https://mesh.example:8443/"])
def test_valid_link_uses_only_fragment(base, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", base)
    assert enrollment_link("abc_123-xyz") == base.rstrip("/") + "/enroll#abc_123-xyz"


@pytest.mark.parametrize(
    "base",
    [
        "",
        "http://mesh.example",
        "https://user:secret@mesh.example",
        "https://mesh.example/path",
        "https://mesh.example?query",
        "https://mesh.example#fragment",
        " https://mesh.example",
        "https://mesh.example:bad",
        "x" * 2049,
    ],
)
def test_invalid_configuration_fails_closed(base, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", base)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    with pytest.raises(MeshLinkUnavailable) as error:
        enrollment_link("abc")
    assert error.value.code == "mesh_link_unavailable"
    assert "secret" not in str(error.value)


def test_development_http_and_fallback_base(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("BACKEND_PUBLIC_URL", "http://127.0.0.1:8001")
    assert enrollment_link("abc") == "http://127.0.0.1:8001/enroll#abc"


@pytest.mark.parametrize("payload", [None, "", "x?secret", "x" * 2049])
def test_unbounded_or_query_payload_is_refused(payload, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mesh.example")
    with pytest.raises(MeshLinkUnavailable):
        enrollment_link(payload)


@pytest.mark.asyncio
async def test_joining_route_nonce_no_store_and_fragment_scrubbing():
    app = FastAPI()
    app.include_router(mesh_web_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://mesh.example"
    ) as client:
        first = await client.get("/enroll")
        second = await client.get("/enroll")
    assert first.status_code == second.status_code == 200
    nonce = re.search('nonce="([^"]+)"', first.text).group(1)
    assert nonce not in second.text
    assert "'nonce-" + nonce + "'" in first.headers["content-security-policy"]
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in first.headers["content-security-policy"]
    assert 'history.replaceState(null, "", "/enroll")' in first.text
    assert "%%ASTRAL_" not in first.text
    assert "mesh-custody.js?v=" in first.text


@pytest.mark.asyncio
async def test_joining_route_missing_component_fails_closed(monkeypatch):
    from orchestrator import mesh_web

    monkeypatch.setattr(
        mesh_web, "template_path", lambda _: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert (await mesh_web.enrollment_page()).status_code == 503


def test_public_identity_rejects_private_and_noncanonical_keys():
    from orchestrator.mesh_enrollment import normalize_device_key
    import base64

    x = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    valid = {"kty": "OKP", "crv": "Ed25519", "x": x}
    assert normalize_device_key(valid) == valid
    with pytest.raises(ValueError):
        normalize_device_key({**valid, "d": "private"})
    with pytest.raises(ValueError):
        normalize_device_key({**valid, "x": x + "="})
