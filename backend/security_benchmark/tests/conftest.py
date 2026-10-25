"""Pins disposable runtime roots for the benchmark suite, which constructs real
orchestrators; the CI gate always sets these roots explicitly for test processes.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_runtime_roots():
    root = tempfile.mkdtemp(prefix="security-benchmark-roots-")
    previous = {
        name: os.environ.get(name)
        for name in ("PERSONAL_AGENT_ARTIFACT_ROOT", "ATTACHMENT_UPLOAD_ROOT", "DELEGATION_REQUIRED")
    }
    os.environ["PERSONAL_AGENT_ARTIFACT_ROOT"] = os.path.join(root, "agent-artifacts")
    os.environ["ATTACHMENT_UPLOAD_ROOT"] = os.path.join(root, "blobs")
    os.environ["DELEGATION_REQUIRED"] = "false"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
