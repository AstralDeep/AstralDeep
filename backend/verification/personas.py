"""Persona catalogue and synthetic fixture generation: four representative personas,
each with a deterministic generated input file carrying known markers so checks can
prove file provenance. Feeds verification/scenarios.py and the drivers.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class Fixture:
    category: str
    extension: str
    filename: str
    writer: Callable[[str], None]
    known_markers: List[str] = field(default_factory=list)
    chart: Optional[Dict[str, str]] = None
    synthetic: bool = True
    expect_unsupported: bool = False


@dataclass
class Persona:
    key: str
    display_name: str
    fixture: Fixture
    query: str
    warrants_ui: bool = True
    roles: List[str] = field(default_factory=lambda: ["user"])
    default_scopes: Dict[str, bool] = field(
        default_factory=lambda: {"tools:read": True, "tools:search": True}
    )
    expected_component_types: List[str] = field(default_factory=list)


def _write_csv(rows: List[List[str]]) -> Callable[[str], None]:
    def _w(path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            for r in rows:
                fh.write(",".join(str(c) for c in r) + "\n")
    return _w


def _write_text(text: str) -> Callable[[str], None]:
    def _w(path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return _w


def _write_png_solid(width: int = 8, height: int = 8,
                     rgb: tuple = (40, 90, 160)) -> Callable[[str], None]:
    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    def _w(path: str) -> None:
        sig = b"\x89PNG\r\n\x1a\n"
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        raw = bytearray()
        row = bytes(rgb) * width
        for _ in range(height):
            raw.append(0)
            raw.extend(row)
        idat = zlib.compress(bytes(raw))
        with open(path, "wb") as fh:
            fh.write(sig)
            fh.write(_chunk(b"IHDR", ihdr))
            fh.write(_chunk(b"IDAT", idat))
            fh.write(_chunk(b"IEND", b""))

    return _w


def _everyday() -> Persona:
    rows = [
        ["date", "description", "category", "amount"],
        ["2026-05-02", "CORNER GROCER", "Groceries", "84.20"],
        ["2026-05-06", "CITY TRANSIT", "Transport", "42.00"],
        ["2026-05-11", "BIG BOX STORE", "Shopping", "1234.56"],
        ["2026-05-19", "CORNER GROCER", "Groceries", "63.10"],
        ["2026-05-24", "POWER UTILITY", "Utilities", "150.75"],
    ]
    return Persona(
        key="everyday",
        display_name="Everyday person",
        fixture=Fixture(
            category="spreadsheet", extension="csv", filename="statement.csv",
            writer=_write_csv(rows),
            known_markers=["Groceries", "1234.56", "Utilities"],
            chart={"x_key": "category", "y_key": "amount"},
        ),
        query="Here is my bank statement. Show me where my money went last month, "
              "broken down by category.",
        expected_component_types=["plotly_chart", "table", "metric"],
    )


def _researcher() -> Persona:
    doc = (
        "# Synthetic Study: Reaction Time vs. Dose\n\n"
        "## Abstract\n"
        "This SYNTHETIC dataset summary reports that reaction time decreased "
        "monotonically with dose, with a notable inflection at 40 mg. "
        "Key finding: the EC50 was estimated at 41.7 mg.\n\n"
        "## Key points\n"
        "- Sample size n=120 (synthetic)\n"
        "- Largest effect observed at 80 mg\n"
        "- No adverse events (synthetic)\n"
    )
    return Persona(
        key="researcher",
        display_name="Researcher",
        fixture=Fixture(
            category="text", extension="md", filename="study_summary.md",
            writer=_write_text(doc),
            known_markers=["EC50", "41.7", "inflection"],
        ),
        query="Summarize this paper with the key points and the headline finding.",
        warrants_ui=False,
        expected_component_types=["card", "tabs", "text"],
    )


def _government() -> Persona:
    rows = [
        ["department", "fy2025", "fy2026"],
        ["Public Works", "1200000", "1320000"],
        ["Parks", "450000", "445000"],
        ["Public Safety", "2300000", "2530000"],
        ["Libraries", "300000", "315000"],
    ]
    return Persona(
        key="government",
        display_name="Government official",
        fixture=Fixture(
            category="spreadsheet", extension="csv", filename="city_budget.csv",
            writer=_write_csv(rows),
            known_markers=["Public Safety", "2530000", "Libraries"],
            chart={"x_key": "department", "y_key": "fy2026"},
        ),
        query="This is our public city budget. Break it down by department and "
              "show the year-over-year change.",
        expected_component_types=["plotly_chart", "table", "metric"],
    )


def _medical() -> Persona:
    return Persona(
        key="medical",
        display_name="Medical professional",
        fixture=Fixture(
            category="image", extension="png", filename="synthetic_scan.png",
            writer=_write_png_solid(),
            known_markers=[],
        ),
        query="Here is a synthetic scan image. Describe what it shows.",
        roles=["user"],
        warrants_ui=False,
        expected_component_types=["image", "card"],
    )


def _unsupported_fixture() -> Fixture:
    return Fixture(
        category="data", extension="zzv", filename="mystery.zzv",
        writer=_write_text("VERIF-SYNTHETIC-UNSUPPORTED-BLOB\n"),
        known_markers=[],
        expect_unsupported=True,
    )


_CATALOGUE: Dict[str, Callable[[], Persona]] = {
    "everyday": _everyday,
    "researcher": _researcher,
    "government": _government,
    "medical": _medical,
}


def all_personas(keys: Optional[List[str]] = None) -> List[Persona]:
    items = [factory() for factory in _CATALOGUE.values()]
    if keys:
        want = set(keys)
        items = [p for p in items if p.key in want]
    return items


def get_persona(key: str) -> Persona:
    return _CATALOGUE[key]()


def unsupported_fixture() -> Fixture:
    return _unsupported_fixture()


def materialize(fixture: Fixture, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, fixture.filename)
    fixture.writer(path)
    return path
