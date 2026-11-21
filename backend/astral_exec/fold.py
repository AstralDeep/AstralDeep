"""The darwin path fold, Unicode's canonical caseless match computed only from the committed fold_table.py and never from the running interpreter's Unicode data.
policy.py and select.py compare darwin paths through it, because APFS treats case and canonically equivalent spellings as one name.
"""

from __future__ import annotations

import bisect

from .fold_table import TABLE

_DECOMPOSE = {chr(int(key, 16)): value for key, value in TABLE["decompose"].items()}
_FOLD = {chr(int(key, 16)): value for key, value in TABLE["fold"].items()}
_CCC = {chr(int(key, 16)): value for key, value in TABLE["ccc"].items()}
_ASSIGNED = TABLE["assigned"]
_ASSIGNED_STARTS = [low for low, _ in _ASSIGNED]
_HANGUL_BASE, _HANGUL_COUNT = 0xAC00, 11172
_JAMO_L, _JAMO_V, _JAMO_T = 0x1100, 0x1161, 0x11A7
_JAMO_V_COUNT, _JAMO_T_COUNT = 21, 28


def assigned(char: str) -> bool:
    code = ord(char)
    at = bisect.bisect_right(_ASSIGNED_STARTS, code) - 1
    return at >= 0 and code <= _ASSIGNED[at][1]


def _hangul(char: str) -> str:
    lead, rest = divmod(ord(char) - _HANGUL_BASE, _JAMO_V_COUNT * _JAMO_T_COUNT)
    vowel, tail = divmod(rest, _JAMO_T_COUNT)
    out = chr(_JAMO_L + lead) + chr(_JAMO_V + vowel)
    return out + (chr(_JAMO_T + tail) if tail else "")


def _ordered(text: str) -> str:
    out: list[str] = []
    run: list[str] = []
    for char in text:
        if _CCC.get(char):
            run.append(char)
            continue
        out += sorted(run, key=lambda mark: _CCC[mark]) + [char]
        run = []
    return "".join(out + sorted(run, key=lambda mark: _CCC[mark]))


def _nfd(text: str) -> str:
    pieces = [
        _hangul(char)
        if 0 <= ord(char) - _HANGUL_BASE < _HANGUL_COUNT
        else _DECOMPOSE.get(char, char)
        for char in text
    ]
    return _ordered("".join(pieces))


def fold(path: str, platform: str | None) -> str:
    if platform != "darwin":
        return path
    return _nfd("".join(_FOLD.get(char, char) for char in _nfd(path)))


def contains(root: str, path: str, platform: str | None) -> bool:
    root, path = fold(root, platform), fold(path, platform)
    return path == root or path.startswith(root + "/")
