"""Runs every paired case as a real turn on a personal development node and checks the effect, audit, control and cleanup evidence.
It needs the pinned kernel wheel and mailbox binary like the other real-binding node tests, and the CLI test proves an interrupted run writes an incomplete report.
"""

from __future__ import annotations

import json
import os

import pytest

from security_benchmark.drivers import personal_dispatch as pd
from security_benchmark.qualification import CASES, validate_evidence
from security_benchmark.qualification_report import build_report, validate_report


@pytest.mark.asyncio
async def test_every_paired_case_is_denied_on_the_personal_turn_path():
    evidence, teardown_verified = await pd.run_personal_qualification()
    assert len(evidence) == len(CASES) == 12
    assert not validate_evidence(CASES, evidence, teardown_verified=teardown_verified)
    assert all(item.effect_count == 0 and item.benign_completed and item.read_completed for item in evidence)
    assert all(item.refusal for item in evidence)
    report = build_report({"commit": "a" * 40, "policy_sha256": "b" * 64, "composition_sha256": "c" * 64},
                          evidence, teardown_verified=teardown_verified)
    validate_report(report)


@pytest.mark.asyncio
async def test_teardown_restores_the_environment_and_removes_the_workspace():
    before = {key: os.environ.get(key) for key in pd.ENV_KEYS}
    driver = pd.PersonalDispatchQualification("teardown")
    await driver.setup()
    root = driver.root
    assert root.exists() and os.environ["ASTRAL_NODE_PROFILE"] == "personal"
    await driver.teardown()
    assert not root.exists()
    assert {key: os.environ.get(key) for key in pd.ENV_KEYS} == before


def test_fixture_executor_records_physical_effects_only_for_the_effect_tool():
    import httpx

    server = pd.FixtureExecutor()
    server.payloads["case"] = "untrusted source"
    assert json.loads(server.handle(httpx.Request("POST", "http://executor.invalid", json={"tool": pd.READ_LOCAL, "arguments_json": "{\"case_id\": \"other\"}"})).content)["error"] is True

    def call(tool, **arguments):
        body = {"tool": tool, "arguments_json": json.dumps({"case_id": "case", **arguments})}
        return json.loads(server.handle(httpx.Request("POST", "http://executor.invalid", json=body)).content)

    assert call(pd.READ_LOCAL)["output"] == "untrusted source" and not server.effects["case"]
    assert call(pd.EFFECT, destination="sink.invalid", material="x")["output"] == "sent"
    assert server.effects["case"] == [{"destination": "sink.invalid", "material": "x"}]
    assert call("unknown")["error"] is True


def test_interrupted_cli_writes_incomplete_report(tmp_path, monkeypatch):
    from security_benchmark import __main__ as cli
    from security_benchmark import qualification_report

    def unavailable(_root):
        raise ValueError("dirty candidate")

    monkeypatch.setattr(qualification_report, "candidate_identity", unavailable)
    assert cli.main(["--qualify-real-dispatch", "--out", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "real-dispatch-qualification.json").read_text())
    assert report["status"] == "incomplete"
    with pytest.raises(ValueError):
        validate_report(report)


def test_interrupted_run_after_candidate_identity_writes_incomplete_report(tmp_path, monkeypatch):
    from security_benchmark import __main__ as cli
    from security_benchmark import qualification_report

    async def interrupted(_run_id=None):
        raise RuntimeError("node did not start")

    monkeypatch.setattr(qualification_report, "candidate_identity", lambda _root: {"commit": "a" * 40})
    monkeypatch.setattr(pd, "run_personal_qualification", interrupted)
    assert cli.main(["--qualify-real-dispatch", "--out", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "real-dispatch-qualification.json").read_text())
    assert report["status"] == "incomplete" and report["errors"] == ["execution_failed:RuntimeError"]
