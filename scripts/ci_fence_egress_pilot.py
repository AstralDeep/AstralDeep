"""Declares the reviewed OpenAI Fence egress pilot for the pure lint CI job and verifies
candidate workflow text against that declaration. The module generates the exact pilot
patch and evaluates deterministic egress fixtures mirroring the reviewed Fence block-mode
semantics; it performs no enforcement, and live qualification happens only in the
disposable-runner diagnostic inside the piloted job.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import ipaddress
import json
import re
import sys

FENCE_ACTION = "openai/fence"
REVIEWED_BUNDLE_SHA = "abc5790eac3445fe3854eea6b506aa7d31bed67c"
REVIEWED_VERSION = "v0.10.5"
SOURCE_MAIN_SHA = "567ebd3787467b0ea8eaddc00f5ad9247ace585b"
PILOT_JOB = "lint"
PILOT_MODE = "block"
JOB_PERMISSIONS = "contents: read"
ACTION_PIN = f"{FENCE_ACTION}@{REVIEWED_BUNDLE_SHA} # {REVIEWED_VERSION}"

ALLOWED_ENDPOINTS = (
    ("github.com", 443, "git checkout transport and release resolution"),
    ("codeload.github.com", 443, "pinned action bundle fetch"),
    ("raw.githubusercontent.com", 443, "setup-python versions manifest"),
    ("objects.githubusercontent.com", 443, "setup-python toolchain artifact"),
    ("release-assets.githubusercontent.com", 443, "setup-python toolchain artifact"),
    ("pypi.org", 443, "pip index for the hash-locked lint tooling"),
    ("files.pythonhosted.org", 443, "pip package artifacts"),
)
FORBIDDEN_INPUTS = ("unsafe_preserve",)
DIAGNOSTIC_POSITIVE_PROBES = ("https://pypi.org/simple/", "https://github.com/")
DIAGNOSTIC_DENIED_PROBES = (
    "https://example.com/",
    "https://1.1.1.1/",
    "--resolve pypi.org:443:127.0.0.1 https://pypi.org/simple/",
)
CHECKOUT_USES_PREFIX = "uses: actions/checkout@"
CHECKOUT_ACTION = "actions/checkout@"
LOCK_INSTALL = (
    "python -m pip install --require-hashes -r tooling/python-ci/requirements.lock.txt"
)

FENCE_STEP_NAME = "Install the reviewed Fence egress firewall in block mode"
DIAGNOSTIC_STEP_NAME = (
    "Prove allowlisted egress and deny excluded and bypassed destinations"
)

RULE_ALLOW = "allowlisted_destination"
RULE_DESTINATION = "destination_not_allowed"
RULE_BYPASS_ADDRESS = "address_literal_bypass_denied"
RULE_BYPASS_DNS = "dns_resolution_bypass_denied"
RULE_TRANSPORT = "insecure_transport_denied"
RULE_FAIL_CLOSED = "fail_closed_agent_integrity"

STATE_ABSENT = "absent"
STATE_CONFORMING = "conforming"
STATE_NONCONFORMING = "nonconforming"

_JOB_PATTERN = r"(?ms)^  {name}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)"


class PilotPatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectionAttempt:
    host: str
    port: int
    scheme: str = "https"
    resolved_addresses: tuple[str, ...] = ()
    verified_addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str


@dataclass(frozen=True)
class PilotEvaluation:
    state: str
    reasons: tuple[str, ...] = ()


def _job_slice(workflow: str, job_name: str) -> str:
    match = re.search(_JOB_PATTERN.format(name=re.escape(job_name)), workflow)
    if not match:
        raise PilotPatchError(f"workflow job is missing: {job_name}")
    return match.group("body")


def _normalize_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def decide_egress(
    attempt: ConnectionAttempt, agent_integrity: str = "verified"
) -> Decision:
    if agent_integrity != "verified":
        return Decision(False, RULE_FAIL_CLOSED)
    if attempt.scheme != "https":
        return Decision(False, RULE_TRANSPORT)
    host = _normalize_host(attempt.host)
    if _is_ip_literal(host):
        return Decision(False, RULE_BYPASS_ADDRESS)
    if not any(
        host == _normalize_host(entry[0]) and attempt.port == entry[1]
        for entry in ALLOWED_ENDPOINTS
    ):
        return Decision(False, RULE_DESTINATION)
    if attempt.resolved_addresses:
        verified = {
            ipaddress.ip_address(address) for address in attempt.verified_addresses
        }
        resolved = {
            ipaddress.ip_address(address) for address in attempt.resolved_addresses
        }
        if not verified or resolved - verified:
            return Decision(False, RULE_BYPASS_DNS)
    return Decision(True, RULE_ALLOW)


def allowlist_block() -> str:
    return "".join(
        f"            {host}:{port}\n" for host, port, _ in ALLOWED_ENDPOINTS
    )


def _diagnostic_script() -> str:
    positives = "\n".join(
        f"          probe {url}" for url in DIAGNOSTIC_POSITIVE_PROBES
    )
    lines = [
        "          set -euo pipefail",
        "          probe() {",
        "            curl -fsS --max-time 8 --proto '=https' --tlsv1.2 \"$@\" -o /dev/null",
        "          }",
        positives,
    ]
    for index, url in enumerate(DIAGNOSTIC_DENIED_PROBES):
        lines.append("          if probe " + url + "; then")
        lines.append(
            f"            echo '::error::Fence pilot diagnostic: denied destination {index + 1} was reachable'"
        )
        lines.append("            exit 1")
        lines.append("          fi")
    lines.append(
        "          echo 'Fence pilot diagnostic: allowlisted egress allowed, excluded and bypassed destinations denied.'"
    )
    return "\n".join(lines)


def render_pilot_steps() -> str:
    return (
        f"      - name: {FENCE_STEP_NAME}\n"
        "        # Reviewed provenance, approved endpoints, residual GitHub/Azure\n"
        "        # metadata exceptions, and rollback: docs/ci-egress-fence-pilot.md.\n"
        f"        uses: {ACTION_PIN}\n"
        "        with:\n"
        f"          mode: {PILOT_MODE}\n"
        "          allowlist: |\n"
        f"{allowlist_block()}"
        f"      - name: {DIAGNOSTIC_STEP_NAME}\n"
        "        run: |\n"
        f"{_diagnostic_script()}\n"
    )


def _step_blocks(job: str) -> list[str]:
    starts = [match.start() for match in re.finditer(r"(?m)^      - ", job)]
    return [
        job[start : (end if end is not None else len(job))]
        for start, end in zip(starts, starts[1:] + [None])
    ]


def _step_uses(block: str) -> str | None:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("- uses: "):
            stripped = stripped[2:]
        if stripped.startswith("uses: "):
            return stripped
    return None


def _checkout_index(job: str) -> int:
    lines = job.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith("- uses: ") and CHECKOUT_USES_PREFIX in line:
            return index
    raise PilotPatchError("lint job has no checkout step")


def evaluate_workflow(workflow: str) -> PilotEvaluation:
    reasons: list[str] = []
    try:
        job = _job_slice(workflow, PILOT_JOB)
    except PilotPatchError as error:
        return PilotEvaluation(STATE_NONCONFORMING, (str(error),))
    blocks = _step_blocks(job)
    fence_blocks = [
        block
        for block in blocks
        if (uses := _step_uses(block)) and uses.startswith(f"uses: {FENCE_ACTION}@")
    ]
    if not fence_blocks:
        return PilotEvaluation(STATE_ABSENT)
    if len(fence_blocks) != 1:
        return PilotEvaluation(
            STATE_NONCONFORMING, ("fence action must appear exactly once",)
        )
    if _step_uses(fence_blocks[0]) != f"uses: {ACTION_PIN}":
        reasons.append(
            "fence action is not the reviewed full-SHA pin with version comment"
        )
    fence_index = blocks.index(fence_blocks[0])
    checkout_index = next(
        (
            index
            for index, block in enumerate(blocks)
            if (uses := _step_uses(block))
            and uses.startswith("uses: " + CHECKOUT_ACTION)
        ),
        None,
    )
    if checkout_index is None or fence_index != 0 or not fence_index < checkout_index:
        reasons.append(
            "the fence action must be the first step and run before checkout"
        )
    diagnostic_index = next(
        (
            index
            for index, block in enumerate(blocks)
            if f"name: {DIAGNOSTIC_STEP_NAME}" in block
        ),
        None,
    )
    if diagnostic_index is None or not fence_index < diagnostic_index < (
        checkout_index or 0
    ):
        reasons.append(
            "the bounded diagnostic must run between the fence action and checkout"
        )
    if "unsafe_preserve" in job:
        reasons.append("unsafe_preserve is forbidden in block mode")
    if re.search(r"(?m)^\s*mode:\s*audit\s*$", job):
        reasons.append("audit mode enforcement must not be configured")
    declared = allowlist_block()
    allowlist_match = re.search(
        r"(?m)^          allowlist: \|\n((?:            [^\n]*\n)+)", job
    )
    if not allowlist_match or allowlist_match.group(1) != declared:
        reasons.append("the allowlist must equal the reviewed endpoint set exactly")
    if f"          mode: {PILOT_MODE}\n" not in job:
        reasons.append("the fence action must run in explicit block mode")
    if f"    permissions:\n      {JOB_PERMISSIONS}\n" not in job:
        reasons.append("the job must pin minimal permissions explicitly")
    if LOCK_INSTALL not in job:
        reasons.append("the hash-locked tooling install step must be retained")
    if "cache-dependency-path: tooling/python-ci/requirements.lock.txt" not in job:
        reasons.append("the tooling cache pin must be retained")
    if "run: ruff check ." not in job:
        reasons.append("the lint step must be retained")
    if (
        "secrets." in job
        or "docker" in job.lower()
        or re.search(r"(?m)^\s*sudo\s", job)
    ):
        reasons.append("the pilot job must not gain secrets, docker, or sudo")
    timeout = re.search(r"(?m)^    timeout-minutes: (\d+)$", job)
    if not timeout or int(timeout.group(1)) > 30:
        reasons.append("the job must retain a timeout-minutes budget of at most 30")
    for probe in DIAGNOSTIC_POSITIVE_PROBES + DIAGNOSTIC_DENIED_PROBES:
        if probe not in job:
            reasons.append(f"diagnostic probe missing: {probe}")
    if "::error::Fence pilot diagnostic:" not in job:
        reasons.append("diagnostic denials must fail visibly")
    if reasons:
        return PilotEvaluation(STATE_NONCONFORMING, tuple(reasons))
    return PilotEvaluation(STATE_CONFORMING)


def pilot_patch(workflow: str) -> str:
    evaluation = evaluate_workflow(workflow)
    if evaluation.state == STATE_CONFORMING:
        return workflow
    if evaluation.state != STATE_ABSENT:
        raise PilotPatchError(
            "the pilot patch applies only to an un-piloted workflow: "
            + "; ".join(evaluation.reasons)
        )
    job_match = re.search(_JOB_PATTERN.format(name=re.escape(PILOT_JOB)), workflow)
    if job_match is None:  # pragma: no cover - evaluate_workflow proved the job exists
        raise PilotPatchError(f"workflow job is missing: {PILOT_JOB}")
    job = job_match.group("body")
    permission_anchor = "    timeout-minutes: 10\n"
    if permission_anchor not in job:
        raise PilotPatchError("lint job timeout anchor is missing")
    job = job.replace(
        permission_anchor,
        permission_anchor + f"    permissions:\n      {JOB_PERMISSIONS}\n",
        1,
    )
    checkout = _checkout_index(job)
    lines = job.splitlines(keepends=True)
    inserted = render_pilot_steps()
    patched_job = "".join(lines[:checkout]) + inserted + "".join(lines[checkout:])
    return (
        workflow[: job_match.start("body")]
        + patched_job
        + workflow[job_match.end("body") :]
    )


def unified_patch(workflow: str) -> str:
    patched = pilot_patch(workflow)
    if patched == workflow:
        return ""
    return "".join(
        difflib.unified_diff(
            workflow.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=".github/workflows/ci.yml",
            tofile=".github/workflows/ci.yml (fence pilot)",
        )
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: ci_fence_egress_pilot.py (--check|--patch) WORKFLOW"
    if len(argv) != 2 or argv[0] not in ("--check", "--patch"):
        print(usage, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        workflow = handle.read()
    if argv[0] == "--check":
        evaluation = evaluate_workflow(workflow)
        print(
            json.dumps({"state": evaluation.state, "reasons": list(evaluation.reasons)})
        )
        return 0 if evaluation.state in (STATE_CONFORMING, STATE_ABSENT) else 1
    print(unified_patch(workflow), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
