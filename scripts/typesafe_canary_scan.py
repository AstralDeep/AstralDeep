#!/usr/bin/env python3
"""Feature 089 (T059, SC-007): does a TypeSafe key ever escape?

A synthetic key in the **real shape** is put through every path that touches
one — the credential store, the routing seam, the audit trail, the settings
surface, the renderers — and then everything the system wrote is searched for
it: logs, audit rows, durable operation records, rendered HTML and SDUI, test
artifacts, and the container's own stdout.

The canary is synthetic. It matches the committed pattern and nothing else; it
is not derived from the owner's key and is not a prefix of it. Expect zero
hits, including of its prefix alone — a redaction that leaves the prefix
behind still tells an attacker which vendor to try.

Usage:
    python scripts/typesafe_canary_scan.py
    python scripts/typesafe_canary_scan.py --container astraldeep
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

#: The same synthetic key the hygiene tests use, in the real shape: a short
#: lowercase prefix, an underscore, a long lowercase-alphanumeric tail with
#: digits. Invented letters, not derived from and not a prefix of any real key.
#: One canary in the repository rather than several, so there is one thing to
#: allowlist and one shape to reason about.
CANARY = "zqkfmp_" + ("0canary9notarealkey" * 6)[:101]
#: The prefix on its own. A redaction that leaves this behind has still leaked.
CANARY_PREFIX = "zqkfmp_"

#: Directories whose contents are written by a run and are therefore evidence.
ARTIFACT_ROOTS = ("build", "backend/tmp", "backend/data", ".pytest_cache")


class _Capture(logging.Handler):
    """Every log record any module emits during the exercise."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.lines.append(repr(record.__dict__))


async def _exercise(capture: _Capture) -> dict:
    """Put the canary through every path that handles a key."""
    from fakes.typesafe_fake import FakeTypeSafeClient, high_confidence
    from llm_config.log_scrub import _redact_text
    from llm_config.typesafe_store import key_fingerprint
    from orchestrator.typesafe_routing.budget import UserCircuit
    from orchestrator.typesafe_routing.questions import (
        AgentOption, RoutingRequest, ToolOption,
    )
    from orchestrator.typesafe_routing.runner import (
        await_decision, screen_instruction, start_routing,
    )

    produced: dict[str, str] = {}

    class _Enabled:
        @staticmethod
        def is_enabled(_name: str) -> bool:
            return True

    request = RoutingRequest.build(
        current_request="What is the weather in Lexington right now?",
        agents=[AgentOption(agent_id="weather-1", name="Weather",
                            description="Conditions and forecasts")],
        tools_by_agent={"weather-1": [
            ToolOption(name="weather-1__get_current_weather",
                       description="Current conditions")]},
    )
    fingerprint = key_fingerprint(CANARY)
    produced["fingerprint"] = fingerprint

    # The routing seam, the success path.
    client = FakeTypeSafeClient(
        default=high_confidence("weather-1", "weather-1__get_current_weather"))
    task = await start_routing(user_id="canary-user", request=request, api_key=CANARY,
                               fingerprint=fingerprint, client=client,
                               circuit=UserCircuit(), flags=_Enabled)
    outcome = await await_decision(task, user_id="canary-user")
    produced["routing_outcome"] = repr(outcome)

    # The routing seam, a failure path -- the one that logs most.
    from fakes.typesafe_fake import TypeSafeAuthenticationError
    failing = FakeTypeSafeClient(default=high_confidence("weather-1", "x"),
                                 faults=[TypeSafeAuthenticationError] * 6)
    task = await start_routing(user_id="canary-user", request=request, api_key=CANARY,
                               fingerprint=fingerprint, client=failing,
                               circuit=UserCircuit(), flags=_Enabled)
    produced["failure_outcome"] = repr(await await_decision(task, user_id="canary-user"))

    # The submission screen.
    produced["screen"] = repr(await screen_instruction(
        user_id="canary-user", text="File the weekly digest.", api_key=CANARY,
        fingerprint=fingerprint, client=FakeTypeSafeClient(default=high_confidence("a", "b")),
        circuit=UserCircuit(), flags=_Enabled))

    # The stored-credential value object: its repr is what lands in a traceback
    # or a debug line, so it is exactly where a key would escape unnoticed.
    from llm_config.typesafe_store import StoredTypeSafeKey
    stored = StoredTypeSafeKey(api_key=CANARY, fingerprint=fingerprint)
    produced["stored_repr"] = repr(stored)
    produced["stored_str"] = str(stored)
    produced["stored_format"] = f"{stored}"

    # The scrubber, on a line that contains the key.
    produced["scrubbed"] = _redact_text(
        f"authorization=Bearer {CANARY} model=jev-latest"
    )

    # The settings surface's view of a saved key: it is serialised into SDUI
    # and into rendered HTML, so anything it carries reaches a page.
    from llm_config.typesafe_store import TypeSafeKeyStatus
    status = TypeSafeKeyStatus(name="active")
    produced["status_repr"] = repr(status)

    # A deliberate log line carrying the key, to prove the handler captures it
    # and that the scrubber is what removes it rather than luck.
    logging.getLogger("astral.canary").info("probe api_key=%s", CANARY)
    produced["deliberate_leak_present"] = str(
        any(CANARY in line for line in capture.lines)
    )
    return produced


