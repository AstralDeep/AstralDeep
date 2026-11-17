"""The backend conformance suite: it selects the backend the machine offers, runs every case against it with loopback fixtures and reports one PASS, FAIL or REFUSED line per case.
run.py conformance calls main(), and the exit status is 0 only when every case passed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astral_exec import Policy
from astral_exec.errors import Refusal
from astral_exec.facts import probe
from astral_exec.select import select
from . import cases as cases_module
from .context import Context, Failed
from .fixtures import Fixtures

PASS, FAIL, REFUSED = "PASS", "FAIL", "REFUSED"
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
BACKENDS = ("auto", "seatbelt", "linux")
BACKEND_PLATFORM = {"seatbelt": "darwin", "linux": "linux"}
PLATFORM_BACKEND = {value: key for key, value in BACKEND_PLATFORM.items()}


@dataclass(frozen=True)
class Result:
    case: str
    result: str
    detail: str = ""

    def line(self, as_json: bool) -> str:
        if as_json:
            return json.dumps(
                {"case": self.case, "result": self.result, "detail": self.detail}
            )
        return f"{self.result} {self.case}" + (
            f": {self.detail}" if self.detail else ""
        )


def choose(requested: str, facts: Mapping[str, Any], workdir: str) -> Refusal | str:
    name = PLATFORM_BACKEND.get(facts["platform"])
    if name is None:
        return Refusal(
            "exec.backend_unavailable",
            f"No confinement backend exists for {facts['platform']}.",
        )
    if requested not in ("auto", name):
        return Refusal(
            "exec.backend_unavailable",
            f"The {requested} backend was requested, and this machine can only offer {name}.",
        )
    data = f"{workdir}/data"
    os.makedirs(f"{data}/exec/probe", exist_ok=True)
    policy = Policy.load(
        {
            "v": 1,
            "reads": [],
            "workspace": f"{data}/exec/probe",
            "hosts": [],
            "limits": {
                "memory_bytes": 1 << 30,
                "processes": 1,
                "open_files": 256,
                "file_bytes": 1 << 24,
            },
            "time": {"wall_ms": 60000},
        },
        facts["platform"],
        data,
        f"{workdir}/home",
    )
    chosen = select(policy, facts, data)
    return chosen if isinstance(chosen, Refusal) else name


def execute(case: cases_module.Case, ctx: Context, refusal: Refusal | None) -> Result:
    if case.needs_backend and refusal is not None:
        return Result(case.name, REFUSED, refusal.message)
    try:
        case.run(ctx)
    except Failed as failure:
        return Result(case.name, FAIL, str(failure))
    except Exception as error:
        return Result(case.name, FAIL, f"{type(error).__name__}: {error}")
    return Result(case.name, PASS)


def run_suite(
    backend: str = "auto",
    workdir: str | None = None,
    only: Sequence[str] = (),
    facts: Mapping[str, Any] | None = None,
) -> list[Result]:
    own = workdir is None
    root = os.path.realpath(
        tempfile.mkdtemp(prefix="astral-conformance-") if own else workdir
    )
    fixtures = None
    try:
        for directory in ("home", "keys"):
            os.makedirs(f"{root}/{directory}", exist_ok=True)
        os.makedirs(f"{root}/readable", exist_ok=True)
        facts = probe() if facts is None else facts
        refusal = choose(backend, facts, root)
        fixtures = Fixtures(Path(root, "readable"), Path(root, "keys"))
        ctx = Context(facts, root, fixtures)
        chosen = [
            case
            for case in cases_module.CASES
            if facts["platform"] in case.platforms
            and (not only or any(case.name.startswith(p) for p in only))
        ]
        return [
            execute(case, ctx, refusal if isinstance(refusal, Refusal) else None)
            for case in chosen
        ]
    finally:
        if fixtures is not None:
            fixtures.stop()
        if own:
            shutil.rmtree(root, ignore_errors=True)


def status(results: Sequence[Result]) -> int:
    if any(result.result == FAIL for result in results):
        return EXIT_FAIL
    if not results or any(result.result == REFUSED for result in results):
        return EXIT_REFUSED
    return 0


def main(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="run.py conformance")
    parser.add_argument("--backend", choices=BACKENDS, default="auto")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="run only cases whose name starts with this prefix",
    )
    try:
        options = parser.parse_args(list(args))
    except SystemExit as stop:
        return EXIT_USAGE if stop.code else 0
    results = run_suite(options.backend, only=options.case)
    for result in results:
        print(result.line(options.json), flush=True)
    outcome = status(results)
    print(f"conformance exit {outcome}", file=sys.stderr)
    return outcome
