"""Tests for backend/shared/qr.py's first-party QR encoder: known-answer matrices,
version/capacity selection, matrix structural invariants, format/version bit vectors,
every ECC level, and well-formed PNG output.
"""

from __future__ import annotations

import hashlib
import struct
import sys
import zlib
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from shared.qr import (  # noqa: E402
    QRError,
    encode_matrix,
    min_version_for,
    qr_png,
    qr_png_base64,
)
from shared.qr import _format_bits, _version_bits  # noqa: E402


def _matrix_hash(matrix) -> str:
    return hashlib.sha256(
        "".join("".join(map(str, row)) for row in matrix).encode()
    ).hexdigest()


KNOWN_ANSWERS = {
    "https://iam.ai.uky.edu/realms/astral/device?user_code=WDJB-MJHT":
        (37, "c6d93abd19be3ee2ddc7b27dbe85e3afc4e621a664f56f2e1900436a3754a887"),
    "https://iam.ai.uky.edu/realms/astral/device?user_code=ABCD-EFGH&x=1234567890":
        (37, "55129524b855ba26b11cb20915bb56b3b7a9bd81d552b71b044df2d5b031d977"),
    "http://127.0.0.1:8180/realms/astral/device?user_code=XKCD-2026":
        (33, "79b945484596bffac9aa7967a8bd552fb2c418a0f9c9f74cebc1025e4c0fc650"),
    "hello world 051":
        (25, "89700aa06249d1cf8dd9fbdaa40ee0d2a6461163025ac98060fda7bc877e34e4"),
}


def test_known_answer_matrices():
    for payload, (size, digest) in KNOWN_ANSWERS.items():
        m = encode_matrix(payload)
        assert len(m) == size and all(len(r) == size for r in m)
        assert _matrix_hash(m) == digest, f"matrix drift for {payload!r}"


def test_version_selection_and_capacity():
    assert min_version_for(14) == 1
    assert min_version_for(15) == 2
    assert min_version_for(62) == 4
    assert min_version_for(63) == 5
    assert min_version_for(213) == 10
    with pytest.raises(QRError):
        min_version_for(214)
    with pytest.raises(QRError):
        encode_matrix("x" * 500)
    with pytest.raises(QRError):
        encode_matrix("")


def test_matrix_structural_invariants():
    m = encode_matrix("https://example.com/device?user_code=TEST-CODE")
    size = len(m)
    version = (size - 17) // 4
    assert size == 17 + 4 * version

    def finder_ok(r0, c0):
        for dr in range(7):
            for dc in range(7):
                dark = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                if m[r0 + dr][c0 + dc] != (1 if dark else 0):
                    return False
        return True

    assert finder_ok(0, 0) and finder_ok(0, size - 7) and finder_ok(size - 7, 0)
    for i in range(8, size - 8):
        assert m[6][i] == (1 if i % 2 == 0 else 0)
        assert m[i][6] == (1 if i % 2 == 0 else 0)
    assert m[size - 8][8] == 1


def test_format_and_version_bit_vectors():
    assert _format_bits("L", 0) == 0b111011111000100
    assert _format_bits("M", 0) == 0b101010000010010
    assert _version_bits(7) == 0b000111110010010100


def test_all_ecc_levels_encode():
    for level in ("L", "M", "Q", "H"):
        m = encode_matrix("astral", ecc=level)
        assert len(m) >= 21
    with pytest.raises(QRError):
        encode_matrix("astral", ecc="X")


def test_png_is_wellformed():
    payload = "https://iam.ai.uky.edu/realms/astral/device?user_code=WDJB-MJHT"
    scale, border = 8, 4
    png = qr_png(payload, scale=scale, border=border)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png.endswith(struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND")))
    w, h, depth, ctype = struct.unpack(">IIBB", png[16:26])
    size = len(encode_matrix(payload))
    assert w == h == (size + 2 * border) * scale
    assert depth == 8 and ctype == 0
    idat_start = png.index(b"IDAT") + 4
    idat_len = struct.unpack(">I", png[png.index(b"IDAT") - 4:png.index(b"IDAT")])[0]
    raw = zlib.decompress(png[idat_start:idat_start + idat_len])
    assert len(raw) == h * (1 + w)
    first_row = raw[1:1 + w]
    assert set(first_row) == {255}
    center_of_finder = raw[(border * scale + 3 * scale) * (1 + w) + 1 + border * scale + 3 * scale]
    assert center_of_finder == 0


def test_png_base64_and_bounds():
    b64 = qr_png_base64("astral", scale=2, border=1)
    import base64
    assert base64.b64decode(b64).startswith(b"\x89PNG")
    with pytest.raises(QRError):
        qr_png("astral", scale=0)
    with pytest.raises(QRError):
        qr_png("astral", border=-1)
