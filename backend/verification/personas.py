"""Persona catalogue and synthetic fixture generation: a representative persona with a
deterministic generated input file carrying known markers so probes can prove file
provenance. Feeds drivers/in_process.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List


@dataclass
class Fixture:
    category: str
    extension: str
    filename: str
    writer: Callable[[str], None]
    known_markers: List[str] = field(default_factory=list)
    synthetic: bool = True
    expect_unsupported: bool = False


@dataclass
class Persona:
    key: str
    display_name: str
    fixture: Fixture


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
        ),
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
}


def get_persona(key: str) -> Persona:
    return _CATALOGUE[key]()


def unsupported_fixture() -> Fixture:
    return _unsupported_fixture()


def materialize(fixture: Fixture, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, fixture.filename)
    fixture.writer(path)
    return path
