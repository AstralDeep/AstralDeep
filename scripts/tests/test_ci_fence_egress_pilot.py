"""Exercises strict workflow conformance and the trusted helper buffer loaders.
Synthetic helpers test loader effects only; no fixture or Fence runtime acceptance is fabricated.
"""

from __future__ import annotations

import builtins
from dataclasses import replace
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
from types import SimpleNamespace
import urllib.request
import urllib.response

import pytest

from scripts import ci_fence_egress_pilot as pilot

MODULE = Path(pilot.__file__)
FIXTURE = "13ac78bf5902de46"
PEER = b'''import builtins
from pathlib import Path
builtins.pilot_events.append("peer_loaded")
def start():
    builtins.pilot_events.append("start")
    return {"id": "13ac78bf5902de46"}
def close(state):
    builtins.pilot_events.append("close")
    return {"resources_removed": True, "supervisor_exited": True}
def read_owned(path):
    return {"id": "13ac78bf5902de46"}
def _directory(fixture):
    return Path("/run/astral-ci-peer") / fixture
'''
VERIFIER = b'''import builtins
builtins.pilot_events.append("verifier_loaded")
def runner_node24():
    return "synthetic-node"
def verify(fixture, invocation, node, *, peer, action_pin):
    builtins.pilot_events.append("verify")
    return {"fixture": fixture, "invocation": invocation, "loaded_peer": callable(peer.close), "action_pin": action_pin}
'''


def trust(peer=PEER, verifier=VERIFIER):
    return pilot.ReviewedHelpers("3e" * 20, hashlib.sha256(peer).hexdigest(), hashlib.sha256(verifier).hexdigest())


def run_step(name, script, extra=""):
    return (f"      - name: {name}\n" + extra
            + "        shell: bash --noprofile --norc -euo pipefail {0}\n        run: |\n"
            + "".join("          " + line + "\n" for line in script.splitlines()))


def workflow(helpers=None):
    helpers = helpers or trust()
    start = run_step("Start and baseline the immutable reviewed CI peer", pilot.expected_start(helpers),
                     "        id: fence_peer\n")
    fence = """      - name: Install the reviewed Fence egress firewall in block mode
        uses: openai/fence@f9751ca5d767dc0199e88733c2b286bd4800a008 # v0.12.5
        with:
          mode: block
          invocation_id: astral-lint-${{ github.run_id }}-${{ github.run_attempt }}
          allow_github_artifacts: true
          allowlist: |
            github.com:443
            codeload.github.com:443
            raw.githubusercontent.com:443
            objects.githubusercontent.com:443
            release-assets.githubusercontent.com:443
            pypi.org:443
            files.pythonhosted.org:443
            ip 192.0.2.2 tcp 18443
"""
    verify = run_step("Verify actual Fence evidence and controlled peer denials", pilot.expected_verify(helpers))
    close = run_step("Close the owned peer and attest supervisor exit", pilot.expected_close(helpers),
                     "        if: always()\n")
    return """name: CI
on: [push]
permissions:
  contents: read
env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"
jobs:
  lint:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
""" + start + fence + verify + close + """      - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha || github.sha }}
      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0
        with:
          python-version: "3.11"
          cache: pip
          cache-dependency-path: tooling/python-ci/requirements.lock.txt
      - name: Install hash-locked CI-only Python tooling
        run: python -m pip install --require-hashes -r tooling/python-ci/requirements.lock.txt
      - name: Lint Deep-owned Python
        run: ruff check .

  other:
    runs-on: ubuntu-24.04
    steps:
      - run: echo unrelated
"""


