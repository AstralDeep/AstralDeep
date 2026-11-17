"""Derives the published keys of the four seeded development mesh devices from public labels with the mesh reference test kit's seed and scalar rules, and the device ids that follow from their identity keys.
orchestrator/node_mesh.py refuses a mesh that lists one of these ids outside development, and devtools/dev_mesh.py opens the devices with these keys."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

NAMES = ("owner", "phone", "second-owner", "second-phone")
LABEL_PREFIX = "astral-dev-mesh/v1/"
SEED_PREFIX = b"astral-test-seed/"
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def seed(label: str) -> bytes:
    return hashlib.sha256(SEED_PREFIX + label.encode()).digest()


def scalar(label: str) -> int:
    return int.from_bytes(seed(label), "big") % (P256_ORDER - 1) + 1


def _point(value: int) -> bytes:
    return ec.derive_private_key(value, ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


@dataclass(frozen=True)
class DevDeviceKeys:
    name: str
    identity_scalar: int
    presence_scalar: int
    operations_secret: bytes
    wrap_secret: bytes
    restore_marker: bytes
    identity_public_key: bytes
    presence_public_key: bytes

    @property
    def device_id(self) -> str:
        return "sha256:" + hashlib.sha256(self.identity_public_key).hexdigest()


@lru_cache(maxsize=None)
def device_keys(name: str) -> DevDeviceKeys:
    if name not in NAMES:
        raise ValueError(f"{name!r} is not a development mesh device")
    label = LABEL_PREFIX + name + "/"
    identity, presence = scalar(label + "identity"), scalar(label + "presence")
    return DevDeviceKeys(
        name=name,
        identity_scalar=identity,
        presence_scalar=presence,
        operations_secret=seed(label + "ops"),
        wrap_secret=seed(label + "wrap"),
        restore_marker=seed(label + "restore"),
        identity_public_key=_point(identity),
        presence_public_key=_point(presence),
    )


@lru_cache(maxsize=1)
def device_ids() -> Mapping[str, str]:
    return MappingProxyType({name: device_keys(name).device_id for name in NAMES})
