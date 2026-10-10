"""Tests proof-bound REST requests and encrypted device custody without live IAM.
These SDK checks complement the backend's real PostgreSQL enrollment and
ordinary-dispatch admission tests.
"""

from __future__ import annotations

import json

import httpx
import pytest

from astral_sdk import mesh

PASSWORD = "synthetic-device-password"


def test_encrypted_custody_round_trip_replacement_and_no_token_persistence(tmp_path):
    device = mesh.MeshDevice.generate()
    destination = tmp_path / "device.mesh"
    device.save(destination, PASSWORD)
    assert device.public_key["x"].encode() not in destination.read_bytes()
    loaded = mesh.MeshDevice.load(destination, PASSWORD)
    assert loaded.public_key == device.public_key
    with pytest.raises(FileExistsError):
        loaded.save(destination, PASSWORD)
    loaded.owner_id, loaded.member_id = "owner", "member"
    loaded.save(destination, PASSWORD, replace=True)
    restored = mesh.MeshDevice.load(destination, PASSWORD)
    assert (restored.owner_id, restored.member_id, restored.public_key) == (
        "owner",
        "member",
        device.public_key,
    )
    assert not list(tmp_path.glob(".mesh-custody-*"))
    assert "token" not in destination.read_text()


@pytest.mark.parametrize("password", [None, "", "short"])
def test_custody_password_rejected(tmp_path, password):
    with pytest.raises(ValueError):
        mesh.MeshDevice.generate().save(tmp_path / "device", password)
    with pytest.raises(ValueError):
        mesh.MeshDevice.load(tmp_path / "device", password)


def test_wrong_password_tampered_ciphertext_and_failed_replace_preserve_original(
    tmp_path, monkeypatch
):
    device, destination = mesh.MeshDevice.generate(), tmp_path / "device"
    device.save(destination, PASSWORD)
    original = destination.read_bytes()
    with pytest.raises(ValueError):
        mesh.MeshDevice.load(destination, PASSWORD + "wrong")
    envelope = json.loads(original)
    envelope["ciphertext"] = mesh._encode(b"tampered")
    destination.write_text(json.dumps(envelope))
    with pytest.raises(ValueError):
        mesh.MeshDevice.load(destination, PASSWORD)
    destination.write_bytes(original)

    def fail(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(mesh.os, "replace", fail)
    with pytest.raises(OSError):
        device.save(destination, PASSWORD, replace=True)
    assert destination.read_bytes() == original
    assert not list(tmp_path.glob(".mesh-custody-*"))


@pytest.mark.parametrize(
    "envelope",
    [
        {},
        {"version": 2, "salt": "", "nonce": "", "ciphertext": ""},
        {"version": 1, "salt": "", "nonce": "", "ciphertext": ""},
    ],
)
def test_malformed_custody_fails_closed(tmp_path, envelope):
    destination = tmp_path / "device"
    destination.write_text(json.dumps(envelope))
    with pytest.raises(ValueError):
        mesh.MeshDevice.load(destination, PASSWORD)
    destination.write_bytes(b"a" * 8193)
    with pytest.raises(ValueError):
        mesh.MeshDevice.load(destination, PASSWORD)


@pytest.mark.parametrize(
    "base",
    [
        "",
        "ftp://host",
        "http://remote.invalid",
        "https://u:p@host",
        "https://host/path",
        "https://host?q=token",
        "https://host#fragment",
    ],
)
def test_remote_origin_requires_https_without_credentials(base):
    with pytest.raises(ValueError):
        mesh.MeshClient(base, mesh.MeshDevice.generate())


@pytest.mark.parametrize(
    "payload", [None, "", "bad", mesh._encode(b"{}"), mesh._encode(b'{"v":2}')]
)
def test_malformed_enrollment_payload_fails_before_http(payload):
    with pytest.raises(ValueError):
        mesh.MeshDevice.generate().redemption(payload)


def test_redemption_and_request_proofs_bind_exact_http_nonce_token_and_key():
    device = mesh.MeshDevice.generate()
    challenge = b"synthetic-challenge-32-bytes!!!!!"[:32].ljust(32, b"!")
    payload = mesh._encode(
        mesh._json(
            {
                "v": 1,
                "o": "owner",
                "i": "invite",
                "t": "token",
                "c": mesh._encode(challenge),
            }
        )
    )
    signed = device.redemption(payload, agent_id="reader")
    device._private_key.public_key().verify(
        mesh._decode(signed["signature"]), challenge
    )
    proof = device.proof(
        method="POST",
        target="https://host/api/mesh/session",
        nonce="nonce",
        access_token="member-token",
    )
    header, claims, signature = proof.split(".")
    device._private_key.public_key().verify(
        mesh._decode(signature), (header + "." + claims).encode()
    )
    assert json.loads(mesh._decode(header))["jwk"] == device.public_key
    assert json.loads(mesh._decode(claims))["ath"] == mesh._encode(
        mesh.hashlib.sha256(b"member-token").digest()
    )


def test_client_protocol_redemption_refresh_invocation_and_close():
    device, requests = mesh.MeshDevice.generate(), []
    member = {
        "owner_id": "owner",
        "member_id": "member",
        "device_key": device.public_key,
    }

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/mesh/nonce":
            return httpx.Response(200, json={"nonce": "challenge.nonce"})
        if request.url.path in {"/api/mesh/enrollment/redeem", "/api/mesh/session"}:
            return httpx.Response(
                200,
                json={
                    "token_type": "DPoP",
                    "access_token": "proof-bound-token",
                    "member": member,
                },
            )
        return httpx.Response(200, json={"result": "ok"})

    transport = httpx.MockTransport(respond)
    http = httpx.Client(transport=transport)
    client = mesh.MeshClient("https://host", device, client=http)
    payload = mesh._encode(
        mesh._json(
            {
                "v": 1,
                "o": "owner",
                "i": "invite",
                "t": "token",
                "c": mesh._encode(b"c" * 32),
            }
        )
    )
    assert client.redeem(payload, agent_id="reader") == member
    assert client.session(agent_id="reader") == member
    assert client.invoke("reader", "read", {"id": 1}) == {"result": "ok"}
    final = requests[-1]
    assert final.headers["authorization"] == "DPoP proof-bound-token"
    assert final.headers["dpop-nonce"] == "challenge.nonce"
    assert json.loads(final.content) == {"arguments": {"id": 1}}
    claims = json.loads(mesh._decode(final.headers["dpop"].split(".")[1]))
    assert (
        claims["htm"] == "POST"
        and claims["htu"] == "https://host/api/mesh/tools/reader/read"
    )
    client.close()
    assert client._token is None and not http.is_closed
    http.close()
    owned = mesh.MeshClient("http://127.0.0.1:8001", device)
    owned.close()
    assert owned.client.is_closed


def test_owner_invitation_and_confirmation_use_existing_http_iam_client():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"result": "ok"})

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        headers={"Authorization": "Bearer owner"},
    ) as http:
        client = mesh.MeshClient(
            "https://host", mesh.MeshDevice.generate(), client=http
        )
        assert client.invitation(label="Device", scopes=["tools:read"]) == {
            "result": "ok"
        }
        assert json.loads(requests[0].content)["device_key"] == client.device.public_key
        assert client.confirm("abc123") == {"result": "ok"}
        assert requests[-1].headers["authorization"] == "Bearer owner"
        with pytest.raises(ValueError):
            client.confirm("../foreign")


