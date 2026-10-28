"""Pytest plugin for backend/web suites, loaded by run_backend_web_tests.py: it keeps only the
test files of the shard BQ_SUITE_SHARD names, writes the suite's collection inventory once (from
the pytest-xdist controller when workers run it), and prints failures immediately so interrupted
runs stay diagnosable.
"""

import hashlib
import json
import os
from pathlib import Path
import re

import pytest

_XDIST_COLLECTIONS: dict[str, list[str]] = {}
_SHARD = re.compile(r"([1-9][0-9]*)/([1-9][0-9]*)")


def pytest_collection_modifyitems(session, config, items):
    shard = os.environ.get("BQ_SUITE_SHARD")
    if shard is None:
        return
    match = _SHARD.fullmatch(shard)
    if match is None or int(match[1]) > int(match[2]):
        raise ValueError("suite shard must be <index>/<count> with 1 <= index <= count")
    index, count = int(match[1]), int(match[2])
    selected, deselected = [], []
    for item in items:
        path = item.nodeid.split("::", 1)[0]
        bucket = int(hashlib.sha256(path.encode("utf-8")).hexdigest(), 16) % count
        (selected if bucket == index - 1 else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected


def _write_inventory(nodeids):
    target = os.environ.get("BQ_SUITE_INVENTORY_PATH")
    if target is None:
        return
    from _pytest.junitxml import mangle_test_address

    source = os.environ.get("BQ_SOURCE_COMMIT", "")
    suite = os.environ.get("BQ_SUITE_NAME", "")
    if not re.fullmatch(r"[0-9a-f]{40}", source) or not re.fullmatch(r"[A-Za-z0-9_.-]+", suite):
        raise ValueError("suite inventory requires exact source and fixed suite identity")
    tests = []
    for nodeid in nodeids:
        names = mangle_test_address(nodeid)
        tests.append(".".join(names[:-1]) + "::" + names[-1])
    if len(tests) != len(set(tests)):
        raise ValueError("suite inventory contains duplicate JUnit identities")
    with Path(target).open("x", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "suite": suite, "source_commit": source,
                   "tests": sorted(tests)}, stream, sort_keys=True)
        stream.write("\n")


def pytest_collection_finish(session):
    if hasattr(getattr(session, "config", None), "workerinput"):
        return
    _write_inventory([item.nodeid for item in session.items])


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    target = os.environ.get("BQ_SUITE_INVENTORY_PATH")
    if target is None:
        return
    if target in _XDIST_COLLECTIONS:
        if _XDIST_COLLECTIONS[target] != sorted(ids):
            raise ValueError("xdist workers collected different tests")
        return
    _XDIST_COLLECTIONS[target] = sorted(ids)
    _write_inventory(ids)


def pytest_runtest_logreport(report):
    if report.failed:
        print(f"\nFAILED {report.nodeid} ({report.when})\n{report.longrepr}", flush=True)
