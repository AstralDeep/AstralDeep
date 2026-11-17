"""Runs the whole conformance suite through run.py with --backend linux in the bubblewrap harness and requires every case to pass.
It runs only under scripts/exec_harness/run.sh on Linux, so a host without bubblewrap never reaches it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from astral_exec import launch

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTRAL_EXEC_HARNESS") != "1" or sys.platform != "linux",
    reason="runs only under scripts/exec_harness/run.sh on linux",
)

RUN_PY = Path(launch.__file__).resolve().parent / "run.py"


def test_the_cli_suite_passes_every_case_on_the_linux_backend():
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(RUN_PY),
            "conformance",
            "--backend",
            "linux",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=1500,
        check=False,
    )
    rows = [json.loads(line) for line in done.stdout.splitlines()]
    report = "\n".join(f"{row['result']} {row['case']} {row['detail']}" for row in rows)
    assert done.returncode == 0, f"{report}\n{done.stderr}"
    assert rows and {row["result"] for row in rows} == {"PASS"}, report
    names = {row["case"] for row in rows}
    assert {
        "net.io_uring_denied",
        "fs.workspace_exec_loader_denied",
        "refusal.root_node",
    } <= names


def _widen_reads(plan, root):
    outside = f"{root}/outside"
    plan["bwrap"] += ["--ro-bind", outside, outside]
    plan["landlock"]["exec_read"].append(outside)


def _widen_writes(plan, root):
    plan["bwrap"] += ["--bind", root, root]
    plan["landlock"]["workspace"] = root


def _allow_io_uring(plan, root):
    plan["seccomp"]["deny_syscalls"] = [
        name
        for name in plan["seccomp"]["deny_syscalls"]
        if not name.startswith("io_uring")
    ]


def _allow_fork(plan, root):
    plan["seccomp"]["deny_fork"] = False


WEAKENINGS = {
    "reads": (
        _widen_reads,
        ["fs.read_outside_policy_denied", "fs.list_outside_denied"],
    ),
    "writes": (
        _widen_writes,
        [
            "fs.write_outside_workspace_denied",
            "fs.write_home_startup_denied",
            "fs.symlink_escape_denied",
        ],
    ),
    "io_uring": (_allow_io_uring, ["net.io_uring_denied"]),
    "fork": (_allow_fork, ["proc.single_process_by_default"]),
}


@pytest.mark.parametrize("name", sorted(WEAKENINGS))
def test_a_weakened_plan_fails_the_matching_cases(name, monkeypatch, tmp_path):
    from astral_exec import linux
    from astral_exec.conformance import FAIL, run_suite

    weaken, expected = WEAKENINGS[name]
    real = linux.plan

    def weakened(policy, egress_socket):
        plan = real(policy, egress_socket)
        weaken(
            plan,
            os.path.dirname(
                os.path.dirname(os.path.dirname(policy.document["workspace"]))
            ),
        )
        return plan

    monkeypatch.setattr(linux, "plan", weakened)
    results = run_suite("linux", workdir=str(tmp_path), only=expected)
    assert {r.case: r.result for r in results} == {case: FAIL for case in expected}, (
        results
    )
