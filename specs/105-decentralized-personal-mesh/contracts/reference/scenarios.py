"""Builders that assemble deterministic meshes, tokens, headers and envelopes for the vector generator.
Everything here derives from the fixed seeds in astral_mesh_ref.testkit and is not used by the verification paths.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from astral_mesh_ref import attestation, crypto, event, keys, roster
from astral_mesh_ref.cj import b64url_encode, canonical
from astral_mesh_ref.testkit import DetRng, TestDevice, hlc, scalar, seed

BASE_MS = 1_794_441_600_000
BASE_S = BASE_MS // 1000
ISSUER = "https://auth.example.test/realms/astral"
AUDIENCE = "astral-app"
RSA_P = int(
    "ecb3ef1654bf1e3cd8b6655aa429de671a3861f219acbaa7c60b87b8582aed53aee41b562586b485b507b7cc36ff1542fe5731ec43a0ce2dda352c2a962881d7"
    "e3ccd6779b56a568ce860a3bdce9e2801f16cd24a741266ab8e416ecf8135a4af274bae57d0dcb4a26e68a25953f68ca0cc36d01901d403d13c0e2b610c5f3c1",
    16,
)
RSA_Q = int(
    "e1638bc88dc10e0da3d4f8b99ddb69bb392fa2a861b4a5aa9768b476ae573f814be0afb1c1b5dafb39458dfee404c1b33456eeb44711661e8d73fd39a8e1bd35a"
    "91bdbee9d49a650ccb52e58ecd580b00f6a66ce4d9f8d56d708e124f8459350462137345a0d35ebdecb22cf1d9a9ad92912d90a67dd516b250759046f241f05",
    16,
)


def rsa_key() -> rsa.RSAPrivateKey:
    e = 65537
    d = pow(e, -1, (RSA_P - 1) * (RSA_Q - 1))
    numbers = rsa.RSAPrivateNumbers(
        RSA_P,
        RSA_Q,
        d,
        rsa.rsa_crt_dmp1(d, RSA_P),
        rsa.rsa_crt_dmq1(d, RSA_Q),
        rsa.rsa_crt_iqmp(RSA_P, RSA_Q),
        rsa.RSAPublicNumbers(e, RSA_P * RSA_Q),
    )
    return numbers.private_key()


def _b64int(value: int) -> str:
    return b64url_encode(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def issuer_record(iss: str = ISSUER, aud: str = AUDIENCE) -> dict[str, Any]:
    scalar_value = scalar("issuer/es256")
    point = crypto.p256_public(scalar_value)
    numbers = rsa_key().public_key().public_numbers()
    return {
        "aud": aud,
        "iss": iss,
        "keys": [
            {
                "crv": "P-256",
                "kid": "es-1",
                "kty": "EC",
                "x": b64url_encode(point[1:33]),
                "y": b64url_encode(point[33:]),
            },
            {
                "e": _b64int(numbers.e),
                "kid": "rs-1",
                "kty": "RSA",
                "n": _b64int(numbers.n),
            },
        ],
    }


def make_jwt(alg: str, kid: str, claims: dict[str, Any]) -> str:
    head = b64url_encode(
        json.dumps(
            {"alg": alg, "kid": kid, "typ": "JWT"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    body = b64url_encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    )
    signing_input = (head + "." + body).encode()
    if alg == "ES256":
        sig = crypto.p256_sign(scalar("issuer/es256"), signing_input)
    elif alg == "RS256":
        sig = rsa_key().sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    else:
        sig = b"\x00" * 64
    return head + "." + body + "." + b64url_encode(sig)


def id_token(
    device: TestDevice,
    mesh: str,
    *,
    alg: str = "ES256",
    sub: str = "user-1001",
    nonce: str | None = None,
    iat: int = BASE_S,
    aud: str = AUDIENCE,
    iss: str = ISSUER,
    kid: str | None = None,
) -> str:
    claims = {
        "aud": aud,
        "exp": iat + 300,
        "iat": iat,
        "iss": iss,
        "nonce": nonce
        if nonce is not None
        else attestation.nonce_for(device.identity_public, mesh),
        "sub": sub,
    }
    return make_jwt(alg, kid or ("es-1" if alg == "ES256" else "rs-1"), claims)


def identity_attestation(
    device: TestDevice, mesh: str, **kwargs: Any
) -> dict[str, Any]:
    token = id_token(device, mesh, **kwargs)
    return attestation.build_identity(
        device.identity_scalar,
        device.identity_public,
        mesh,
        kwargs.get("iss", ISSUER),
        kwargs.get("sub", "user-1001"),
        token,
    )


class Mesh:
    def __init__(
        self, label: str, first: TestDevice, recovery_secret: bytes | None = None
    ) -> None:
        self.label = label
        self.rng = DetRng("mesh/" + label)
        self.recovery_secret = recovery_secret or seed("recovery/" + label)
        self.recovery = keys.derive_recovery(self.recovery_secret)
        self.devices = {first.name: first}
        self.entries: list[dict[str, Any]] = []
        self.epoch_key = seed("epoch/personal/" + label)
        self.tick = 0
        body = {
            "device": first.record(None),
            "epoch": keys.make_epoch_body(
                "personal",
                self.epoch_key,
                {first.id: first.wrap_public},
                self.recovery["wrap_public"],
                self.rng,
            ),
            "issuers": [issuer_record()],
            "recovery": {
                "sign": b64url_encode(self.recovery["sign_public"]),
                "wrap": b64url_encode(self.recovery["wrap_public"]),
            },
        }
        self.genesis = roster.build_entry(
            "genesis",
            None,
            [],
            self.clock(),
            first.id,
            body,
            identity_scalar=first.identity_scalar,
            presence_scalar=first.presence_scalar,
        )
        self.mesh = roster.entry_id(self.genesis)
        self.entries.append(self.genesis)

    def clock(self) -> dict[str, int]:
        self.tick += 1
        return hlc(BASE_MS + self.tick * 1000)

    def put(self, entry: dict[str, Any]) -> str:
        self.entries.append(entry)
        return roster.entry_id(entry)

    def entry(
        self,
        type: str,
        author: TestDevice | str,
        follows: list[str],
        body: dict[str, Any],
        *,
        presence: bool | None = None,
    ) -> dict[str, Any]:
        clock = self.clock()
        if author == "recovery":
            return roster.build_entry(
                type,
                self.mesh,
                follows,
                clock,
                "recovery",
                body,
                recovery_seed=self.recovery["sign_seed"],
            )
        assert isinstance(author, TestDevice)
        with_presence = (
            (type in roster.PRESENCE_REQUIRED) if presence is None else presence
        )
        return roster.build_entry(
            type,
            self.mesh,
            follows,
            clock,
            author.id,
            body,
            identity_scalar=author.identity_scalar,
            presence_scalar=author.presence_scalar if with_presence else None,
        )

    def add(
        self,
        author: TestDevice | str,
        device: TestDevice,
        follows: list[str],
        att: dict[str, Any] | None = None,
    ) -> str:
        self.devices[device.name] = device
        return self.put(
            self.entry(
                "add",
                author,
                follows,
                {"attestation": att, "device": device.record(self.mesh)},
            )
        )

    def attest(self, device: TestDevice, follows: list[str], **kwargs: Any) -> str:
        return self.put(
            self.entry(
                "attest",
                device,
                follows,
                {"attestation": identity_attestation(device, self.mesh, **kwargs)},
            )
        )

    def remove(
        self,
        author: TestDevice | str,
        target: TestDevice,
        follows: list[str],
        cutoff: dict[str, int] | None = None,
    ) -> str:
        cut = cutoff if cutoff is not None else {target.feed: 0}
        return self.put(
            self.entry("remove", author, follows, {"cutoff": cut, "device": target.id})
        )

    def wrap_grant(
        self, author: TestDevice, device: TestDevice, follows: list[str]
    ) -> str:
        grants = keys.make_wrap_grants(
            "personal",
            device.id,
            device.wrap_public,
            {self.genesis_epoch(): self.epoch_key},
            self.rng,
        )
        return self.put(
            self.entry(
                "wrap",
                author,
                follows,
                {"device": device.id, "grants": grants, "group": "personal"},
            )
        )

    def genesis_epoch(self) -> str:
        return self.mesh

    def epoch(
        self,
        author: TestDevice | str,
        follows: list[str],
        members: list[TestDevice],
        label: str,
    ) -> str:
        key = seed("epoch/personal/" + self.label + "/" + label)
        body = keys.make_epoch_body(
            "personal",
            key,
            {d.id: d.wrap_public for d in members},
            self.recovery["wrap_public"],
            self.rng,
        )
        return self.put(self.entry("epoch", author, follows, body))

    def stop(self, author: TestDevice, follows: list[str], counter: int) -> str:
        return self.put(self.entry("stop", author, follows, {"counter": counter}))


def header(
    device: TestDevice,
    seq: int,
    prev: str | None,
    clock_ms: int,
    *,
    after: list[str] | None = None,
    scope: str = "mesh",
    type_: str = "settings.set",
    labels: dict[str, str] | None = None,
    body_tag: str = "",
) -> dict[str, Any]:
    body_hash = crypto.sha256_id(f"body/{device.name}/{seq}/{body_tag}".encode())
    return event.build_header(
        device.ops_seed,
        seq=seq,
        prev=prev,
        after=after or [],
        clock=hlc(clock_ms),
        scope=scope,
        type=type_,
        labels=labels or {"confidentiality": "private", "integrity": "owner"},
        key="ck:" + b64url_encode(seed("ck/" + device.name)[:16]),
        body_hash=body_hash,
    )


def chain(
    device: TestDevice, count: int, start_ms: int = BASE_MS, **kwargs: Any
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prev = None
    for n in range(1, count + 1):
        h = header(device, n, prev, start_ms + n * 1000, **kwargs)
        out.append(h)
        prev = event.event_id(h)
    return out


def sealed_craft(
    header_obj: dict[str, Any],
    epoch_id: str,
    epoch_key: bytes,
    envelope_seq: int | None = None,
    nonce_tag: str = "h",
) -> dict[str, Any]:
    nonce = DetRng("craft/" + nonce_tag).bytes(24)
    seq = envelope_seq if envelope_seq is not None else header_obj.get("seq", 0)
    sealed = crypto.aead_seal(
        epoch_key,
        nonce,
        canonical(header_obj),
        event.header_aad(header_obj["feed"], seq, epoch_id),
    )
    return {
        "body_ct": None,
        "feed": header_obj["feed"],
        "header_ct": b64url_encode(crypto.id_bytes(epoch_id) + nonce + sealed),
        "seq": seq,
    }
