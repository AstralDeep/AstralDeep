"""Generates html_entities.json, the character reference tables of Python 3.14's `html.unescape`: every HTML5 named reference with and without its semicolon, the numeric references it remaps and the numeric references it removes.
`--check` fails when the committed table differs, and the run refuses any Python other than 3.14.
phi_detector.py reads the table and reproduces `html.unescape` from it, so no implementation calls its runtime's HTML decoder."""

import argparse
import html
import html.entities
import json
import sys
from pathlib import Path

PYTHON = (3, 14)
OUTPUT = Path(__file__).resolve().with_name("html_entities.json")


def removed_ranges() -> list[list[int]]:
    spans: list[list[int]] = []
    for point in sorted(html._invalid_codepoints):
        if spans and spans[-1][1] == point - 1:
            spans[-1][1] = point
        else:
            spans.append([point, point])
    return spans


def render() -> str:
    table = {
        "format": "astral.html-entities/v1",
        "named": dict(sorted(html.entities.html5.items())),
        "numeric_removed": removed_ranges(),
        "numeric_replaced": {
            str(point): text for point, text in sorted(html._invalid_charrefs.items())
        },
        "pattern": html._charref.pattern,
        "python": ".".join(str(part) for part in PYTHON),
    }
    return json.dumps(table, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != PYTHON:
        raise SystemExit(
            f"html_entities needs Python {PYTHON[0]}.{PYTHON[1]}, this is {sys.version.split()[0]}"
        )
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            raise SystemExit("html_entities.json differs from Python 3.14's html module")
    else:
        OUTPUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
