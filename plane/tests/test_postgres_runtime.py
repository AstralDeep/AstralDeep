"""Tests for src/astralplane/database/postgres.py: driver-pool construction, bounded
checkout, and credential-free error redaction, without a live PostgreSQL driver.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

import astralplane
import astralplane.database.postgres as postgres_module
from astralplane import api as api_module
from astralplane.api import create_postgres_runtime
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.errors import InitializationError


class _ThreadedPool:
    instances: ClassVar[list[_ThreadedPool]] = []

    def __init__(self, minimum: int, maximum: int, **kwargs: object) -> None:
        self.minconn = minimum
        self.maxconn = maximum
        self.kwargs = kwargs
        self.available = [object() for _ in range(maximum)]
        self.returned: list[tuple[object, bool]] = []
        self.closed = False
        self.instances.append(self)

    def getconn(self) -> object:
        return self.available.pop()

    def putconn(self, connection: object, *, close: bool = False) -> None:
        self.returned.append((connection, close))
        if not close:
            self.available.append(connection)

    def _putconn(
        self,
        connection: object,
        key: object = None,
        close: bool = False,
    ) -> tuple[object, object, bool, int]:
        return connection, key, close, self.minconn

    def closeall(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fake_pool() -> None:
    _ThreadedPool.instances.clear()


def _driver_import(name: str) -> object:
    if name == "psycopg2.pool":
        return SimpleNamespace(ThreadedConnectionPool=_ThreadedPool)
    if name == "psycopg2.extras":
        return SimpleNamespace(RealDictCursor=object())
    raise ImportError(name)


def test_factory_owns_driver_construction_and_bounded_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postgres_module.importlib, "import_module", _driver_import)

    pool = create_postgres_driver_pool(
        "postgresql://opaque.invalid/plane",
        minimum_connections=1,
        maximum_connections=2,
        acquire_timeout_seconds=0.01,
        connect_timeout_seconds=7,
        application_name="astralplane:test",
    )
    first = pool.getconn()
    second = pool.getconn()

    driver = _ThreadedPool.instances[0]
    assert driver.kwargs["dsn"] == "postgresql://opaque.invalid/plane"
    assert driver.kwargs["connect_timeout"] == 7
    assert driver.kwargs["application_name"] == "astralplane:test"
    with pytest.raises(InitializationError) as error:
        pool.getconn()
    assert error.value.code == "pool_acquire_timeout"

    pool.putconn(first)
    pool.putconn(second, close=True)
    pool.closeall()
    assert driver.returned == [(first, False), (second, True)]
    assert driver.closed


def test_driver_checkout_failure_releases_the_bounded_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postgres_module.importlib, "import_module", _driver_import)
    pool = create_postgres_driver_pool(
        "postgresql://opaque.invalid/plane",
        minimum_connections=1,
        maximum_connections=1,
        acquire_timeout_seconds=0.01,
    )
    driver = _ThreadedPool.instances[0]
    driver.available.clear()

    with pytest.raises(IndexError):
        pool.getconn()
    replacement = object()
    driver.available.append(replacement)
    assert pool.getconn() is replacement


def test_retaining_driver_uses_maximum_as_the_idle_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postgres_module.importlib, "import_module", _driver_import)
    create_postgres_driver_pool(
        "postgresql://opaque.invalid/plane",
        minimum_connections=1,
        maximum_connections=4,
    )
    driver = _ThreadedPool.instances[0]

    result = driver._putconn(object())

    assert result[3] == 4
    assert driver.minconn == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"database_url": ""},
        {"minimum_connections": 0},
        {"maximum_connections": 0},
        {"acquire_timeout_seconds": float("inf")},
        {"connect_timeout_seconds": 0},
        {"application_name": "x\nunsafe"},
    ],
)
def test_invalid_pool_configuration_fails_before_driver_import(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
) -> None:
    def unexpected_import(name: str) -> object:
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(postgres_module.importlib, "import_module", unexpected_import)
    arguments: dict[str, object] = {
        "database_url": "postgresql://opaque.invalid/plane",
        "minimum_connections": 1,
        "maximum_connections": 2,
        "acquire_timeout_seconds": 1.0,
        "connect_timeout_seconds": 2,
        "application_name": "astralplane",
    }
    arguments.update(overrides)

    with pytest.raises(InitializationError):
        create_postgres_driver_pool(**arguments)  # type: ignore[arg-type]


def test_missing_driver_has_a_typed_credential_free_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(postgres_module.importlib, "import_module", missing)
    secret_dsn = "postgresql://user:do-not-leak@opaque.invalid/plane"

    with pytest.raises(InitializationError) as error:
        create_postgres_driver_pool(secret_dsn, minimum_connections=1)

    assert error.value.code == "postgres_driver_unavailable"
    assert "do-not-leak" not in str(error.value)


def test_driver_construction_failure_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingPool:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(f"sensitive inputs: {args!r} {kwargs!r}")

    def imports(name: str) -> object:
        if name == "psycopg2.pool":
            return SimpleNamespace(ThreadedConnectionPool=FailingPool)
        if name == "psycopg2.extras":
            return SimpleNamespace(RealDictCursor=object())
        raise ImportError(name)

    monkeypatch.setattr(postgres_module.importlib, "import_module", imports)

    with pytest.raises(InitializationError) as error:
        create_postgres_driver_pool("postgresql://user:do-not-leak@opaque.invalid/plane")

    assert error.value.code == "postgres_pool_construction_failed"
    assert "do-not-leak" not in str(error.value)


class _DriverPool:
    def __init__(self) -> None:
        self.closed = False

    def getconn(self) -> object:
        raise AssertionError("runtime construction must remain inert")

    def putconn(self, connection: object, *, close: bool = False) -> None:
        raise AssertionError((connection, close))

    def closeall(self) -> None:
        self.closed = True


class _Coordinator:
    def coordinate(self, **kwargs: object) -> object:
        raise AssertionError(kwargs)


class _Hook:
    name = "postgres-runtime"
    version = "v1"

    def reconcile(self, context: object) -> object:
        return context


def test_public_postgres_runtime_factory_owns_pool_and_runtime_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert astralplane.create_postgres_runtime is create_postgres_runtime
    driver = _DriverPool()
    calls: list[tuple[str, dict[str, object]]] = []

    def create_driver(database_url: str, **kwargs: object) -> _DriverPool:
        calls.append((database_url, kwargs))
        return driver

    monkeypatch.setattr(api_module, "create_postgres_driver_pool", create_driver)

    runtime = create_postgres_runtime(
        "postgresql://opaque.invalid/plane",
        identity="plane-runtime-test",
        reconcilers=(_Hook(),),
        coordinator=_Coordinator(),  # type: ignore[arg-type]
        minimum_connections=3,
        maximum_connections=7,
    )

    assert calls == [
        (
            "postgresql://opaque.invalid/plane",
            {
                "acquire_timeout_seconds": 30.0,
                "application_name": "astralplane:plane-runtime-test",
                "connect_timeout_seconds": 10,
                "maximum_connections": 7,
                "minimum_connections": 3,
            },
        )
    ]
    assert not driver.closed
    runtime.close()
    assert driver.closed


def test_public_factory_closes_driver_when_composition_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _DriverPool()
    monkeypatch.setattr(
        api_module,
        "create_postgres_driver_pool",
        lambda *args, **kwargs: driver,
    )

    with pytest.raises(InitializationError):
        create_postgres_runtime(
            "postgresql://opaque.invalid/plane",
            identity="not valid",
            reconcilers=(_Hook(),),
            coordinator=_Coordinator(),  # type: ignore[arg-type]
        )

    assert driver.closed
