"""Query-count instrumentation for DB round-trip budget tests (feature 052).

Usage:

    from tests.helpers.query_count import count_queries

    with count_queries(manager.db) as counter:
        manager.do_something()
    assert counter.count == 1
    assert "FROM chats" in counter.queries[0]

``count_queries`` wraps the instance's ``execute``/``fetch_one``/``fetch_all``
methods, so every database round trip made through that ``Database`` object
(including calls issued from other threads, e.g. ``asyncio.to_thread``) is
counted and its SQL text recorded. The wrapping is reverted on exit.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import List

_WRAPPED_METHODS = ("execute", "fetch_one", "fetch_all")
_MISSING = object()


@dataclass
class QueryCounter:
    """Live tally of database round trips made inside a count_queries block."""

    count: int = 0
    queries: List[str] = field(default_factory=list)


@contextmanager
def count_queries(db):
    """Count every execute/fetch_one/fetch_all round trip made through ``db``.

    Yields a :class:`QueryCounter` whose ``count`` and ``queries`` update as
    calls happen. Accepts any object exposing the bounded query-counting
    call surface. Nesting is safe; the instance is restored on exit.
    """
    counter = QueryCounter()
    saved = {}

    def _make_wrapper(original):
        def wrapper(query, params=()):
            counter.count += 1
            counter.queries.append(query)
            return original(query, params)
        return wrapper

    for name in _WRAPPED_METHODS:
        saved[name] = db.__dict__.get(name, _MISSING)
        setattr(db, name, _make_wrapper(getattr(db, name)))
    try:
        yield counter
    finally:
        for name, prior in saved.items():
            if prior is _MISSING:
                delattr(db, name)
            else:
                setattr(db, name, prior)
