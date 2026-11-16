"""Ed25519 point arithmetic and strict signature verification for the caller ticket holder key and proof.
It adds the canonical encoding, on-curve, small-order, torsion-free and canonical S checks that the library verifier alone does not guarantee."""

from __future__ import annotations

from astral_mesh_ref import crypto

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)
IDENTITY = (0, 1)
BASE = (
    15112221349535400772501151409588531511454012693041857206046113283949847762202,
    46316835694926478169428394003475163141307993866256225615783033603165251855960,
)

Point = tuple[int, int]
Extended = tuple[int, int, int, int]


def decode_point(encoded: bytes) -> Point | None:
    if len(encoded) != 32:
        return None
    value = int.from_bytes(encoded, "little")
    sign, y = value >> 255, value & ((1 << 255) - 1)
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else (0, y)
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    if x & 1 != sign:
        x = P - x
    return (x, y)


def encode_point(point: Point) -> bytes:
    x, y = point
    return (y | (x & 1) << 255).to_bytes(32, "little")


def add(a: Point, b: Point) -> Point:
    x1, y1 = a
    x2, y2 = b
    t = D * x1 * x2 * y1 * y2 % P
    return (
        (x1 * y2 + y1 * x2) * pow(1 + t, P - 2, P) % P,
        (y1 * y2 + x1 * x2) * pow(1 - t, P - 2, P) % P,
    )


def _add_extended(a: Extended, b: Extended) -> Extended:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    minus = (y1 - x1) * (y2 - x2) % P
    plus = (y1 + x1) * (y2 + x2) % P
    c = 2 * D * t1 * t2 % P
    d = 2 * z1 * z2 % P
    e, f, g, h = plus - minus, d - c, d + c, plus + minus
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def multiply(point: Point, scalar: int) -> Point:
    result: Extended = (0, 1, 1, 0)
    addend: Extended = (point[0], point[1], 1, point[0] * point[1] % P)
    while scalar:
        if scalar & 1:
            result = _add_extended(result, addend)
        addend = _add_extended(addend, addend)
        scalar >>= 1
    x, y, z, _ = result
    inverse = pow(z, P - 2, P)
    return (x * inverse % P, y * inverse % P)


def small_order(point: Point) -> bool:
    return multiply(point, 8) == IDENTITY


def torsion_free(point: Point) -> bool:
    return multiply(point, L) == IDENTITY


def usable_public_key(encoded: bytes) -> bool:
    point = decode_point(encoded)
    return point is not None and not small_order(point) and torsion_free(point)


def verify_strict(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(signature) != 64 or not usable_public_key(public):
        return False
    r = decode_point(signature[:32])
    if r is None or small_order(r):
        return False
    if int.from_bytes(signature[32:], "little") >= L:
        return False
    return crypto.ed25519_verify(public, message, signature)
