"""Generates phi_fold.json, the PHI detector skeleton map of every code point to one ASCII character, from the Unicode 16.0 data in the Python running it.
`--check` fails when the committed table differs, or when a mapped character would change the class that Python's regular expressions give the original (decimal digit, word, space or other), and the run refuses any other Unicode version.
phi_detector.py reads the table, so no implementation depends on its own runtime's Unicode data."""

import argparse
import sys
import unicodedata
from pathlib import Path

UNICODE = "16.0.0"
LAST_CODE_POINT = 0x10FFFF
OUTPUT = Path(__file__).resolve().with_name("phi_fold.json")
ASCII_SPACE = " \t\n\v\f\r"
CASE_EQUIVALENTS = {0x130: "I", 0x131: "i"}
URL_STOPS = "<>\"'"


def unicode_class(char: str) -> str:
    if char.isspace():
        return "space"
    if char.isalnum() or char == "_":
        return "word"
    return "other"


def ascii_class(char: str) -> str:
    if char in ASCII_SPACE:
        return "space"
    if char.isascii() and (char.isalnum() or char == "_"):
        return "word"
    return "other"


def target(point: int) -> str:
    char = chr(point)
    if point < 0x80:
        return " " if 0x1C <= point <= 0x1F else char
    if 0xD800 <= point <= 0xDFFF:
        return "~"
    digit = unicodedata.decimal(char, None)
    if digit is not None:
        return str(digit)
    if point in CASE_EQUIVALENTS:
        return CASE_EQUIVALENTS[point]
    compat = unicodedata.normalize("NFKC", char)
    if (
        len(compat) == 1
        and compat.isascii()
        and compat not in URL_STOPS
        and ascii_class(compat) == unicode_class(char)
    ):
        return compat
    return {"space": " ", "word": "x", "other": "~"}[unicode_class(char)]


def runs() -> list[list]:
    out: list[list] = []
    for point in range(LAST_CODE_POINT + 1):
        value = target(point)
        if out and out[-1][2] == value and out[-1][1] == point - 1:
            out[-1][1] = point
        else:
            out.append([point, point, value])
    return out


def render() -> str:
    def text(value: str) -> str:
        if value == '"' or value == "\\":
            return "\\" + value
        if ord(value) < 0x20 or value == "\x7f":
            return f"\\u{ord(value):04x}"
        return value

    rows = ",\n".join(f'    [{low}, {high}, "{text(value)}"]' for low, high, value in runs())
    return (
        "{\n"
        '  "format": "astral.phi-fold/v1",\n'
        '  "range": "inclusive first and last code point, then the ASCII character each maps to",\n'
        '  "runs": [\n' + rows + "\n  ],\n"
        f'  "unicode": "{UNICODE}"\n'
        "}\n"
    )


def class_breaks() -> list[str]:
    wrong = []
    for point in range(LAST_CODE_POINT + 1):
        if 0xD800 <= point <= 0xDFFF:
            continue
        char = chr(point)
        mapped = target(point)
        if ascii_class(mapped) != unicode_class(char):
            wrong.append(f"U+{point:04X}")
        elif char.isdecimal() and not mapped.isdigit():
            wrong.append(f"U+{point:04X}")
    return wrong


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if unicodedata.unidata_version != UNICODE:
        raise SystemExit(
            f"phi_fold needs Unicode {UNICODE} data, this Python "
            f"{sys.version.split()[0]} has {unicodedata.unidata_version}"
        )
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            raise SystemExit("phi_fold.json differs from Unicode 16.0 data")
        wrong = class_breaks()
        if wrong:
            raise SystemExit(f"fold changes a character class: {wrong[:5]!r}")
    else:
        OUTPUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
