"""First-party ISO/IEC 18004 QR encoder (Model 2, byte mode, ECC levels L-H, versions
1-10) built on stdlib zlib/struct only, avoiding a third-party dependency. Used by
orchestrator/device_login.py to render the device-login verification QR.
"""

from __future__ import annotations

import base64
import struct
import zlib
from typing import List, Sequence, Union

__all__ = ["QRError", "encode_matrix", "qr_png", "qr_png_base64", "min_version_for"]


class QRError(ValueError):
    pass


_TOTAL_CODEWORDS = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172, 7: 196, 8: 242, 9: 292, 10: 346}

_ECC_BLOCKS = {
    "L": {
        1: (7, [19]), 2: (10, [34]), 3: (15, [55]), 4: (20, [80]), 5: (26, [108]),
        6: (18, [68, 68]), 7: (20, [78, 78]), 8: (24, [97, 97]),
        9: (30, [116, 116]), 10: (18, [68, 68, 69, 69]),
    },
    "M": {
        1: (10, [16]), 2: (16, [28]), 3: (26, [44]), 4: (18, [32, 32]),
        5: (24, [43, 43]), 6: (16, [27, 27, 27, 27]), 7: (18, [31, 31, 31, 31]),
        8: (22, [38, 38, 39, 39]), 9: (22, [36, 36, 36, 37, 37]),
        10: (26, [43, 43, 43, 43, 44]),
    },
    "Q": {
        1: (13, [13]), 2: (22, [22]), 3: (18, [17, 17]), 4: (26, [24, 24]),
        5: (18, [15, 15, 16, 16]), 6: (24, [19, 19, 19, 19]),
        7: (18, [14, 14, 14, 14, 15, 15]), 8: (22, [18, 18, 18, 18, 19, 19]),
        9: (20, [16, 16, 16, 16, 17, 17, 17, 17]),
        10: (24, [19, 19, 19, 19, 19, 19, 20, 20]),
    },
    "H": {
        1: (17, [9]), 2: (28, [16]), 3: (22, [13, 13]), 4: (16, [9, 9, 9, 9]),
        5: (22, [11, 11, 12, 12]), 6: (28, [15, 15, 15, 15]),
        7: (26, [13, 13, 13, 13, 14]), 8: (26, [14, 14, 14, 14, 15, 15]),
        9: (24, [12, 12, 12, 12, 13, 13, 13, 13]),
        10: (28, [15, 15, 15, 15, 15, 15, 16, 16]),
    },
}

_ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

_ECC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

_MAX_VERSION = 10

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree: int) -> List[int]:
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= _gf_mul(c, 1)
            nxt[j + 1] ^= _gf_mul(c, _GF_EXP[i])
        poly = nxt
    return poly


def _rs_ecc(data: Sequence[int], degree: int) -> List[int]:
    gen = _rs_generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            for j in range(degree):
                rem[j] ^= _gf_mul(gen[j + 1], factor)
    return rem


def min_version_for(payload_len: int, ecc: str = "M") -> int:
    for version in range(1, _MAX_VERSION + 1):
        ecc_per_block, data_blocks = _ECC_BLOCKS[ecc][version]
        data_bits = sum(data_blocks) * 8
        count_bits = 8 if version <= 9 else 16
        if 4 + count_bits + payload_len * 8 <= data_bits:
            return version
    raise QRError(
        f"payload of {payload_len} bytes exceeds version {_MAX_VERSION} capacity at level {ecc}"
    )


def _data_codewords(payload: bytes, version: int, ecc: str) -> List[int]:
    ecc_per_block, data_blocks = _ECC_BLOCKS[ecc][version]
    capacity_bits = sum(data_blocks) * 8
    count_bits = 8 if version <= 9 else 16

    bits: List[int] = []

    def put(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)
    put(len(payload), count_bits)
    for byte in payload:
        put(byte, 8)

    bits.extend([0] * min(4, capacity_bits - len(bits)))
    if len(bits) % 8:
        bits.extend([0] * (8 - len(bits) % 8))
    codewords = [
        int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)
    ]
    pad = (0xEC, 0x11)
    i = 0
    while len(codewords) < capacity_bits // 8:
        codewords.append(pad[i % 2])
        i += 1
    return codewords


def _interleave(codewords: List[int], version: int, ecc: str) -> List[int]:
    ecc_per_block, data_blocks = _ECC_BLOCKS[ecc][version]
    blocks: List[List[int]] = []
    eccs: List[List[int]] = []
    pos = 0
    for size in data_blocks:
        block = codewords[pos:pos + size]
        pos += size
        blocks.append(block)
        eccs.append(_rs_ecc(block, ecc_per_block))
    out: List[int] = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ecc_per_block):
        for e in eccs:
            out.append(e[i])
    return out


def _bch(value: int, poly: int, poly_bits: int) -> int:
    shift = poly_bits - 1
    rem = value << shift
    while rem.bit_length() >= poly_bits:
        rem ^= poly << (rem.bit_length() - poly_bits)
    return (value << shift) | rem


def _format_bits(ecc: str, mask: int) -> int:
    data = (_ECC_BITS[ecc] << 3) | mask
    return _bch(data, 0b10100110111, 11) ^ 0b101010000010010


