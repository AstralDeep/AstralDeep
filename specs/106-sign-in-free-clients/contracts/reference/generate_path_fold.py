"""Generates the committed path fold table from the Unicode 16.0 data in the Python running it.
`--check` fails when the committed table differs, or when the reference fold in exec_policy.py differs from canonical caseless match on any assigned code point alone, or on any mark or code point that NFD or case folding changes, placed before or after a mark of each combining class.
The run fails outright on any other Unicode version.
"""

import argparse
import json
import sys
import unicodedata
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "path_fold.json"
UNICODE_VERSION = "16.0.0"
HANGUL = range(0xAC00, 0xD7A4)


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def caseless(text: str) -> str:
    return nfd(nfd(text).casefold())


def render() -> str:
    if unicodedata.unidata_version != UNICODE_VERSION:
        raise SystemExit(
            f"needs Unicode {UNICODE_VERSION}, this Python has {unicodedata.unidata_version}"
        )
    assigned: list[list[int]] = []
    decompose: dict[str, str] = {}
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
        if nfd(char) != char:
            decompose[f"{cp:04X}"] = nfd(char)
        elif nfd(char.casefold()) != char:
            fold[f"{cp:04X}"] = nfd(char.casefold())
    table = {
        "unicode": UNICODE_VERSION,
        "assigned": assigned,
        "ccc": ccc,
        "decompose": decompose,
        "fold": fold,
    }
    return json.dumps(table, indent=0, sort_keys=True) + "\n"


def fold_mismatches() -> list[str]:
    sys.path.insert(0, str(OUTPUT.parent))
    import exec_policy

    def fold(text: str) -> str:
        return exec_policy._fold(text, "darwin")

    chars = [
        chr(cp)
        for low, high in exec_policy._TABLE["assigned"]
        for cp in range(low, high + 1)
        if not 0xD800 <= cp <= 0xDFFF
    ]
    classes: dict[int, str] = {}
    marks = []
    for mark in sorted(exec_policy._CCC, key=ord):
        if caseless(mark) != mark:
            marks.append(mark)
        elif exec_policy._CCC[mark] not in classes:
            classes[exec_policy._CCC[mark]] = mark
    marks += classes.values()
    changing = [ch for ch in chars if caseless(ch) != ch or ch in exec_policy._CCC]
    samples = ["x" + ch for ch in chars]
    samples += ["x" + ch + mark for ch in changing for mark in marks]
    samples += ["x" + mark + ch for ch in changing for mark in marks]
    return [text for text in samples if fold(text) != caseless(text)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            raise SystemExit("path_fold.json differs")
        wrong = fold_mismatches()
        if wrong:
            raise SystemExit(
                f"fold differs from canonical caseless match: {wrong[:5]!r}"
            )
    else:
        OUTPUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
