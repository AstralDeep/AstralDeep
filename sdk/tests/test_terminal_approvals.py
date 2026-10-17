"""Checks terminal clients observe human approval without replaying terminal writes.
Controlled HTTP responses exercise synchronous, asynchronous and CLI consumer behavior.
"""

import asyncio
import json

import httpx
import pytest

from astral_sdk import AstralClient, AsyncAstralClient
from astral_sdk.errors import AstralHTTPError

PID = "a" * 32


def transport(outcome):
    requests = []

    def handle(request):
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(202, json={"proposal_id": PID, "status": "pending"})
        return httpx.Response(200, json={"proposal_id": PID, **outcome})

    return httpx.MockTransport(handle), requests


@pytest.mark.parametrize("action", ["create", "write"])
def test_sync_terminal_waits_for_human_result_without_replaying_post(action):
    network, requests = transport({"status": "succeeded", "session": {"session_id": "approved"}})
    with AstralClient(base_url="https://terminal.invalid", token="owner", transport=network) as client:
        result = (client.create_terminal_session() if action == "create"
                  else client.write_terminal("existing", "exact bytes"))
    assert result.session_id == "approved"
    assert requests == [("POST", "/api/terminals" + ("" if action == "create" else "/existing/input")),
                        ("GET", "/api/terminals/approvals/" + PID)]


@pytest.mark.parametrize("action", ["create", "write"])
def test_async_terminal_waits_for_human_result_without_replaying_post(action):
    network, requests = transport({"status": "succeeded", "session": {"session_id": "approved"}})

    async def run():
        async with AsyncAstralClient(base_url="https://terminal.invalid", token="owner", transport=network) as client:
            return (await client.create_terminal_session() if action == "create"
                    else await client.write_terminal("existing", b"exact bytes"))

    assert asyncio.run(run()).session_id == "approved"
    assert [method for method, _ in requests] == ["POST", "GET"]


@pytest.mark.parametrize("status,code", [("declined", "terminal_approval_declined"),
    ("failed", "terminal_dispatch_refused"), ("unknown", "terminal_invalid_approval")])
def test_declined_failed_or_malformed_approval_never_retries_effect(status, code):
    network, requests = transport({"status": status})
    with AstralClient(base_url="https://terminal.invalid", token="owner", transport=network) as client:
        with pytest.raises(AstralHTTPError) as refused:
            client.create_terminal_session()
    assert refused.value.code == code
    assert [method for method, _ in requests].count("POST") == 1


def test_zero_wait_returns_recoverable_request_id_without_cancelling_or_replaying():
    network, requests = transport({"status": "pending"})
    with AstralClient(base_url="https://terminal.invalid", token="owner", transport=network) as client:
        with pytest.raises(AstralHTTPError) as pending:
            client.create_terminal_session(approval_timeout_seconds=0)
        assert pending.value.code == "terminal_confirmation_required"
        assert pending.value.data == {"proposal_id": PID}
        assert client.get_terminal_approval(PID)["status"] == "pending"
    assert [method for method, _ in requests] == ["POST", "GET"]


@pytest.mark.parametrize("wait", [-1, True, float("nan"), float("inf"), "120"])
def test_invalid_approval_wait_is_refused_before_any_terminal_request(wait):
    network, requests = transport({"status": "pending"})
    with AstralClient(base_url="https://terminal.invalid", token="owner", transport=network) as client:
        with pytest.raises(ValueError):
            client.create_terminal_session(approval_timeout_seconds=wait)
    assert requests == []


@pytest.mark.parametrize("asynchronous", [True, False])
def test_pending_and_running_polls_do_not_replay_terminal_effect(asynchronous):
    requests = []
    states = iter(["pending", "running", "succeeded"])

    def handle(request):
        requests.append(request.method)
        state = "pending" if request.method == "POST" else next(states)
        return httpx.Response(202 if request.method == "POST" else 200,
            json={"proposal_id": PID, "status": state, "session": {"session_id": "approved"}})

    network = httpx.MockTransport(handle)

    async def run():
        async with AsyncAstralClient(base_url="https://terminal.invalid", token="owner", transport=network) as client:
            return await client.create_terminal_session()

    if asynchronous:
        result = asyncio.run(run())
    else:
        with AstralClient(base_url="https://terminal.invalid", token="owner", transport=network) as client:
            result = client.create_terminal_session()
    assert result.session_id == "approved" and requests == ["POST", "GET", "GET", "GET"]


def test_async_zero_wait_keeps_proposal_recoverable_and_rejects_bad_status_id():
    network, requests = transport({"status": "pending"})

    async def run():
        async with AsyncAstralClient(base_url="https://terminal.invalid", token="owner", transport=network) as client:
            with pytest.raises(AstralHTTPError, match="Approve this request") as pending:
                await client.create_terminal_session(approval_timeout_seconds=0)
            assert pending.value.data == {"proposal_id": PID}
            with pytest.raises(ValueError):
                await client.get_terminal_approval("../../other")

    asyncio.run(run())
    assert requests == [("POST", "/api/terminals")]


def test_malformed_proposal_and_status_ids_do_not_become_paths():
    network = httpx.MockTransport(lambda _request: httpx.Response(202, json={"status": "pending"}))
    with AstralClient(base_url="https://terminal.invalid", token="owner", transport=network) as client:
        with pytest.raises(AstralHTTPError) as refused:
            client.create_terminal_session()
        assert refused.value.code == "terminal_invalid_approval"
        with pytest.raises(ValueError):
            client.get_terminal_approval("../../other")


@pytest.mark.parametrize("action", ["terminal-create", "terminal-write"])
def test_cli_wait_timeout_prints_original_proposal_and_never_replays_write(action, monkeypatch, capsys):
    from astral_sdk import __main__ as cli

    network, requests = transport({"status": "pending"})
    client = AstralClient(base_url="https://terminal.invalid", token="owner", transport=network)
    monkeypatch.setattr(cli, "_client", lambda _args: client)
    arguments = [action] + (["existing", "--data", "input"] if action == "terminal-write" else [])
    assert cli.run(arguments + ["--approval-timeout", "0"]) == 1
    output = json.loads(capsys.readouterr().err)
    assert output["code"] == "terminal_confirmation_required" and output["proposal_id"] == PID
    assert [method for method, _ in requests] == ["POST"]


def test_cli_exposes_approval_wait_and_status(monkeypatch, capsys):
    from astral_sdk import __main__ as cli

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def close(self):
            pass

        def get_terminal_approval(self, proposal_id):
            assert proposal_id == PID
            return {"proposal_id": PID, "status": "pending"}

    monkeypatch.setattr(cli, "AstralClient", Client)
    assert cli.run(["terminal-approval", PID, "--token", "owner", "--base-url", "https://terminal.invalid"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pending"


def test_cli_bad_wait_is_a_json_refusal_before_any_request(monkeypatch, capsys):
    from astral_sdk import __main__ as cli

    network, requests = transport({"status": "pending"})
    client = AstralClient(base_url="https://terminal.invalid", token="owner", transport=network)
    monkeypatch.setattr(cli, "_client", lambda _args: client)
    assert cli.run(["terminal-create", "--approval-timeout", "-1"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "invalid_arguments"
    assert requests == []
