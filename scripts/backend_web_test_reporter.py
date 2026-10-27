"""Pytest plugin that writes failing test results immediately during backend/web suites
so long runs stay diagnosable if interrupted, and writes each suite's collection inventory once:
from the pytest-xdist controller when workers run the suite; loaded by run_backend_web_tests.py.
"""

import json
import os
from pathlib import Path
import re

import pytest

_XDIST_COLLECTIONS: dict[str, list[str]] = {}


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
