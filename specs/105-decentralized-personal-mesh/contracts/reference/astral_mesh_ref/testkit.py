"""Deterministic keys, randomness and clocks for generating vectors, so every vector is reproducible from fixed seeds.
Nothing here is used by the verification paths, and the seeds it derives are published test secrets.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from . import crypto
from .cj import b64url_encode


def seed(label: str) -> bytes:
    return hashlib.sha256(b"astral-test-seed/" + label.encode()).digest()


def scalar(label: str) -> int:
    return int.from_bytes(seed(label), "big") % (crypto.P256_ORDER - 1) + 1


class DetRng:
    def __init__(self, label: str) -> None:
        self._seed = seed("rng/" + label)
        self._counter = 0

    def bytes(self, count: int) -> bytes:
        out = b""
        while len(out) < count:
            out += hashlib.sha256(
                self._seed + self._counter.to_bytes(8, "big")
            ).digest()
            self._counter += 1
        return out[:count]


@dataclass
class TestDevice:
    __test__ = False
    name: str
    kind: str = "desktop"
    identity_scalar: int = field(init=False)
    presence_scalar: int = field(init=False)
    ops_seed: bytes = field(init=False)
    wrap_private: bytes = field(init=False)

    def __post_init__(self) -> None:
        self.identity_scalar = scalar(self.name + "/identity")
        self.presence_scalar = scalar(self.name + "/presence")
        self.ops_seed = seed(self.name + "/ops")
        self.wrap_private = seed(self.name + "/wrap")

    @property
    def identity_public(self) -> bytes:
        return crypto.p256_public(self.identity_scalar)

    @property
    def presence_public(self) -> bytes:
        return crypto.p256_public(self.presence_scalar)

    @property
    def ops_public(self) -> bytes:
        return crypto.ed25519_public(self.ops_seed)

    @property
    def wrap_public(self) -> bytes:
        return crypto.x25519_public(self.wrap_private)

    @property
    def id(self) -> str:
        return crypto.device_id(self.identity_public)

    @property
    def feed(self) -> str:
        return b64url_encode(self.ops_public)

    def record(self, mesh: str | None, label: str | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "identity": b64url_encode(self.identity_public),
            "kind": self.kind,
            "label": label or self.name,
            "ops": b64url_encode(self.ops_public),
            "presence": b64url_encode(self.presence_public),
            "wrap": b64url_encode(self.wrap_public),
        }
        record["cert"] = crypto.sign_detached(
            "p256",
            self.identity_scalar,
            "astral.pairing/v1",
            {"device": record, "mesh": mesh},
        )
        return record

    def secrets(self) -> dict[str, str]:
        return {
            "identity_scalar_hex": f"{self.identity_scalar:064x}",
            "presence_scalar_hex": f"{self.presence_scalar:064x}",
            "ops_seed_hex": self.ops_seed.hex(),
            "wrap_private_hex": self.wrap_private.hex(),
        }


def hlc(ms: int, n: int = 0) -> dict[str, int]:
    return {"ms": ms, "n": n}
