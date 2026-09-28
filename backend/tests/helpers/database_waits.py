"""PostgreSQL wait helpers for tests that hold a real lock while the code under test waits
or refuses around it: evidence that the database's own timeout ended a wait, an
unbounded wait for one statement or for every request-path transaction in a race test,
and observation of a real lock wait or worker exit.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager

WAIT_BOUND_SQLSTATES = frozenset({"55P03", "57014"})


def database_wait_bound(error: BaseException) -> str | None:
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        code = getattr(current, "pgcode", None) or getattr(current, "sqlstate", None)
        if code in WAIT_BOUND_SQLSTATES:
            return code
        pending.extend((current.__cause__, current.__context__))
    return None


@contextmanager
def unbounded_statement_waits(transaction):
    before = transaction.fetch_one(
        "SELECT current_setting('lock_timeout') AS lock_timeout, "
        "current_setting('statement_timeout') AS statement_timeout")
    transaction.fetch_one("SELECT set_config('lock_timeout', '0', true), "
                          "set_config('statement_timeout', '0', true)")
    yield
    transaction.fetch_one("SELECT set_config('lock_timeout', %s, true), "
                          "set_config('statement_timeout', %s, true)",
                          (before["lock_timeout"], before["statement_timeout"]))


def unbounded_request_execution_waits(monkeypatch):
    from astralplane.repositories.history import SessionRepository

    monkeypatch.setattr(SessionRepository, "bound_request_execution_waits",
                        staticmethod(lambda transaction: None))


def observe_lock_wait(transaction, *, blocker, waiter, finished):
    while not transaction.fetch_one("SELECT %s = ANY(pg_blocking_pids(%s)) AS waiting",
                                    (blocker, waiter))["waiting"]:
        if finished.is_set():
            return False
        time.sleep(0.001)
    return True


def plane_transaction_workers():
    return {task for task in asyncio.all_tasks() if task.get_name() == "astralplane-transaction"}
