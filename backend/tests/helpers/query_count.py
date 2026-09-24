"""count_queries() wraps a Database's execute/fetch_one/fetch_all to tally and record
round trips for DB query-budget tests, reverting the wrap on exit.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import List

_WRAPPED_METHODS = ("execute", "fetch_one", "fetch_all")
_MISSING = object()


@dataclass
class QueryCounter:
    count: int = 0
    queries: List[str] = field(default_factory=list)


@contextmanager
def count_queries(db):
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
