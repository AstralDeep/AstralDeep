"""Names the SQL dialect behind a Plane transaction so repositories can pick the few statements
that differ between PostgreSQL and SQLite. Transactions expose it as `dialect`, and
`dialect_of` falls back to PostgreSQL for test doubles and third-party executors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class Dialect:
    name: str

    def __post_init__(self) -> None:
        if self.name not in {"postgresql", "sqlite"}:
            raise ValueError("unsupported Plane SQL dialect")

    @property
    def is_sqlite(self) -> bool:
        return self.name == "sqlite"

    @property
    def is_postgres(self) -> bool:
        return self.name == "postgresql"

    def choose(self, *, postgres: str, sqlite: str) -> str:
        return sqlite if self.is_sqlite else postgres


POSTGRES: Final = Dialect("postgresql")
SQLITE: Final = Dialect("sqlite")


def dialect_of(executor: Any) -> Dialect:
    found = getattr(executor, "dialect", None)
    return found if isinstance(found, Dialect) else POSTGRES


__all__ = ("POSTGRES", "SQLITE", "Dialect", "dialect_of")
