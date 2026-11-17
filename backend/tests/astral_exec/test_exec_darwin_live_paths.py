"""Checks the F_GETPATH canonicalization against a real macOS filesystem, where a stated case differs from the on-disk case.
It runs only when ASTRAL_EXEC_HARNESS=1 is set by hand on a Mac, and every other run skips it.
"""

from __future__ import annotations

import os
import sys

import pytest
from astral_exec import Policy, PolicyError
from astral_exec.paths import canonical_path
from exec_vectors import minimal_policy

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTRAL_EXEC_HARNESS") != "1" or sys.platform != "darwin",
    reason="set ASTRAL_EXEC_HARNESS=1 on a Mac to run this",
)


def test_darwin_canonical_path_returns_the_on_disk_case(tmp_path):
    real = tmp_path / "MixedCase"
    real.mkdir()
    (tmp_path / "ws").mkdir()
    probe = str(tmp_path / "mixedcase")
    assert canonical_path(probe, "darwin") == str(real)
    with pytest.raises(PolicyError) as caught:
        Policy.load(
            minimal_policy(reads=[probe], workspace=str(tmp_path / "ws")),
            "darwin",
            str(tmp_path),
            str(tmp_path),
        )
    assert caught.value.code == "policy.path_invalid"