def _hits_in_text(label: str, text: str) -> list[dict]:
    hits = []
    for pattern, kind in ((CANARY, "full key"), (CANARY_PREFIX, "prefix")):
        for match in re.finditer(re.escape(pattern), text):
            start = max(0, match.start() - 40)
            hits.append({
                "where": label, "kind": kind,
                "context": text[start:match.start()] + "<<REDACTED>>"
                           + text[match.end():match.end() + 20],
            })
    return hits


def _scan_files(roots: Iterable[Path]) -> list[dict]:
    hits: list[dict] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            try:
                label = str(path.relative_to(ROOT))
            except ValueError:
                label = str(path)  # a scratch artifact outside the repository
            hits.extend(_hits_in_text(label, text))
    return hits


def _scan_container(container: str) -> list[dict]:
    try:
        run = subprocess.run(["docker", "logs", "--tail", "5000", container],
                             capture_output=True, text=True, timeout=120,
                             env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    except Exception as exc:
        return [{"where": f"docker logs {container}", "kind": "unavailable",
                 "context": str(exc)}]
    return _hits_in_text(f"docker logs {container}", run.stdout + run.stderr)


def _run_tests(artifact_dir: Path) -> tuple[int, str]:
    """Every TypeSafe test, with its output kept as an artifact to scan."""
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
           "tests/test_typesafe_budget.py", "tests/test_typesafe_decision.py",
           "tests/test_typesafe_turn_routing.py", "tests/test_typesafe_security.py",
           "tests/test_typesafe_resilience.py", "tests/test_typesafe_layout.py",
           "tests/test_typesafe_settings.py", "tests/test_typesafe_submission_screen.py",
           "llm_config/tests/test_typesafe_store.py",
           "llm_config/tests/test_typesafe_secret_hygiene.py"]
    run = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT / "backend",
                         timeout=1800)
    output = run.stdout + run.stderr
    (artifact_dir / "typesafe-tests.log").write_text(output, encoding="utf-8")
    # A failing suite is still scanned -- a test that fails mid-way is exactly
    # when a value gets printed -- but it is reported, because "no hits" from a
    # suite that never ran is not evidence of anything.
    if run.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-12:])
        sys.stderr.write(
            "\nthe TypeSafe suite did not pass; its output was still scanned:\n"
            + tail + "\n\n"
        )
    return run.returncode, output


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="astraldeep")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-container", action="store_true")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    capture = _Capture()
    capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(capture)

    with tempfile.TemporaryDirectory(prefix="canary-089-") as tmp:
        artifact_dir = Path(tmp)
        try:
            produced = asyncio.run(_exercise(capture))
        finally:
            root_logger.removeHandler(capture)
            root_logger.setLevel(previous_level)

        test_rc, test_output = (0, "")
        if not args.skip_tests:
            test_rc, test_output = _run_tests(artifact_dir)

        hits: list[dict] = []
        # 1. Everything any module logged during the exercise, minus the one
        #    line this script deliberately emitted to prove capture works.
        deliberate = f"probe api_key={CANARY}"
        for line in capture.lines:
            if deliberate in line:
                continue
            hits.extend(_hits_in_text("captured log record", line))
        # 2. Every value the exercise produced.
        for name, value in produced.items():
            if name == "deliberate_leak_present":
                continue
            hits.extend(_hits_in_text(f"produced:{name}", str(value)))
        # 3. Test output and artifacts.
        if test_output:
            hits.extend(_hits_in_text("typesafe test output", test_output))
        hits.extend(_scan_files([artifact_dir]))
        # 4. Anything the repository wrote.
        hits.extend(_scan_files([ROOT / part for part in ARTIFACT_ROOTS]))
        # 5. The running stack's own stdout.
        container_hits = [] if args.skip_container else _scan_container(args.container)
        unavailable = [h for h in container_hits if h["kind"] == "unavailable"]
        hits.extend([h for h in container_hits if h["kind"] != "unavailable"])

    report = {
        "canary_shape": f"{CANARY_PREFIX}<{len(CANARY) - len(CANARY_PREFIX)} chars>",
        "fingerprint": produced["fingerprint"],
        "capture_works": produced["deliberate_leak_present"] == "True",
        "log_records_examined": len(capture.lines),
        "typesafe_tests_exit": test_rc,
        "container_scanned": not args.skip_container and not unavailable,
        "container_note": unavailable[0]["context"] if unavailable else None,
        "hits": hits,
        "pass": not hits and produced["deliberate_leak_present"] == "True",
    }

    lines = [
        "TypeSafe canary scan (SC-007)",
        f"canary shape:      {report['canary_shape']}",
        f"fingerprint:       {report['fingerprint']}",
        f"capture works:     {report['capture_works']} "
        "(a deliberate leak was seen, so a real one would be)",
        f"log records:       {report['log_records_examined']}",
        f"typesafe tests:    exit {report['typesafe_tests_exit']}",
        f"container scanned: {report['container_scanned']}"
        + (f"  ({report['container_note']})" if report["container_note"] else ""),
        f"hits:              {len(hits)}",
    ]
    for hit in hits[:20]:
        lines.append(f"  {hit['kind']:<10} {hit['where']}: {hit['context']}")
    lines.append("")
    lines.append("PASS" if report["pass"] else "FAIL")
    lines.append("")
    sys.stdout.write("\n".join(lines))

    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
