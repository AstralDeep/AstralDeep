"""Tests for scripts/produce_backend_web_qualification.py: fixed-phase execution,
isolated candidate environment, and fail-closed incomplete receipts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

from scripts import produce_backend_web_qualification as producer


@pytest.fixture
def args(tmp_path, monkeypatch):
    monkeypatch.setattr(producer, "observer_user", lambda: "1000:1000")
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    (source / "config/astral-composition.json").write_text(json.dumps({"components": {"astral-plane": {"commit": "e" * 40}}}))
    return argparse.Namespace(policy_commit="a" * 40, candidate_sha="b" * 40,
                              candidate_image="sha256:" + "c" * 64, lets_image="sha256:" + "d" * 64,
                              materializer_image="sha256:" + "e" * 64, source=source,
                              baseline_plane_source=tmp_path, private_root=tmp_path / "private",
                              output=tmp_path / "output", port=18091, protected=False,
                              voice_image=None, voice_closure_sha256=None, speech_runtime_env=None)


def test_candidate_process_environment_has_no_provider_job_authority(monkeypatch):
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "RUNNER_NAME"):
        monkeypatch.setenv(key, "do-not-propagate")
    monkeypatch.setenv("PATH", "safe-path")
    cleaned = producer.clean_environment()
    assert cleaned["PATH"] == "safe-path"
    assert "do-not-propagate" not in cleaned.values()


def test_execute_uses_fixed_argv_sanitized_environment_and_safe_error(monkeypatch, tmp_path):
    calls = []
    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    assert producer.execute(["fixed", "command"], cwd=tmp_path).returncode == 0
    assert calls[0][1]["timeout"] == 300
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="fixed qualification"):
        producer.execute(["fixed"], cwd=tmp_path)


def test_context_requires_exact_protected_owner_and_private_new_paths(args, monkeypatch):
    driver = SimpleNamespace(verify_source=lambda *_: None, IMAGE=re.compile(r"sha256:[0-9a-f]{64}"))
    monkeypatch.setattr(producer, "module", lambda _: driver)
    assert producer.verify_context(args) is driver
    args.protected = True
    with pytest.raises(ValueError, match="protected producer"):
        producer.verify_context(args)
    for key, value in {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_JOB": "qualify-backend-web", "GITHUB_WORKFLOW_SHA": args.policy_commit,
                       "BACKEND_WEB_PRODUCER_SHA": args.policy_commit,
                       "RUNNER_NAME": "runner", "BACKEND_WEB_PRODUCER_RUNNER": "runner"}.items():
        monkeypatch.setenv(key, value)
    assert producer.verify_context(args) is driver
    args.output = args.private_root
    with pytest.raises(ValueError, match="separate trees"):
        producer.verify_context(args)


@pytest.mark.parametrize("field,value", [("candidate_sha", "bad"), ("port", 80), ("candidate_image", "latest"), ("output", Path("relative"))])
def test_invalid_context_refused_before_commands(args, monkeypatch, field, value):
    monkeypatch.setattr(producer, "module", lambda _: SimpleNamespace(verify_source=lambda *_: None, IMAGE=re.compile(r"sha256:[0-9a-f]{64}")))
    setattr(args, field, value)
    with pytest.raises(ValueError):
        producer.verify_context(args)


@pytest.mark.parametrize("failure", [None, "gates", "services", "deployment", "auth", "auth-record"])
def test_fixed_phases_record_missing_acceptance_without_fake_success(args, monkeypatch, failure):
    calls = []
    def initialize(**kwargs):
        calls.append(("initialize", kwargs))
        if failure == "services":
            raise RuntimeError("sensitive service error")
        return {"runtime_fragment": str(args.private_root / "runtime.env"),
                "material_root": str(args.private_root / "materials/app"), "service_network": "isolated-services"}
    def deploy(arguments):
        calls.append(("deploy", arguments))
        if failure == "deployment":
            raise RuntimeError("sensitive deployment error")
        return {"status": "started"}
    driver = SimpleNamespace(deploy=deploy)
    monkeypatch.setattr(producer, "verify_context", lambda _: driver)
    def load(name):
        if name == "validate_backend_web_evidence":
            return SimpleNamespace(CHECKS={"real-keycloak-auth", "session-renewal", "owner-authorization-denials", "voice-enabled-browser", "backend-complete"})
        return SimpleNamespace(initialize=initialize)
    monkeypatch.setattr(producer, "module", load)
    def execute(arguments, **kwargs):
        calls.append(("execute", arguments, kwargs))
        if arguments[0] == "bash":
            if failure == "gates":
                raise RuntimeError("sensitive gates error")
            assert "ASTRAL_GATE_POLICY_ROOT" in kwargs["environment"]
            return SimpleNamespace(returncode=0)
        if failure == "auth":
            raise RuntimeError("sensitive authentication error")
        identifier = arguments[arguments.index("--qualification-id") + 1]
        result = {"qualification_id": identifier, "synthetic_only": True,
                  **dict.fromkeys(("real_application_keycloak_login_pkce", "login_state_denial", "application_session_renewal",
                                   "owner_read_denials", "logout_isolation", "chat_history_retention",
                                   "attachment_upload_metadata_retention", "attachment_owner_denials"), "passed")}
        if failure == "auth-record":
            result["application_session_renewal"] = "skipped"
        (args.output / "authentication.json").write_text(json.dumps(result))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(producer, "execute", execute)
    report = producer.produce(args)
    assert report["status"] == "incomplete-not-qualified"
    assert report["release_authorized"] is False
    assert "voice-enabled-browser" in report["pending_checks"]
    assert "backend-complete" in report["pending_checks"]
    assert not (args.output / "evidence.json").exists()
    assert "sensitive" not in json.dumps(report)
    if failure is None:
        assert "session-renewal" in report["observed_checks"]
        auth = next(row[1] for row in calls if row[0] == "execute" and row[1][0] == "docker")
        assert args.materializer_image in auth and args.candidate_image not in auth
        assert "--read-only" in auth and "/probe.py" in auth
        assert "--user" in auth and "1000:1000" in auth
    if failure in {"auth", "auth-record"}:
        assert "session-renewal" in report["pending_checks"]


def test_cli_incomplete_is_nonzero_and_does_not_authorize(args, monkeypatch, capsys):
    values = []
    for name, value in vars(args).items():
        if value is not None and name != "protected":
            values += ["--" + name.replace("_", "-"), str(value)]
    monkeypatch.setattr(producer, "produce", lambda _: {"status": "incomplete-not-qualified", "pending_checks": ["voice"]})
    assert producer.main(values) == 2
    assert json.loads(capsys.readouterr().out)["release_authorized"] is False
    def fail(_):
        raise ValueError("private detail")
    monkeypatch.setattr(producer, "produce", fail)
    assert producer.main(values) == 1
    assert "private detail" not in capsys.readouterr().err


def test_protected_module_loading_uses_policy_tree():
    assert producer.module("run_backend_web_qualification").BASELINE_PLANE == producer.BASELINE_PLANE
