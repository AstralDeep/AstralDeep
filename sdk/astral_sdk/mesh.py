"""Enrolls proof-bound mesh devices through the ordinary Astral REST dispatcher.
Device signing keys stay in memory or password-encrypted local custody; owner IAM
credentials and delegated tokens are never written by this client.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import httpx


def _encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


class MeshDevice:
    def __init__(self, private_key, *, owner_id=None, member_id=None):
        self._private_key = private_key
        self.owner_id = owner_id
        self.member_id = member_id

    @classmethod
    def generate(cls):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return cls(Ed25519PrivateKey.generate())

    @property
    def public_key(self):
        from cryptography.hazmat.primitives import serialization

        raw = self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return {"kty": "OKP", "crv": "Ed25519", "x": _encode(raw)}

    def redemption(self, payload, *, agent_id=None):
        try:
            fields = json.loads(_decode(payload))
            if set(fields) != {"v", "o", "i", "t", "c"} or fields["v"] != 1:
                raise ValueError
            challenge = _decode(fields["c"])
            if len(challenge) != 32 or any(
                not isinstance(fields[key], str) or not fields[key]
                for key in ("o", "i", "t", "c")
            ):
                raise ValueError
        except Exception as exc:
            raise ValueError("invalid enrollment payload") from exc
        return {
            "payload": payload,
            "signature": _encode(self._private_key.sign(challenge)),
            "agent_id": agent_id,
        }

    def proof(self, *, method, target, nonce, access_token=None):
        payload = {
            "jti": uuid.uuid4().hex,
            "htm": method,
            "htu": target,
            "iat": int(time.time()),
            "nonce": nonce,
        }
        if access_token is not None:
            payload["ath"] = _encode(hashlib.sha256(access_token.encode()).digest())
        header = {"typ": "dpop+jwt", "alg": "EdDSA", "jwk": self.public_key}
        signed = _encode(_json(header)) + "." + _encode(_json(payload))
        return signed + "." + _encode(self._private_key.sign(signed.encode()))

    def save(self, path, password, *, replace=False):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        if not isinstance(password, str) or len(password) < 12:
            raise ValueError("custody password requires at least 12 characters")
        destination = Path(path)
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        key = PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000
        ).derive(password.encode())
        raw = self._private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        identity = {
            "key": _encode(raw),
            "owner_id": self.owner_id,
            "member_id": self.member_id,
        }
        envelope = {
            "version": 1,
            "salt": _encode(salt),
            "nonce": _encode(nonce),
            "ciphertext": _encode(
                AESGCM(key).encrypt(nonce, _json(identity), b"astral-mesh-device-v1")
            ),
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=".mesh-custody-", dir=destination.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(_json(envelope))
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, destination)
            else:
                os.link(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def load(cls, path, password):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        try:
            if not isinstance(password, str) or len(password) < 12:
                raise ValueError
            raw = Path(path).read_bytes()
            if len(raw) > 8192:
                raise ValueError
            envelope = json.loads(raw)
            if (
                set(envelope) != {"version", "salt", "nonce", "ciphertext"}
                or envelope["version"] != 1
            ):
                raise ValueError
            salt, nonce = _decode(envelope["salt"]), _decode(envelope["nonce"])
            if len(salt) != 16 or len(nonce) != 12:
                raise ValueError
            key = PBKDF2HMAC(
                algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000
            ).derive(password.encode())
            identity = json.loads(
                AESGCM(key).decrypt(
                    nonce, _decode(envelope["ciphertext"]), b"astral-mesh-device-v1"
                )
            )
            if set(identity) != {"key", "owner_id", "member_id"} or any(
                identity[name] is not None
                and (not isinstance(identity[name], str) or not identity[name])
                for name in ("owner_id", "member_id")
            ):
                raise ValueError
            return cls(
                Ed25519PrivateKey.from_private_bytes(_decode(identity["key"])),
                owner_id=identity["owner_id"],
                member_id=identity["member_id"],
            )
        except Exception as exc:
            raise ValueError("device custody could not be decrypted") from exc


class MeshClient:
    def __init__(self, base_url, device, *, client=None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme != "https"
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError("mesh server requires HTTPS or explicit loopback HTTP")
        self.base_url = base_url.rstrip("/")
        self.device = device
        self._owned = client is None
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self._token = None

    def close(self):
        self._token = None
        if self._owned:
            self.client.close()

    def _request(self, method, path, **kwargs):
        response = self.client.request(method, self.base_url + path, **kwargs)
        response.raise_for_status()
        return response.json()

    def invitation(self, *, label, scopes, ttl_seconds=300):
        return self._request(
            "POST",
            "/api/mesh/invitations",
            json={
                "label": label,
                "device_key": self.device.public_key,
                "scopes": scopes,
                "ttl_seconds": ttl_seconds,
            },
        )

    def confirm(self, invite_id):
        if not isinstance(invite_id, str) or not invite_id.isalnum():
            raise ValueError("invalid invitation identifier")
        return self._request("POST", f"/api/mesh/invitations/{invite_id}/confirm")

    def redeem(self, payload, *, agent_id=None):
        response = self._request(
            "POST",
            "/api/mesh/enrollment/redeem",
            json=self.device.redemption(payload, agent_id=agent_id),
        )
        self._remember(response)
        return response["member"]

    def _remember(self, response):
        if response.get("token_type") != "DPoP" or not isinstance(
            response.get("access_token"), str
        ):
            raise ValueError("server returned an unbound member session")
        member = response["member"]
        if member.get("device_key") != self.device.public_key:
            raise ValueError("server returned a foreign device")
        self.device.owner_id, self.device.member_id = (
            member["owner_id"],
            member["member_id"],
        )
        self._token = response["access_token"]

    def nonce(self):
        if not self.device.owner_id or not self.device.member_id:
            raise ValueError("device is not enrolled")
        response = self._request(
            "POST",
            "/api/mesh/nonce",
            json={"owner_id": self.device.owner_id, "member_id": self.device.member_id},
        )
        return response["nonce"]

    def session(self, *, agent_id=None):
        nonce, path = self.nonce(), "/api/mesh/session"
        response = self._request(
            "POST",
            path,
            json={
                "owner_id": self.device.owner_id,
                "member_id": self.device.member_id,
                "agent_id": agent_id,
                "nonce": nonce,
                "proof": self.device.proof(
                    method="POST", target=self.base_url + path, nonce=nonce
                ),
            },
        )
        self._remember(response)
        return response["member"]

    def request(self, method, path, *, body=None):
        if (
            not self._token
            or not path.startswith("/api/mesh/")
            or "?" in path
            or "#" in path
            or "\\" in path
        ):
            raise ValueError("current member session and mesh path required")
        nonce = self.nonce()
        proof = self.device.proof(
            method=method,
            target=self.base_url + path,
            nonce=nonce,
            access_token=self._token,
        )
        return self._request(
            method,
            path,
            headers={
                "Authorization": "DPoP " + self._token,
                "DPoP": proof,
                "DPoP-Nonce": nonce,
            },
            **({"json": body} if body is not None else {}),
        )

    def invoke(self, agent_id, tool_name, arguments):
        if any(
            not isinstance(value, str)
            or not value
            or any(character in value for character in "/?#\\")
            for value in (agent_id, tool_name)
        ) or not isinstance(arguments, dict):
            raise ValueError("agent, tool and arguments are malformed")
        return self.request(
            "POST",
            f"/api/mesh/tools/{agent_id}/{tool_name}",
            body={"arguments": arguments},
        )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m astral_sdk.mesh")
    parser.add_argument("--custody", required=True)
    parser.add_argument("--server")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("key-init")
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--agent")
    invoke = commands.add_parser("tool")
    invoke.add_argument("agent")
    invoke.add_argument("tool")
    invoke.add_argument("--arguments", default="{}")
    args = parser.parse_args(argv)
    password = getpass.getpass("Device custody password: ")
    if args.command == "key-init":
        device = MeshDevice.generate()
        device.save(args.custody, password)
        print(json.dumps(device.public_key))
        return 0
    if not args.server:
        parser.error("--server is required")
    device = MeshDevice.load(args.custody, password)
    client = MeshClient(args.server, device)
    try:
        if args.command == "enroll":
            member = client.redeem(
                getpass.getpass("Enrollment payload: "), agent_id=args.agent
            )
            device.save(args.custody, password, replace=True)
            print(json.dumps(member))
        else:
            client.session(agent_id=args.agent)
            print(
                json.dumps(
                    client.invoke(args.agent, args.tool, json.loads(args.arguments))
                )
            )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
