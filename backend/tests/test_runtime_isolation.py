"""Proves pytest-xdist workers cannot share the suite's PostgreSQL database: helpers/xdist_database.py
derives <suite database>_<worker id>, recreates it empty from template0 and repoints only the test
database URLs, and conftest.py applies it in every worker of the running session.
"""

from contextlib import closing
import os

import psycopg2
from psycopg2.extensions import parse_dsn
import pytest

from tests import conftest
from tests.helpers.xdist_database import (
    DATABASE_VARIABLES, drop_worker_database, isolate_worker_database, worker_database,
)


@pytest.fixture
def suite():
    url = os.environ.get("DATABASE_URL", "")
    if os.environ.get("ASTRAL_TEST_ISOLATED") != "1" or not url:
        pytest.skip("database unavailable: an isolated ASTRAL_TEST_ISOLATED PostgreSQL is required")
    return url


def environment(suite, worker):
    return {**dict.fromkeys(DATABASE_VARIABLES, suite), "ASTRAL_TEST_ISOLATED": "1",
            "PYTEST_XDIST_WORKER": worker, "UNRELATED_URL": suite}


def scalar(url, query):
    with closing(psycopg2.connect(url)) as connection, connection.cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchone()[0]


def database_exists(suite, name):
    with closing(psycopg2.connect(suite)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (name,))
        return cursor.fetchone()[0] == 1


def test_each_xdist_worker_gets_its_own_database(suite):
    first, second = environment(suite, "gw7"), environment(suite, "gw8")
    selected = [isolate_worker_database(first), isolate_worker_database(second)]
    try:
        names = [parse_dsn(target)["dbname"] for _, target in selected]
        assert names == [f"{parse_dsn(suite)['dbname']}_gw7", f"{parse_dsn(suite)['dbname']}_gw8"]
        assert all(first[name] == selected[0][1] for name in DATABASE_VARIABLES)
        assert all(second[name] == selected[1][1] for name in DATABASE_VARIABLES)
        assert first["UNRELATED_URL"] == second["UNRELATED_URL"] == suite
        with closing(psycopg2.connect(selected[0][1])) as connection, connection.cursor() as cursor:
            connection.autocommit = True
            cursor.execute("CREATE TABLE leftover (id integer)")
        tables = "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        assert scalar(selected[0][1], tables) == 1
        assert isolate_worker_database(environment(suite, "gw7")) == selected[0]
        assert scalar(selected[0][1], tables) == 0 and scalar(selected[1][1], tables) == 0
    finally:
        for pair in selected:
            drop_worker_database(*pair)
    assert not any(database_exists(suite, name) for name in names)


@pytest.mark.parametrize("change", [{"PYTEST_XDIST_WORKER": None}, {"ASTRAL_TEST_ISOLATED": "0"},
                                    {"DATABASE_URL": ""}])
def test_serial_or_shared_runs_keep_the_suite_database(change):
    environ = {**environment("dbname=ad_gate_suite", "gw0"), **change}
    environ = {name: value for name, value in environ.items() if value is not None}
    before = dict(environ)
    assert worker_database(environ) is None and isolate_worker_database(environ) is None
    assert environ == before


@pytest.mark.parametrize("environ", [environment("dbname=ad_gate_suite", "gw0;drop"),
                                     environment("dbname=ad_gate_suite", "worker"),
                                     environment(f"dbname=ad_gate_{'a' * 52}", "gw10")])
def test_unsafe_worker_database_names_are_refused(environ):
    with pytest.raises(ValueError, match="safe identifier"):
        worker_database(environ)


def test_this_session_runs_on_its_worker_database(suite):
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is None:
        assert conftest.WORKER_DATABASE is None
        return
    original, target = conftest.WORKER_DATABASE
    expected = f"{parse_dsn(original)['dbname']}_{worker}"
    assert all(parse_dsn(os.environ[name])["dbname"] == expected for name in DATABASE_VARIABLES)
    assert target == os.environ["DATABASE_URL"] and database_exists(original, expected)
