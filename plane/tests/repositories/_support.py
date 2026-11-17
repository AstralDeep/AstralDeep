"""Scripted Result/ScriptedTransaction test doubles standing in for a real Plane
Transaction; shared by most astralplane.repositories unit tests that don't need
PostgreSQL.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Result:
    rowcount: int = 1
    status_message: str | None = "OK"
    returned_records: tuple[dict[str, Any], ...] = ()


class ScriptedTransaction:
    def __init__(
        self,
        *,
        one: list[dict[str, Any] | None | BaseException] | None = None,
        all_rows: list[tuple[dict[str, Any], ...] | BaseException] | None = None,
        execute: list[Result | BaseException] | None = None,
    ) -> None:
        self.one = deque(one or [])
        self.all_rows = deque(all_rows or [])
        self.execute_results = deque(execute or [])
        self.calls: list[tuple[str, str, object]] = []

    def fetch_one(self, statement: str, parameters: object = ()) -> dict[str, Any] | None:
        self.calls.append(("one", statement, parameters))
        if not self.one:
            raise AssertionError(f"unexpected fetch_one: {statement}")
        result = self.one.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def fetch_all(self, statement: str, parameters: object = ()) -> tuple[dict[str, Any], ...]:
        self.calls.append(("all", statement, parameters))
        if not self.all_rows:
            raise AssertionError(f"unexpected fetch_all: {statement}")
        result = self.all_rows.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def execute(self, statement: str, parameters: object = ()) -> Result:
        self.calls.append(("execute", statement, parameters))
        if not self.execute_results:
            return Result()
        result = self.execute_results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def fetch_sql(self) -> str:
        return "\n".join(statement for _, statement, _ in self.calls)