def test_only_actual_reviewed_trust_can_enable_cli_conformance(tmp_path, monkeypatch, capsys):
    path = tmp_path / "workflow.yml"
    path.write_text(workflow())
    monkeypatch.setattr(pilot, "REVIEWED_HELPERS", None)
    assert pilot.main(["--check", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "missing_trust"
    monkeypatch.setattr(pilot, "REVIEWED_HELPERS", trust())
    assert pilot.main(["--check", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "conforming"


@pytest.mark.parametrize("field,value", [("commit", "main"), ("commit", "0" * 40),
    ("peer_sha256", "f" * 63), ("verifier_sha256", "0" * 64), ("peer_sha256", None)])
def test_missing_or_invalid_trust_cannot_qualify(field, value):
    invalid = replace(trust(), **{field: value})
    assert pilot.evaluate_workflow(workflow(), invalid).state == "missing_trust"
    with pytest.raises(ValueError):
        pilot.expected_start(invalid)


@pytest.mark.parametrize("old,new", [
    ("    steps:\n", "    permissions:\n      contents: read\n      actions: write\n    steps:\n"),
    ("permissions:\n  contents: read", "permissions:\n  contents: write"),
    ("  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: \"true\"", "  BASH_ENV: /tmp/untrusted"),
    ("timeout-minutes: 10", "timeout-minutes: 30"),
    ("ubuntu-24.04", "self-hosted"),
    ("f9751ca5d767dc0199e88733c2b286bd4800a008", "a" * 40),
    ("mode: block", "mode: audit"),
    ("mode: block", "mode: block\n          unsafe_preserve: true"),
    ("allow_github_artifacts: true", "allow_github_artifacts: false"),
    ("ip 192.0.2.2 tcp 18443", "ip 192.0.2.2 tcp *"),
    ("            pypi.org:443\n", ""),
    ("            pypi.org:443\n", "            pypi.org:443\n            example.com:443\n"),
    ("if: always()", "if: success()"),
    ("if: always()", "if: always()\n        continue-on-error: true"),
    ("/usr/bin/env -i PATH=", "/usr/bin/env PATH="),
    ("data = response.read(limit + 1)", "data = response.read()"),
    ("os.O_EXCL | os.O_NOFOLLOW", "os.O_EXCL"),
    ("0o444, dir_fd=descriptor", "0o666, dir_fd=descriptor"),
    ("peer.close(state)", "pass"),
    ("resources_removed\") is not True", "resources_removed\") is False"),
    ("cache-dependency-path: tooling/python-ci/requirements.lock.txt", "cache-dependency-path: backend/requirements.txt"),
    ("--require-hashes", "--no-deps"),
    ("run: ruff check .", "run: echo linted"),
    ("--run-attempt-does-not-exist", "unused"),
])
def test_changed_enforcement_trust_order_or_normal_operations_are_rejected(old, new):
    original = workflow()
    if old == "--run-attempt-does-not-exist":
        old, new = "invocation_id: astral-lint-", "invocation_id: another-"
    assert old in original
    result = pilot.evaluate_workflow(original.replace(old, new, 1), trust())
    assert result.state == "nonconforming", result


def test_checkout_cannot_precede_attested_cleanup():
    value = workflow()
    start = value.index("      - name: Close the owned peer")
    checkout = value.index("      - uses: actions/checkout@")
    setup = value.index("      - uses: actions/setup-python@")
    reordered = value[:start] + value[checkout:setup] + value[start:checkout] + value[setup:]
    assert pilot.evaluate_workflow(reordered, trust()).state == "nonconforming"


@pytest.mark.parametrize("override", [
    "    permissions:\n      contents: read\n",
    "    permissions:\n      contents: write\n",
    "    permissions:\n      contents: read\n      actions: read\n",
    "    permissions: {contents: read}\n",
])
def test_lint_inherits_exact_global_readonly_and_refuses_job_override(override):
    ordinary = workflow()
    assert pilot.evaluate_workflow(ordinary, trust()).state == "conforming"
    assert pilot.evaluate_workflow(ordinary.replace("    steps:\n", override + "    steps:\n", 1),
                                   trust()).state == "nonconforming"


def test_absent_actual_action_is_not_success(tmp_path, monkeypatch, capsys):
    path = tmp_path / "absent.yml"
    path.write_text(workflow().replace("uses: openai/fence@", "uses: unrelated/action@"))
    monkeypatch.setattr(pilot, "REVIEWED_HELPERS", trust())
    assert pilot.main(["--check", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "absent"


@pytest.mark.parametrize("value", [None, "x" * (1024 * 1024 + 1), "jobs:\n", "  lint:\njobs:\n",
    workflow() + "jobs:\n", workflow() + "  lint:\n"])
def test_malformed_oversized_or_duplicate_jobs_refuse(value):
    assert pilot.evaluate_workflow(value, trust()).state == "nonconforming"


def test_inherited_shell_cannot_change_trusted_commands():
    value = workflow().replace("jobs:\n", "defaults:\n  run:\n    shell: python\njobs:\n")
    assert pilot.evaluate_workflow(value, trust()).state == "nonconforming"


@pytest.mark.parametrize("shadow", [
    'env: {BASH_ENV: /tmp/untrusted}\n',
    '"env": {BASH_ENV: /tmp/untrusted}\n',
    'permissions: write-all\n',
    'jobs: {}\n',
])
def test_shadowed_yaml_globals_cannot_bypass_exact_trusted_job(shadow):
    value = workflow().replace("jobs:\n", shadow + "jobs:\n")
    assert pilot.evaluate_workflow(value, trust()).state == "nonconforming"


@pytest.mark.parametrize("shadow", ['  lint: {}\n', '  "lint": {}\n', '  lint: &shadow {}\n'])
def test_shadowed_yaml_job_cannot_replace_qualified_lint(shadow):
    value = workflow() + shadow
    assert pilot.evaluate_workflow(value, trust()).state == "nonconforming"


def test_duplicate_other_job_is_not_a_qualified_workflow():
    assert pilot.evaluate_workflow(workflow() + "  other:\n", trust()).state == "nonconforming"


def test_single_lint_job_at_end_of_file_can_conform():
    assert pilot.evaluate_workflow(workflow().partition("  other:\n")[0], trust()).state == "conforming"


def test_real_cli_refuses_different_helper_identity_and_removed_patch_mode(tmp_path, capsys):
    path = tmp_path / "workflow.yml"
    path.write_text(workflow())
    result = subprocess.run([sys.executable, str(MODULE), "--check", str(path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout)["state"] == "nonconforming"
    assert pilot.main(["--patch", str(path)]) == 2
    assert pilot.main([]) == 2
    assert "usage:" in capsys.readouterr().err
    assert pilot.main(["--check", str(tmp_path / "missing")]) == 1
    path.write_bytes(b"\xff")
    assert pilot.main(["--check", str(path)]) == 1


def test_script_entrypoint_refuses_different_helper_identity(tmp_path, monkeypatch, capsys):
    path = tmp_path / "workflow.yml"
    path.write_text(workflow())
    monkeypatch.setattr(sys, "argv", [str(MODULE), "--check", str(path)])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(MODULE), run_name="__main__")
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out)["state"] == "nonconforming"


def test_actual_workflow_cli_uses_only_real_merged_helper_identities():
    path = MODULE.parents[1] / ".github" / "workflows" / "ci.yml"
    result = subprocess.run([sys.executable, str(MODULE), "--check", str(path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"state": "conforming", "reasons": []}


class BufferFile(io.BytesIO):
    def __init__(self, data, descriptor, owner):
        super().__init__(data)
        self.descriptor = descriptor
        self.owner = owner

    def fileno(self):
        return self.descriptor

    def close(self):
        if not self.closed:
            self.owner.written[self.descriptor] = self.getvalue()
        super().close()


class ControlledFiles:
    def __init__(self, *, data=None, fail_copy=False, bad_metadata=None):
        self.data = data or {pilot.PEER_FILE: PEER, pilot.VERIFIER_FILE: VERIFIER}
        self.fail_copy = fail_copy
        self.bad_metadata = bad_metadata
        self.entries = {}
        self.written = {}

    def open(self, name, flags, mode=0, *, dir_fd=None):
        descriptor = len(self.entries) + 10
        if self.fail_copy and name == pilot.VERIFIER_FILE and flags & os.O_WRONLY:
            raise OSError("synthetic copy failure")
        self.entries[descriptor] = (name, flags)
        return descriptor

    def fstat(self, descriptor):
        name, _flags = self.entries[descriptor]
        is_file = name in self.data
        permissions = 0o444 if is_file else 0o755
        if self.bad_metadata == name:
            permissions |= 0o020
        return SimpleNamespace(st_uid=0, st_mode=(stat.S_IFREG if is_file else stat.S_IFDIR) | permissions)

    def fdopen(self, descriptor, mode):
        name, _flags = self.entries[descriptor]
        return BufferFile(self.data[name] if mode == "rb" else b"", descriptor, self)


def execute_source(source, files, events, *, downloads=None, response_status=200, redirect=False, uid=0,
                   redirect_target=None, requested=None):
    actual_import = builtins.__import__
    fake_os = SimpleNamespace(**vars(os))
    fake_os.open, fake_os.fstat, fake_os.fdopen = files.open, files.fstat, files.fdopen
    fake_os.close, fake_os.geteuid = lambda _descriptor: None, lambda: uid
    fake_builtins = SimpleNamespace(pilot_events=events)
    fake_sys = SimpleNamespace(argv=["-", FIXTURE, "astral-lint-123-1"])

    class ControlledRedirect(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
        def http_open(self, request):
            requested.append(request.full_url)
            initial = len(requested) == 1
            headers = Message()
            if initial:
                headers["Location"] = redirect_target
            response = urllib.response.addinfourl(
                io.BytesIO(b"" if initial else PEER),
                headers,
                request.full_url, 302 if initial else 200,
            )
            response.msg = "Found" if initial else "OK"
            return response

        https_open = http_open

    def opener(*handlers):
        if redirect_target:
            return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers, ControlledRedirect())
        return SimpleNamespace(open=urlopen)

    def urlopen(url, timeout):
        if redirect_target:
            return opener().open(url, timeout=timeout)
        result = io.BytesIO((downloads or files.data)[url.rsplit("/", 1)[1]])
        result.status = response_status
        result.geturl = lambda: url + ("?redirected" if redirect else "")
        return result

    fake_urllib = SimpleNamespace(request=SimpleNamespace(urlopen=urlopen, build_opener=opener,
                                                        HTTPRedirectHandler=urllib.request.HTTPRedirectHandler))

    def imported(name, *args, **kwargs):
        return {"os": fake_os, "sys": fake_sys, "builtins": fake_builtins,
                "urllib.request": fake_urllib}.get(name) or actual_import(name, *args, **kwargs)

    namespace = {"__builtins__": {**vars(builtins), "__import__": imported}}
    exec(compile(source, "<controlled-loader>", "exec"), namespace)


def test_root_start_executes_verified_buffer_and_writes_only_fixed_copies(capsys):
    files, events = ControlledFiles(), []
    execute_source(pilot.start_source(trust()), files, events)
    assert events == ["peer_loaded", "start"]
    assert json.loads(capsys.readouterr().out) == {"fixture_id": FIXTURE}
    assert set(files.written.values()) == {PEER, VERIFIER}


def test_root_copy_failure_closes_using_already_loaded_peer():
    files, events = ControlledFiles(fail_copy=True), []
    with pytest.raises(OSError, match="copy failure"):
        execute_source(pilot.start_source(trust()), files, events)
    assert events == ["peer_loaded", "start", "close"]


def test_root_copy_failure_requires_supervisor_exit_attestation():
    peer = PEER.replace(b'"supervisor_exited": True', b'"supervisor_exited": False')
    files = ControlledFiles(data={pilot.PEER_FILE: peer, pilot.VERIFIER_FILE: VERIFIER}, fail_copy=True)
    events = []
    with pytest.raises(RuntimeError, match="cleanup_incomplete"):
        execute_source(pilot.start_source(trust(peer=peer)), files, events)
    assert events == ["peer_loaded", "start", "close"]


@pytest.mark.parametrize("target", ["http://other.invalid/helper.py", "https://other.invalid/helper.py"])
def test_root_refuses_redirect_before_requesting_http_or_another_host(target):
    events, requested = [], []
    with pytest.raises(RuntimeError, match="redirect|response_invalid"):
        execute_source(pilot.start_source(trust()), ControlledFiles(), events,
                       redirect_target=target, requested=requested)
    assert requested == [
        "https://raw.githubusercontent.com/AstralDeep/AstralDeep/" + "3e" * 20 + "/scripts/ci_network_fixture.py"
    ]
    assert events == []


def test_root_refuses_invalid_fixture_directory_then_closes_loaded_peer():
    events = []
    with pytest.raises(RuntimeError, match="directory_invalid"):
        execute_source(pilot.start_source(trust()),
                       ControlledFiles(bad_metadata="/run/astral-ci-peer/" + FIXTURE), events)
    assert events == ["peer_loaded", "start", "close"]


@pytest.mark.parametrize("mode", ["start", "verify"])
def test_oversized_even_correctly_hashed_helpers_never_execute(mode):
    oversized = VERIFIER + b" " * pilot.MAX_HELPER_BYTES
    helpers = trust(verifier=oversized)
    files = ControlledFiles(data={pilot.PEER_FILE: PEER, pilot.VERIFIER_FILE: oversized})
    events = []
    source = pilot.start_source(helpers) if mode == "start" else pilot.loader_source(helpers, mode)
    with pytest.raises(RuntimeError, match="digest_mismatch"):
        execute_source(source, files, events)
    assert events == []


@pytest.mark.parametrize("options,reason", [
    ({"downloads": {pilot.PEER_FILE: PEER, pilot.VERIFIER_FILE: b"tampered"}}, "digest_mismatch"),
    ({"response_status": 404}, "response_invalid"),
    ({"redirect": True}, "response_invalid"),
    ({"uid": 1000}, "requires_root"),
])
def test_root_refuses_untrusted_input_before_loading_or_starting(options, reason):
    events = []
    with pytest.raises(RuntimeError, match=reason):
        execute_source(pilot.start_source(trust()), ControlledFiles(), events, **options)
    assert events == []


@pytest.mark.parametrize("mode,expected", [("verify", ["peer_loaded", "verifier_loaded", "verify"]),
                                         ("close", ["peer_loaded", "verifier_loaded", "close"])])
def test_loaders_execute_both_verified_buffers_before_fixed_operation(mode, expected, capsys):
    events = []
    execute_source(pilot.loader_source(trust(), mode), ControlledFiles(), events)
    assert events == expected
    result = json.loads(capsys.readouterr().out)
    assert result["loaded_peer"] is True if mode == "verify" else result["supervisor_exited"] is True
    if mode == "verify":
        assert result["action_pin"] == pilot.REVIEWED_BUNDLE_SHA


@pytest.mark.parametrize("bad_metadata", ["/run/astral-ci-peer", FIXTURE, pilot.PEER_FILE])
def test_unprivileged_loader_refuses_writable_root_directory_or_source(bad_metadata):
    events = []
    with pytest.raises(RuntimeError, match="invalid"):
        execute_source(pilot.loader_source(trust(), "verify"), ControlledFiles(bad_metadata=bad_metadata), events)
    assert events == []


def test_loader_checks_second_digest_before_executing_first_buffer():
    events = []
    files = ControlledFiles(data={pilot.PEER_FILE: PEER, pilot.VERIFIER_FILE: b"tampered"})
    with pytest.raises(RuntimeError, match="digest_mismatch"):
        execute_source(pilot.loader_source(trust(), "close"), files, events)
    assert events == []


def test_missing_exit_attestation_cannot_be_cleanup_success():
    peer = PEER.replace(b'"supervisor_exited": True', b'"supervisor_exited": False')
    events = []
    with pytest.raises(RuntimeError, match="cleanup_incomplete"):
        execute_source(pilot.loader_source(trust(peer=peer), "close"),
                       ControlledFiles(data={pilot.PEER_FILE: peer, pilot.VERIFIER_FILE: VERIFIER}), events)
    assert events[-1] == "close"


def test_unknown_loader_mode_has_no_execution_path():
    with pytest.raises(ValueError, match="mode"):
        pilot.loader_source(trust(), "arbitrary-command")


@pytest.mark.parametrize("builder", [pilot.expected_start, pilot.expected_verify, pilot.expected_close])
def test_trusted_run_blocks_parse_as_shell_without_execution(builder):
    result = subprocess.run(["/bin/bash", "-n"], input=builder(trust()), text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_active_profile_is_exact_qualified_v2_and_explicitly_selected():
    assert pilot.REVIEWED_BUNDLE_SHA == 'f9751ca5d767dc0199e88733c2b286bd4800a008'
    assert pilot.REVIEWED_VERSION == 'v0.12.5'
    assert pilot.VERIFIER_FILE == 'ci_fence_fixture_verifier_v2.py'
    assert pilot.REVIEWED_HELPERS.commit == 'a1bfc19ccd9d1cc58c8298ce26718e92b76b3fb2'
    assert pilot.REVIEWED_HELPERS.verifier_sha256 == '6c16e452bb0be0f877c40067663c6cc3759eab9ebe11fffd0d41952539b90b57'
    source = pilot.loader_source(trust(), 'verify')
    assert 'action_pin=' in source
    assert pilot.REVIEWED_BUNDLE_SHA in source
    candidate = workflow().replace(pilot.REVIEWED_BUNDLE_SHA, 'abc5790eac3445fe3854eea6b506aa7d31bed67c')
    assert pilot.evaluate_workflow(candidate, trust()).state == pilot.STATE_NONCONFORMING