def _version_bits(version: int) -> int:
    return _bch(version, 0b1111100100101, 13)


def _make_matrix(version: int):
    size = 17 + 4 * version
    matrix = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def set_module(r: int, c: int, val: int) -> None:
        matrix[r][c] = val
        reserved[r][c] = True

    def finder(r: int, c: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < size and 0 <= cc < size:
                    dark = (
                        0 <= dr <= 6 and 0 <= dc <= 6
                        and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                    )
                    set_module(rr, cc, 1 if dark else 0)

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    centers = _ALIGNMENT[version]
    for r in centers:
        for c in centers:
            if reserved[r][c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    set_module(r + dr, c + dc, 1 if dark else 0)

    for i in range(8, size - 8):
        if not reserved[6][i]:
            set_module(6, i, 1 if i % 2 == 0 else 0)
        if not reserved[i][6]:
            set_module(i, 6, 1 if i % 2 == 0 else 0)

    set_module(size - 8, 8, 1)
    for i in range(9):
        if not reserved[8][i]:
            set_module(8, i, 0)
        if not reserved[i][8]:
            set_module(i, 8, 0)
    for i in range(8):
        if not reserved[8][size - 1 - i]:
            set_module(8, size - 1 - i, 0)
        if not reserved[size - 1 - i][8]:
            set_module(size - 1 - i, 8, 0)

    if version >= 7:
        vbits = _version_bits(version)
        for i in range(18):
            bit = (vbits >> i) & 1
            set_module(i // 3, size - 11 + i % 3, bit)
            set_module(size - 11 + i % 3, i // 3, bit)

    return matrix, reserved


def _place_data(matrix, reserved, bits: List[int]) -> None:
    size = len(matrix)
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not reserved[r][c]:
                    matrix[r][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


_MASK_FUNCS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _apply_mask(matrix, reserved, mask: int):
    size = len(matrix)
    fn = _MASK_FUNCS[mask]
    out = [row[:] for row in matrix]
    for r in range(size):
        for c in range(size):
            if not reserved[r][c] and fn(r, c):
                out[r][c] ^= 1
    return out


def _draw_format(matrix, reserved, ecc: str, mask: int) -> None:
    size = len(matrix)
    fbits = _format_bits(ecc, mask)
    bit = [(fbits >> (14 - i)) & 1 for i in range(15)]
    coords_a = [
        (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
        (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),
    ]
    coords_b = [
        (size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
        (size - 5, 8), (size - 6, 8), (size - 7, 8),
        (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
        (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1),
    ]
    for i, (r, c) in enumerate(coords_a):
        matrix[r][c] = bit[i]
    for i, (r, c) in enumerate(coords_b):
        matrix[r][c] = bit[i]


def _penalty(matrix) -> int:
    size = len(matrix)
    score = 0
    for lines in (matrix, list(zip(*matrix))):
        for line in lines:
            run = 1
            for i in range(1, size):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            if run >= 5:
                score += 3 + run - 5
    for r in range(size - 1):
        for c in range(size - 1):
            if matrix[r][c] == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += 3
    pat_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat_b = pat_a[::-1]
    for lines in (matrix, list(zip(*matrix))):
        for line in lines:
            line = list(line)
            for i in range(size - 10):
                window = line[i:i + 11]
                if window == pat_a or window == pat_b:
                    score += 40
    dark = sum(sum(row) for row in matrix)
    percent = dark * 100 / (size * size)
    score += int(abs(percent - 50) // 5) * 10
    return score


def encode_matrix(data: Union[str, bytes], ecc: str = "M") -> List[List[int]]:
    if ecc not in _ECC_BLOCKS:
        raise QRError(f"unsupported ECC level {ecc!r}")
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if not payload:
        raise QRError("empty payload")
    version = min_version_for(len(payload), ecc)

    codewords = _interleave(_data_codewords(payload, version, ecc), version, ecc)
    bits: List[int] = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    base, reserved = _make_matrix(version)
    _place_data(base, reserved, bits)

    best = None
    best_score = None
    for mask in range(8):
        candidate = _apply_mask(base, reserved, mask)
        _draw_format(candidate, reserved, ecc, mask)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    return best


def qr_png(data: Union[str, bytes], *, scale: int = 8, border: int = 4, ecc: str = "M") -> bytes:
    if scale < 1 or border < 0:
        raise QRError("scale must be >=1 and border >=0")
    matrix = encode_matrix(data, ecc=ecc)
    size = len(matrix)
    dim = (size + 2 * border) * scale

    rows = bytearray()
    blank = bytes([0]) + bytes([255]) * dim
    for _ in range(border * scale):
        rows += blank
    for row in matrix:
        line = bytearray([0])
        line += bytes([255]) * (border * scale)
        for module in row:
            line += bytes([0 if module else 255]) * scale
        line += bytes([255]) * (border * scale)
        for _ in range(scale):
            rows += line
    for _ in range(border * scale):
        rows += blank

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + tag + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", dim, dim, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def qr_png_base64(data: Union[str, bytes], *, scale: int = 8, border: int = 4, ecc: str = "M") -> str:
    return base64.b64encode(qr_png(data, scale=scale, border=border, ecc=ecc)).decode("ascii")
