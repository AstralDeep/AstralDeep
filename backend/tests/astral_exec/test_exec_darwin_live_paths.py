"""Checks the F_GETPATH canonicalization against a real macOS filesystem, where a stated case differs from the on-disk case.
It lives in its own file so the Linux harness never has to skip it.
"""

from __future__ import annotations

import sys

import pytest
from astral_exec import Policy, PolicyError
from astral_exec.paths import canonical_path
from exec_vectors import minimal_policy

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="F_GETPATH exists only on macOS"
)


def test_darwin_canonical_path_returns_the_on_disk_case(tmp_path):
    real = tmp_path / "MixedCase"
    real.mkdir()
    (tmp_path / "ws").mkdir()
    probe = str(tmp_path / "mixedcase")
    assert canonical_path(probe, "darwin") == str(real)
    with pytest.raises(PolicyError) as caught:
        Policy.load(
            minimal_policy(reads=[probe], workspace=str(tmp_path / "ws")), "darwin"
        )
    assert caught.value.code == "policy.path_invalid"
