"""Verify owner emergency REST transport, framework denial, and bounded failures.
These wire tests complement backend current-authority and real Plane tests.
"""

import json

import httpx
import pytest

from astral_sdk import AstralClient, AsyncAstralClient
from astral_sdk.errors import AstralAuthError, AstralConflictError, AstralHTTPError, AstralTimeoutError


def test_current_owner_transport_round_trip(fake_server):
    with AstralClient(fake_server.base_url, fake_server.state.owner_token) as client:
        assert not client.emergency_status()["engaged"]
        first = client.emergency_stop(reason="drill")
        assert first["engaged"]
        with pytest.raises(AstralConflictError):
            client.emergency_resume(expected_revision=first["revision"] + 1)
        assert not client.emergency_resume(expected_revision=first["revision"])["engaged"]


@pytest.mark.asyncio
async def test_async_current_owner_transport_round_trip(fake_server):
    async with AsyncAstralClient(fake_server.base_url, fake_server.state.owner_token) as client:
        assert not (await client.emergency_status())["engaged"]
        stopped = await client.emergency_stop()
        assert not (await client.emergency_resume(expected_revision=stopped["revision"]))["engaged"]
    async with AsyncAstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        await client.emergency_stop(reason="framework conservative stop")
        assert (await client.emergency_status())["engaged"]
        with pytest.raises(AstralAuthError):
            await client.emergency_resume(expected_revision=1)


@pytest.mark.parametrize("status,body,error", [
    (401, {"detail": "invalid_token"}, AstralAuthError),
    (403, {"error": "denied"}, AstralAuthError),
    (409, {"detail": "stale"}, AstralConflictError),
    (503, {"detail": {"reason": "unavailable"}}, AstralHTTPError),
    (503, [], AstralHTTPError), (200, {}, AstralHTTPError),
    (200, {"engaged": 1}, AstralHTTPError), (200, "invalid-json", AstralHTTPError),
])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_owner_wire_failures_are_not_retried(status, body, error, asynchronous):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.path == "/api/emergency-stop/resume"
        assert request.headers["authorization"] == "Bearer current-owner-token"
        assert json.loads(request.content) == {"expected_revision": 7}
        return httpx.Response(status, text=body) if isinstance(body, str) else httpx.Response(status, json=body)

    transport = httpx.MockTransport(respond)
    if asynchronous:
        async with AsyncAstralClient("https://host.invalid", "current-owner-token", transport=transport) as client:
            with pytest.raises(error):
                await client.emergency_resume(expected_revision=7)
    else:
        with AstralClient("https://host.invalid", "current-owner-token", transport=transport) as client:
            with pytest.raises(error):
                client.emergency_resume(expected_revision=7)
    assert len(calls) == 1


@pytest.mark.parametrize("fault,error", [(httpx.ReadTimeout, AstralTimeoutError),
                                       (httpx.ConnectError, AstralHTTPError)])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_owner_transport_failure_preserves_uncertainty(fault, error, asynchronous):
    calls = []

    def respond(request):
        calls.append(request)
        raise fault("synthetic transport failure", request=request)

    transport = httpx.MockTransport(respond)
    if asynchronous:
        async with AsyncAstralClient("https://host.invalid", "owner", transport=transport) as client:
            with pytest.raises(error):
                await client.emergency_stop()
    else:
        with AstralClient("https://host.invalid", "owner", transport=transport) as client:
            with pytest.raises(error):
                client.emergency_stop()
    assert len(calls) == 1


def test_owner_cli_dispatches_explicit_revision_to_current_owner_transport(fake_server, capsys):
    from astral_sdk.__main__ import run

    with AstralClient(fake_server.base_url, fake_server.state.owner_token) as client:
        first = client.emergency_stop()
    assert run(["emergency-resume", "--base-url", fake_server.base_url,
                "--token", fake_server.state.owner_token, "--expected-revision", str(first["revision"])]) == 0
    assert not json.loads(capsys.readouterr().out)["engaged"]