def test_missing_member_session_invalid_paths_tools_and_unbound_server_response():
    client = mesh.MeshClient("https://host", mesh.MeshDevice.generate())
    try:
        with pytest.raises(ValueError):
            client.nonce()
        with pytest.raises(ValueError):
            client.request("GET", "/api/mesh/me")
        client._token = "synthetic"
        for path in (
            "/foreign",
            "/api/mesh/me?token=x",
            "/api/mesh/me#fragment",
            "/api/mesh/\\",
        ):
            with pytest.raises(ValueError):
                client.request("GET", path)
        for agent, tool, arguments in (
            ("", "read", {}),
            ("reader", "../tool", {}),
            ("reader", "read", []),
        ):
            with pytest.raises(ValueError):
                client.invoke(agent, tool, arguments)
        with pytest.raises(ValueError):
            client._remember({"token_type": "Bearer", "access_token": "unbound"})
        with pytest.raises(ValueError):
            client._remember(
                {
                    "token_type": "DPoP",
                    "access_token": "bound",
                    "member": {"device_key": {}},
                }
            )
    finally:
        client.close()


def test_http_failure_is_not_reported_as_success():
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(403))
    ) as http:
        client = mesh.MeshClient(
            "https://host", mesh.MeshDevice.generate(), client=http
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.invitation(label="device", scopes=[])


def test_cli_key_and_enrollment_never_print_credentials(tmp_path, monkeypatch, capsys):
    destination = tmp_path / "device"
    monkeypatch.setattr(mesh.getpass, "getpass", lambda prompt: PASSWORD)
    assert mesh.main(["--custody", str(destination), "key-init"]) == 0
    public = json.loads(capsys.readouterr().out)
    assert public["kty"] == "OKP"
    loaded = mesh.MeshDevice.load(destination, PASSWORD)
    calls = []

    class Client:
        def __init__(self, base, device):
            self.device = device

        def redeem(self, payload, **kwargs):
            self.device.owner_id, self.device.member_id = "owner", "member"
            return {"member_id": "member"}

        def session(self, **kwargs):
            calls.append("session")

        def invoke(self, *args):
            calls.append(args)
            return {"result": "ok"}

        def close(self):
            calls.append("close")

    monkeypatch.setattr(mesh, "MeshClient", Client)
    assert (
        mesh.main(
            [
                "--custody",
                str(destination),
                "--server",
                "https://host",
                "enroll",
                "--agent",
                "reader",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == '{"member_id": "member"}'
    assert mesh.MeshDevice.load(destination, PASSWORD).public_key == loaded.public_key
    assert (
        mesh.main(
            [
                "--custody",
                str(destination),
                "--server",
                "https://host",
                "tool",
                "reader",
                "read",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == '{"result": "ok"}'
    assert calls[-3:] == ["session", ("reader", "read", {}), "close"]
    with pytest.raises(SystemExit):
        mesh.main(["--custody", str(destination), "tool", "reader", "read"])
