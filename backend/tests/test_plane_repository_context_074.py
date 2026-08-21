"""Fail-closed tests for Deep's SQL-free AstralPlane context seam."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)


class _Runtime:
    def __init__(self) -> None:
        self.transaction_value = object()
        self.entries = 0
        self.repositories = SimpleNamespace(example=object())

    @contextmanager
    def transaction(self):
        self.entries += 1
        yield self.transaction_value


def test_context_uses_only_the_initialized_plane_runtime() -> None:
    runtime = _Runtime()
    repository = object()
    context = PlaneRepositoryContext(repository=repository, plane_runtime=runtime)

    observed = context.call(lambda transaction, *, value: (transaction, value), value=3)

    assert observed == (runtime.transaction_value, 3)
    assert runtime.entries == 1
    assert context.repository is repository
    assert context.plane_runtime is runtime


def test_raw_legacy_database_is_rejected_without_borrowing_or_sql() -> None:
    class RawDatabase:
        borrowed = False

        def _get_connection(self):
            self.borrowed = True
            raise AssertionError("a Deep connection must never be borrowed")

        def fetch_one(self, *_args, **_kwargs):
            raise AssertionError("Deep SQL must never be executed")

    raw = RawDatabase()
    with pytest.raises(ValueError, match="initialized Plane runtime"):
        PlaneRepositoryContext(repository=object(), legacy_database=raw)
    with pytest.raises(ValueError, match="initialized Plane runtime"):
        repository_from(
            "example",
            plane_runtime=None,
            repositories=None,
            legacy_database=raw,
        )
    assert raw.borrowed is False


def test_temporary_carrier_alias_can_only_expose_existing_plane_objects() -> None:
    runtime = _Runtime()
    carrier = SimpleNamespace(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )

    repository, observed_runtime = repository_from(
        "example",
        plane_runtime=None,
        repositories=None,
        legacy_database=carrier,
    )
    context = PlaneRepositoryContext(
        repository=repository,
        legacy_database=carrier,
    )

    assert repository is runtime.repositories.example
    assert observed_runtime is runtime
    assert context.plane_runtime is runtime


def test_repository_catalog_is_required_when_runtime_has_none() -> None:
    runtime = _Runtime()
    runtime.repositories = None

    with pytest.raises(ValueError, match="repository catalog"):
        repository_from(
            "example",
            plane_runtime=runtime,
            repositories=None,
        )
