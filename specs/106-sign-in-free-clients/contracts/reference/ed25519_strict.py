"""Ed25519 point decoding and strict signature verification for the caller ticket holder key and proof.
It adds the canonical encoding, on-curve, small-order and canonical S checks that the library verifier alone does not guarantee."""

from __future__ import annotations

from astral_mesh_ref import crypto

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)
IDENTITY = (0, 1)

Point = tuple[int, int]


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


def add(a: Point, b: Point) -> Point:
    x1, y1 = a
    x2, y2 = b
    t = D * x1 * x2 * y1 * y2 % P
    return (
        (x1 * y2 + y1 * x2) * pow(1 + t, P - 2, P) % P,
        (y1 * y2 + x1 * x2) * pow(1 - t, P - 2, P) % P,
    )


def small_order(point: Point) -> bool:
    for _ in range(3):
        point = add(point, point)
    return point == IDENTITY


def usable_public_key(encoded: bytes) -> bool:
    point = decode_point(encoded)
    return point is not None and not small_order(point)


def verify_strict(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(signature) != 64 or not usable_public_key(public):
        return False
    r = decode_point(signature[:32])
    if r is None or small_order(r):
        return False
    if int.from_bytes(signature[32:], "little") >= L:
        return False
    return crypto.ed25519_verify(public, message, signature)
