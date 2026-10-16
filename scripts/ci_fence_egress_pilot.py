"""Checks the actual lint workflow's pinned Fence and immutable reviewed-helper boundary.
Missing merged-helper identities fail closed; network enforcement belongs to the actual action and trusted verifier.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import textwrap

FENCE_ACTION = "openai/fence"
REVIEWED_BUNDLE_SHA = "abc5790eac3445fe3854eea6b506aa7d31bed67c"
REVIEWED_VERSION = "v0.10.5"
ACTION_PIN = f"{FENCE_ACTION}@{REVIEWED_BUNDLE_SHA} # {REVIEWED_VERSION}"
INVOCATION = "astral-lint-${{ github.run_id }}-${{ github.run_attempt }}"
FIXTURE_ID = "${{ steps.fence_peer.outputs.fixture_id }}"
PEER_FILE = "ci_network_fixture.py"
VERIFIER_FILE = "ci_fence_fixture_verifier.py"
MAX_HELPER_BYTES = 262144
ALLOWED_ENDPOINTS = (
    "github.com:443",
    "codeload.github.com:443",
    "raw.githubusercontent.com:443",
    "objects.githubusercontent.com:443",
    "release-assets.githubusercontent.com:443",
    "pypi.org:443",
    "files.pythonhosted.org:443",
    "ip 192.0.2.2 tcp 18443",
)
LOCK_INSTALL = "python -m pip install --require-hashes -r tooling/python-ci/requirements.lock.txt"
STATE_ABSENT = "absent"
STATE_CONFORMING = "conforming"
STATE_NONCONFORMING = "nonconforming"
STATE_MISSING_TRUST = "missing_trust"


@dataclass(frozen=True)
class ReviewedHelpers:
    commit: str
    peer_sha256: str
    verifier_sha256: str

    def identities(self) -> tuple[tuple[str, str], ...]:
        values = ((PEER_FILE, self.peer_sha256), (VERIFIER_FILE, self.verifier_sha256))
        if (not isinstance(self.commit, str) or not re.fullmatch(r"[0-9a-f]{40}", self.commit)
                or self.commit == "0" * 40 or any(
                    not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or digest == "0" * 64 for _name, digest in values)):
            raise ValueError("reviewed merged-main helper identities are invalid")
        return values


REVIEWED_HELPERS: ReviewedHelpers | None = ReviewedHelpers(
    "1f76a8c820f1709865106af0443aff5982fd4930",
    "23c0ff99e9cb3a8e0889767c9ef75f8b00c85aaf981c57634d3eecc4a134c240",
    "f3e72a05048b50f55f4115f931137fb954a0573b40d37d35bd0b14485600d201",
)


@dataclass(frozen=True)
class PilotEvaluation:
    state: str
    reasons: tuple[str, ...] = ()


def _source_prefix(helpers: ReviewedHelpers) -> str:
    return (
        "import hashlib, json, os, re, stat, sys, types\n"
        f"identities = {helpers.identities()!r}\n"
        f"limit = {MAX_HELPER_BYTES}\n"
    )


def start_source(helpers: ReviewedHelpers) -> str:
    return _source_prefix(helpers) + f"commit = {helpers.commit!r}\n" + textwrap.dedent('''\
        import urllib.request
        if os.geteuid() != 0:
            raise RuntimeError("trusted_start_requires_root")
        class RefuseRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_arguments, **_keywords):
                raise RuntimeError("trusted_helper_redirect_refused")
        opener = urllib.request.build_opener(RefuseRedirect())
        buffers = {}
        for name, digest in identities:
            url = "https://raw.githubusercontent.com/AstralDeep/AstralDeep/" + commit + "/scripts/" + name
            with opener.open(url, timeout=15) as response:
                if response.status != 200 or response.geturl() != url:
                    raise RuntimeError("trusted_helper_response_invalid")
                data = response.read(limit + 1)
            if len(data) > limit or hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError("trusted_helper_digest_mismatch")
            buffers[name] = data
        peer = types.ModuleType("ci_peer_fixture")
        peer.__file__ = "/run/astral-ci-peer/ci_network_fixture.py"
        exec(compile(buffers["ci_network_fixture.py"], peer.__file__, "exec"), peer.__dict__)
        state = peer.start()
        try:
            fixture = state["id"]
            if not isinstance(fixture, str) or re.fullmatch("[0-9a-f]{16}", fixture) is None:
                raise RuntimeError("trusted_fixture_identity_invalid")
            directory = "/run/astral-ci-peer/" + fixture
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_uid != 0 or metadata.st_mode & 0o022 or not stat.S_ISDIR(metadata.st_mode):
                    raise RuntimeError("trusted_fixture_directory_invalid")
                for name, _digest in identities:
                    target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o444, dir_fd=descriptor)
                    with os.fdopen(target, "wb") as handle:
                        handle.write(buffers[name])
            finally:
                os.close(descriptor)
        except BaseException:
            receipt = peer.close(state)
            if (not isinstance(receipt, dict) or receipt.get("resources_removed") is not True
                    or receipt.get("supervisor_exited") is not True):
                raise RuntimeError("trusted_fixture_cleanup_incomplete")
            raise
        print(json.dumps({"fixture_id": fixture}, sort_keys=True))
    ''')


def loader_source(helpers: ReviewedHelpers, mode: str) -> str:
    if mode not in {"verify", "close"}:
        raise ValueError("trusted helper mode is invalid")
    source = _source_prefix(helpers) + textwrap.dedent('''\
        fixture = sys.argv[1]
        if re.fullmatch("[0-9a-f]{16}", fixture) is None:
            raise RuntimeError("trusted_fixture_identity_invalid")
        directory = "/run/astral-ci-peer/" + fixture
        parent = os.open("/run/astral-ci-peer", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        buffers = {}
        try:
            metadata = os.fstat(parent)
            if metadata.st_uid != 0 or metadata.st_mode & 0o022 or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("trusted_fixture_root_invalid")
            descriptor = os.open(fixture, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_uid != 0 or metadata.st_mode & 0o022 or not stat.S_ISDIR(metadata.st_mode):
                    raise RuntimeError("trusted_fixture_directory_invalid")
                for name, digest in identities:
                    target = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                    with os.fdopen(target, "rb") as handle:
                        metadata = os.fstat(handle.fileno())
                        if metadata.st_uid != 0 or metadata.st_mode & 0o022 or not stat.S_ISREG(metadata.st_mode):
                            raise RuntimeError("trusted_helper_metadata_invalid")
                        data = handle.read(limit + 1)
                    if len(data) > limit or hashlib.sha256(data).hexdigest() != digest:
                        raise RuntimeError("trusted_helper_digest_mismatch")
                    buffers[name] = data
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
        modules = {}
        for name, _digest in identities:
            module = types.ModuleType(name[:-3])
            module.__file__ = directory + "/" + name
            exec(compile(buffers[name], module.__file__, "exec"), module.__dict__)
            modules[name] = module
        peer = modules["ci_network_fixture.py"]
    ''')
    if mode == "verify":
        return source + textwrap.dedent('''\
            verifier = modules["ci_fence_fixture_verifier.py"]
            result = verifier.verify(fixture, sys.argv[2], verifier.runner_node24(), peer=peer)
            print(json.dumps(result, sort_keys=True))
        ''')
    return source + textwrap.dedent('''\
        state = peer.read_owned(peer._directory(fixture) / "ready.json")
        result = peer.close(state)
        if result.get("resources_removed") is not True or result.get("supervisor_exited") is not True:
            raise RuntimeError("trusted_fixture_cleanup_incomplete")
        print(json.dumps(result, sort_keys=True))
    ''')


def expected_start(helpers: ReviewedHelpers) -> str:
    return (
        "state=$(/usr/bin/sudo --non-interactive /usr/bin/env -i "
        "PATH=/usr/bin:/usr/sbin:/bin:/sbin LANG=C LC_ALL=C /usr/bin/python3 -I - <<'ASTRAL_REVIEWED_START'\n"
        + start_source(helpers) + "ASTRAL_REVIEWED_START\n)\n"
        + "printf '%s' \"$state\" | /usr/bin/python3 -I -c "
        + "'import json,re,sys; value=json.load(sys.stdin)[\"fixture_id\"]; "
        + "assert isinstance(value,str) and re.fullmatch(\"[0-9a-f]{16}\",value); "
        + "print(\"fixture_id=\"+value)' >> \"$GITHUB_OUTPUT\"\n"
    )


def expected_verify(helpers: ReviewedHelpers) -> str:
    return (
        f"/usr/bin/python3 -I - '{FIXTURE_ID}' '{INVOCATION}' <<'ASTRAL_REVIEWED_VERIFY'\n"
        + loader_source(helpers, "verify") + "ASTRAL_REVIEWED_VERIFY\n"
    )


def expected_close(helpers: ReviewedHelpers) -> str:
    return (
        f"/usr/bin/python3 -I - '{FIXTURE_ID}' <<'ASTRAL_REVIEWED_CLOSE'\n"
        + loader_source(helpers, "close") + "ASTRAL_REVIEWED_CLOSE\n"
    )


def _run_step(name: str, script: str, *, start=False, close=False) -> str:
    return (
        f"      - name: {name}\n" + ("        id: fence_peer\n" if start else "")
        + ("        if: always()\n" if close else "")
        + "        shell: bash --noprofile --norc -euo pipefail {0}\n        run: |\n"
        + textwrap.indent(script, "          ")
    )


def _expected_steps(helpers: ReviewedHelpers) -> tuple[str, ...]:
    fence = (
        "      - name: Install the reviewed Fence egress firewall in block mode\n"
        f"        uses: {ACTION_PIN}\n        with:\n          mode: block\n"
        f"          invocation_id: {INVOCATION}\n          allow_github_artifacts: true\n"
        "          allowlist: |\n" + "".join(f"            {entry}\n" for entry in ALLOWED_ENDPOINTS)
    )
    return (
        _run_step("Start and baseline the immutable reviewed CI peer", expected_start(helpers), start=True),
        fence,
        _run_step("Verify actual Fence evidence and controlled peer denials", expected_verify(helpers)),
        _run_step("Close the owned peer and attest supervisor exit", expected_close(helpers), close=True),
        "      - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1\n"
        "        with:\n          ref: ${{ github.event.pull_request.head.sha || github.sha }}\n",
        "      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0\n"
        "        with:\n          python-version: \"3.11\"\n          cache: pip\n"
        "          cache-dependency-path: tooling/python-ci/requirements.lock.txt\n",
        "      - name: Install hash-locked CI-only Python tooling\n" f"        run: {LOCK_INSTALL}\n",
        "      - name: Lint Deep-owned Python\n        run: ruff check .\n",
    )


def evaluate_workflow(workflow: str, helpers: ReviewedHelpers | None = None) -> PilotEvaluation:
    helpers = helpers if helpers is not None else REVIEWED_HELPERS
    if not isinstance(helpers, ReviewedHelpers):
        return PilotEvaluation(STATE_MISSING_TRUST, ("actual reviewed merged-main helper SHA and two digests are missing",))
    try:
        helpers.identities()
    except ValueError as error:
        return PilotEvaluation(STATE_MISSING_TRUST, (str(error),))
    if not isinstance(workflow, str) or len(workflow) > 1024 * 1024:
        return PilotEvaluation(STATE_NONCONFORMING, ("workflow text is invalid or exceeds its bound",))
    jobs = list(re.finditer(r"(?m)^jobs:\n", workflow))
    lint = list(re.finditer(r"(?m)^  lint:\n", workflow))
    if len(jobs) != 1 or len(lint) != 1 or lint[0].start() < jobs[0].end():
        return PilotEvaluation(STATE_NONCONFORMING, ("workflow must contain exactly one ordinary lint job",))
    prefix = workflow[:jobs[0].start()]
    keys = []
    for line in prefix.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            key = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):[^\n]*", line)
            if key is None or key.group(1) not in {"name", "on", "concurrency", "permissions", "env", "defaults"}:
                return PilotEvaluation(STATE_NONCONFORMING, ("workflow global keys must be ordinary unaliased declarations",))
            keys.append(key.group(1))
    if len(set(keys)) != len(keys) or re.search(r"(?m)^[^\s#]", workflow[jobs[0].end():]):
        return PilotEvaluation(STATE_NONCONFORMING, ("workflow global declarations cannot be duplicated or shadowed",))
    job_keys = []
    for key in re.finditer(r"(?m)^  [^\s#][^\n]*", workflow[jobs[0].end():]):
        name = re.fullmatch(r"  ([A-Za-z0-9_-]+):", key.group())
        if name is None:
            return PilotEvaluation(STATE_NONCONFORMING, ("workflow jobs must be ordinary unaliased declarations",))
        job_keys.append(name.group(1))
    if len(set(job_keys)) != len(job_keys):
        return PilotEvaluation(STATE_NONCONFORMING, ("workflow jobs cannot be duplicated or shadowed",))
    expected_globals = {
        "permissions": "permissions:\n  contents: read\n",
        "env": "env:\n  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: \"true\"\n",
    }
    for key, expected in expected_globals.items():
        matches = list(re.finditer(rf"(?m)^{key}:\n(?:  [^\n]*\n)*", prefix))
        if len(matches) != 1 or matches[0].group() != expected:
            return PilotEvaluation(STATE_NONCONFORMING, ("workflow inherited environment and readonly permissions must stay exact",))
    if re.search(r"(?m)^defaults:", prefix):
        return PilotEvaluation(STATE_NONCONFORMING, ("workflow shell defaults cannot alter trusted bootstrap execution",))
    rest = workflow[lint[0].end():]
    end = re.search(r"(?m)^  [A-Za-z0-9_-]+:|^[^\s#]", rest)
    job = rest[:end.start()] if end else rest
    job = job.rstrip("\n") + "\n"
    if "uses: openai/fence@" not in job:
        return PilotEvaluation(STATE_ABSENT, ("the actual controlled Fence pilot is required",))
    prelude = (
        "    runs-on: ubuntu-24.04\n    timeout-minutes: 10\n"
        "    permissions:\n      contents: read\n    steps:\n"
    )
    expected = prelude + "".join(_expected_steps(helpers))
    if job != expected:
        return PilotEvaluation(STATE_NONCONFORMING, (
            "lint must exactly preserve trusted start/Fence/verify/always-close before unchanged checkout/setup/locked install/Ruff",
        ))
    return PilotEvaluation(STATE_CONFORMING)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] != "--check":
        print("usage: ci_fence_egress_pilot.py --check WORKFLOW", file=sys.stderr)
        return 2
    try:
        with Path(argv[1]).open(encoding="utf-8") as handle:
            workflow = handle.read(1024 * 1024 + 1)
    except (OSError, UnicodeError):
        print(json.dumps({"state": STATE_NONCONFORMING, "reasons": ["workflow cannot be read"]}))
        return 1
    evaluation = evaluate_workflow(workflow)
    print(json.dumps({"state": evaluation.state, "reasons": list(evaluation.reasons)}))
    return 0 if evaluation.state == STATE_CONFORMING else 1


if __name__ == "__main__":
    sys.exit(main())
