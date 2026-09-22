"""Write failures immediately, so long suite runs remain diagnosable."""

import json
import os
from pathlib import Path
import re


def pytest_collection_finish(session):
    """Record the expected cases before any test executes, using JUnit identities."""
    target = os.environ.get("BQ_SUITE_INVENTORY_PATH")
    if target is None:
        return
    from _pytest.junitxml import mangle_test_address

    source = os.environ.get("BQ_SOURCE_COMMIT", "")
    suite = os.environ.get("BQ_SUITE_NAME", "")
    if not re.fullmatch(r"[0-9a-f]{40}", source) or not re.fullmatch(r"[A-Za-z0-9_.-]+", suite):
        raise ValueError("suite inventory requires exact source and fixed suite identity")
    tests = []
    for item in session.items:
        names = mangle_test_address(item.nodeid)
        tests.append(".".join(names[:-1]) + "::" + names[-1])
    if len(tests) != len(set(tests)):
        raise ValueError("suite inventory contains duplicate JUnit identities")
    with Path(target).open("x", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "suite": suite, "source_commit": source,
                   "tests": sorted(tests)}, stream, sort_keys=True)
        stream.write("\n")


def pytest_runtest_logreport(report):
    if report.failed:
        print(f"\nFAILED {report.nodeid} ({report.when})\n{report.longrepr}", flush=True)
