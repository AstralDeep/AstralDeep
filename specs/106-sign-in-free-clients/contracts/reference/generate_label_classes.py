"""Generates label_classes.json, the code point ranges of the Unicode 16.0 categories a caller ticket label refuses, from this interpreter's Unicode data.
It uses only the standard library, refuses to run unless that data is Unicode 16.0.0, and `--check` fails when the committed table differs.
caller_ticket.py reads the table, so no implementation depends on its own runtime's Unicode version."""

import argparse
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path

UNICODE = "16.0.0"
REFUSED = ("Cc", "Cf", "Cs", "Co", "Cn")
LAST_CODE_POINT = 0x10FFFF
OUTPUT = Path(__file__).resolve().with_name("label_classes.json")


def ranges(category_of: Callable[[int], str]) -> dict[str, list[list[int]]]:
    out: dict[str, list[list[int]]] = {name: [] for name in REFUSED}
    for point in range(LAST_CODE_POINT + 1):
        spans = out.get(category_of(point))
        if spans is None:
            continue
        if spans and spans[-1][1] == point - 1:
            spans[-1][1] = point
        else:
            spans.append([point, point])
    return out


def render(version: str, category_of: Callable[[int], str]) -> str:
    spans = ranges(category_of)
    blocks = []
    for name in sorted(spans):
        rows = ",\n".join(f"      [{first}, {last}]" for first, last in spans[name])
        blocks.append(f'    "{name}": [\n{rows}\n    ]')
    return (
        "{\n"
        '  "format": "astral.label-classes/v1",\n'
        '  "range": "inclusive first and last code point",\n'
        '  "refused": {\n' + ",\n".join(blocks) + "\n  },\n"
        f'  "unicode": "{version}"\n'
        "}\n"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if unicodedata.unidata_version != UNICODE:
        raise SystemExit(
            f"label_classes needs Unicode {UNICODE} data, this Python "
            f"{sys.version.split()[0]} has {unicodedata.unidata_version}"
        )
    content = render(UNICODE, lambda point: unicodedata.category(chr(point)))
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            raise SystemExit("label_classes.json differs from Unicode 16.0 data")
    else:
        OUTPUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
