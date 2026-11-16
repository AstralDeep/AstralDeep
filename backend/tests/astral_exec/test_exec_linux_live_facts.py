"""Probes the real machine and selects a backend, which only a Linux host with bubblewrap, Landlock ABI 6 and user namespaces can answer.
It runs only when ASTRAL_EXEC_HARNESS=1, which only scripts/exec_harness/run.sh sets, so a host without bubblewrap never runs it.
"""

from __future__ import annotations

import os
import sys

import pytest
from astral_exec import Backend, Policy, select
from astral_exec.facts import probe
from exec_vectors import minimal_policy

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTRAL_EXEC_HARNESS") != "1" or sys.platform != "linux",
    reason="runs only under scripts/exec_harness/run.sh on linux",
)


def test_the_real_probe_finds_landlock_bwrap_and_user_namespaces():
    facts = probe()
    assert facts["platform"] == "linux"
    assert facts["landlock_abi"] >= 6
    assert facts["bwrap"] is True
    assert facts["userns"] is True
    assert facts["sandbox_exec"] is False and facts["app_sandboxed"] is False


def test_the_real_facts_select_the_linux_backend(tmp_path):
    facts = probe()
    data = tmp_path / "data"
    workspace = data / "exec" / "job"
    home = tmp_path / "home"
    for directory in (workspace, home):
        directory.mkdir(parents=True)
    policy = Policy.load(
        minimal_policy(workspace=str(workspace)), "linux", str(data), str(home)
    )
    outcome = select(policy, facts, str(data))
    assert outcome == Backend("linux")
    assert outcome.bwrap == facts.bwrap_path
    assert outcome.bwrap in ("/usr/bin/bwrap", "/bin/bwrap")
