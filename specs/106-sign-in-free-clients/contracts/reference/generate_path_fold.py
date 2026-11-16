"""Generates the committed path fold table from the Unicode 16.0 data in the Python running it.
`--check` fails when the committed table differs, and the run fails outright on any other Unicode version.
"""

import argparse
import json
import sys
import unicodedata
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "path_fold.json"
UNICODE_VERSION = "16.0.0"
HANGUL = range(0xAC00, 0xD7A4)


def one(char: str) -> str:
    decomposed = unicodedata.normalize("NFD", char)
    return unicodedata.normalize("NFD", decomposed.casefold())


def render() -> str:
    if unicodedata.unidata_version != UNICODE_VERSION:
        raise SystemExit(
            f"needs Unicode {UNICODE_VERSION}, this Python has {unicodedata.unidata_version}"
        )
    assigned: list[list[int]] = []
    fold: dict[str, str] = {}
    ccc: dict[str, int] = {}
    for cp in range(0x110000):
        char = chr(cp)
        if unicodedata.category(char) != "Cn":
            if assigned and assigned[-1][1] == cp - 1:
                assigned[-1][1] = cp
            else:
                assigned.append([cp, cp])
        if unicodedata.combining(char):
            ccc[f"{cp:04X}"] = unicodedata.combining(char)
        if cp in HANGUL or 0xD800 <= cp <= 0xDFFF:
            continue
        if one(char) != char:
            fold[f"{cp:04X}"] = one(char)
    table = {"unicode": UNICODE_VERSION, "assigned": assigned, "ccc": ccc, "fold": fold}
    return json.dumps(table, indent=0, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            raise SystemExit("path_fold.json differs")
    else:
        OUTPUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
