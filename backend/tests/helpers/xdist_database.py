"""Gives each pytest-xdist worker of a disposable suite its own empty PostgreSQL database named
<suite database>_<worker id>, created from template0; conftest.py points the three test database URLs
at it before any test connects and drops it when the worker ends.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from contextlib import closing
import re

DATABASE_VARIABLES = ("DATABASE_URL", "ASTRALPLANE_TEST_DATABASE_URL", "ASTRALPLANE_TEST_POSTGRES_DSN")
_WORKER = re.compile(r"gw[0-9]{1,4}\Z")
_DATABASE = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")


def worker_database(environ: MutableMapping[str, str]) -> tuple[str, str] | None:
    worker, suite = environ.get("PYTEST_XDIST_WORKER"), environ.get("DATABASE_URL", "")
    if worker is None or not suite or environ.get("ASTRAL_TEST_ISOLATED") != "1":
        return None
    from psycopg2.extensions import make_dsn, parse_dsn

    database = f"{parse_dsn(suite).get('dbname', '')}_{worker}"
    if _WORKER.fullmatch(worker) is None or _DATABASE.fullmatch(database) is None:
        raise ValueError("xdist worker database name is outside the safe identifier contract")
    return suite, make_dsn(suite, dbname=database)


def _execute(suite: str, *statements) -> None:
    import psycopg2

    with closing(psycopg2.connect(suite, connect_timeout=10)) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)


def isolate_worker_database(environ: MutableMapping[str, str]) -> tuple[str, str] | None:
    selected = worker_database(environ)
    if selected is None:
        return None
    suite, target = selected
    from psycopg2 import sql
    from psycopg2.extensions import parse_dsn

    name = sql.Identifier(parse_dsn(target)["dbname"])
    _execute(suite, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name),
             sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(name))
    for variable in DATABASE_VARIABLES:
        if environ.get(variable) == suite:
            environ[variable] = target
    return suite, target


def drop_worker_database(suite: str, target: str) -> None:
    from psycopg2 import sql
    from psycopg2.extensions import parse_dsn

    _execute(suite, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
        sql.Identifier(parse_dsn(target)["dbname"])))
