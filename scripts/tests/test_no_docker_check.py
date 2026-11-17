"""Tests the pure steps of scripts/no_docker_check.py against fakes and, when ASTRAL_NODE_PACK names a pack folder, runs the whole check against it.
It imports the script by path, so it runs under the repository interpreter and under the Linux exec harness."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "no_docker_check.py"
spec = importlib.util.spec_from_file_location("no_docker_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
sys.modules["no_docker_check"] = check
spec.loader.exec_module(check)

GOOD = {
    "paired": True,
    "tool_name": "generate_dynamic_chart",
    "anonymous": {"status": 401, "text": ""},
    "bearer": {"status": 403, "text": ""},
    "listing": {"status": 200, "text": json.dumps({"result": {"tools": [{"name": "generate_dynamic_chart"}]}})},
    "call": {"status": 200, "text": json.dumps({"result": {"content": check.CHART_TITLE}})},
    "uncovered": {"status": 403, "text": ""},
    "replay": {"status": 401, "text": ""},
}


def test_the_path_is_system_directories_and_docker_on_it_is_found():
    assert check.clean_path(["/x"]).split(":")[0] == "/x"
    assert check.docker_on_path("/a", which=lambda name, path: None) is None
    assert check.docker_on_path("/a", which=lambda name, path: f"{path}/{name}") == "/a/docker"


def test_the_docker_step_reports_both_outcomes(monkeypatch):
    report = check.Report()
    check.step_docker(report)
    assert report.checks[0].ok
    monkeypatch.setattr(check, "docker_on_path", lambda path: "/usr/bin/docker")
    check.step_docker(report)
    assert not report.checks[1].ok and report.exit_code == 1


def test_env_text_parsing_and_merging_keep_existing_values():
    assert check.parse_env_text("# c\nA=1\nB='two'\nbad\n") == {"A": "1", "B": "two"}
    merged = check.merge_env("A=1", {"A": "9", "C": "3"})
    assert merged == "A=1\nC=3\n"
    assert check.merge_env("", {"C": "3"}) == "C=3\n"


@pytest.mark.parametrize(
    ("code", "out", "ok"),
    [(0, "PASS a\nPASS b\n", True), (3, "PASS a\nREFUSED b\n", False), (1, "PASS a\nFAIL b: x\n", False), (0, "", False)],
)
def test_conformance_passes_only_when_every_case_passes(code, out, ok):
    assert check.conformance_verdict(code, out)[0] is ok


def test_mcp_requests_carry_the_protocol_and_the_tool_name():
    body = json.loads(check.mcp_body("tools/call", {"name": "t"}))
    assert body["method"] == "tools/call" and body["params"]["name"] == "t" and "_meta" in body["params"]
    assert check.mcp_headers("tools/call", "t")["Mcp-Name"] == "t"
    assert "Mcp-Name" not in check.mcp_headers("tools/list")


def test_result_reading_tolerates_text_and_garbage():
    assert check.covered_names("nope") == []
    assert check.covered_names(json.dumps({"result": {"tools": [{"name": "b"}, {"name": "a"}, 3]}})) == ["a", "b"]
    assert "x" in check.result_text(json.dumps({"result": "x"}))
    assert check.result_text("plain") == "plain"
    assert check.result_text("[1]") == "[1]"


def test_refusal_is_a_denied_status_or_an_error_body():
    assert check.refused(401) and check.refused(500)
    assert not check.refused(None)
    assert check.refused(200, json.dumps({"error": {}}))
    assert not check.refused(200, json.dumps({"result": {}}))
    assert not check.refused(200, "not json")


def test_a_full_set_of_observations_passes():
    assert check.evaluate(GOOD, "generate_dynamic_chart").exit_code == 0


@pytest.mark.parametrize(
    "key",
    ["anonymous", "bearer", "listing", "call", "uncovered", "replay"],
)
def test_each_missing_denial_or_answer_fails_the_report(key):
    broken = dict(GOOD)
    broken[key] = {"status": 200, "text": json.dumps({"result": {"tools": [{"name": "a"}, {"name": "b"}]}})} if key != "call" else {"status": 500, "text": ""}
    assert check.evaluate(broken, "generate_dynamic_chart").exit_code == 1


def test_an_unpaired_owner_stops_the_report_and_no_second_tool_fails():
    stopped = check.evaluate({"paired": False, "paired_detail": "none"}, "t")
    assert len(stopped.checks) == 1 and stopped.exit_code == 1
    lonely = {k: v for k, v in GOOD.items() if k != "uncovered"}
    assert any(not c.ok and "outside" in c.name for c in check.evaluate(lonely, "generate_dynamic_chart").checks)


def test_an_empty_report_fails_and_lines_summarise():
    assert check.Report().exit_code == 1
    assert check.evaluate(GOOD, "generate_dynamic_chart").lines()[-1].startswith("RESULT")


def test_the_pack_interpreter_must_exist(tmp_path):
    with pytest.raises(check.CheckError):
        check.installed_pack_python(tmp_path)
    python = check.pack_python(tmp_path)
    python.parent.mkdir(parents=True)
    python.write_text("")
    assert check.installed_pack_python(tmp_path) == python


def test_the_install_step_needs_a_descriptor_and_reports_verify_failures(tmp_path):
    report = check.Report()
    with pytest.raises(check.CheckError):
        check.step_install(report, tmp_path, tmp_path / "root")
    (tmp_path / "node-pack.json").write_text("{}")
    with pytest.raises(check.CheckError):
        check.step_install(report, tmp_path, tmp_path / "root")
    assert not report.checks[-1].ok


def test_the_conformance_step_runs_the_pack_python(tmp_path, monkeypatch):
    python = check.pack_python(tmp_path)
    python.parent.mkdir(parents=True)
    python.write_text("")
    seen = {}

    def fake(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "PASS a\n", "")

    monkeypatch.setattr(subprocess, "run", fake)
    report = check.Report()
    check.step_conformance(report, tmp_path, tmp_path)
    assert report.checks[0].ok and seen["command"][0] == str(python) and "conformance" in seen["command"]


def test_the_home_install_failure_and_success(tmp_path, monkeypatch):
    def failing(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, "", "boom")

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(check.CheckError):
        check.step_install_home(tmp_path, tmp_path / "home", 1)

    def working(command, **kwargs):
        home = Path(command[command.index("--home") + 1])
        home.mkdir(exist_ok=True)
        (home / "node.env").write_text("ASTRAL_ENV=production\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", working)
    env = check.step_install_home(tmp_path, tmp_path / "home", 7)
    values = check.parse_env_text(env.read_text())
    assert values["ASTRAL_ENV"] == "production" and values["PUBLIC_BASE_URL"] == "http://127.0.0.1:7"
    assert values["ASTRAL_NODE_MESH_RELAY"] == "none"
    assert f"general-1__{check.TOOL}" in values["ASTRAL_NODE_PUBLISHED_TOOLS"]


def test_waiting_for_a_file_and_the_offer(tmp_path, monkeypatch):
    target = tmp_path / "secret"
    with pytest.raises(check.CheckError):
        check.wait_for_file(target, 0.2)
    target.write_text("s\n")
    assert check.wait_for_file(target, 1) == "s"

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    monkeypatch.setattr(check.urllib.request, "urlopen", lambda request, timeout: Response({"state": "offer", "payload": "p"}))
    assert check.fetch_offer(1, "s") == "p"
    monkeypatch.setattr(check.urllib.request, "urlopen", lambda request, timeout: Response({"state": "enrolled"}))
    with pytest.raises(check.CheckError):
        check.fetch_offer(1, "s")

    def broken(request, timeout):
        raise OSError("down")

    monkeypatch.setattr(check.urllib.request, "urlopen", broken)
    with pytest.raises(check.CheckError):
        check.fetch_offer(1, "s")


def test_post_reports_status_and_transport_failures(monkeypatch):
    class Failure(check.urllib.error.HTTPError):
        def __init__(self):
            super().__init__("u", 401, "m", {}, None)

        def read(self):
            return b"denied"

    def deny(request, timeout):
        raise Failure()

    monkeypatch.setattr(check.urllib.request, "urlopen", deny)
    assert check.post("http://x", "/p", b"", {}) == {"status": 401, "text": "denied"}

    def down(request, timeout):
        raise OSError("down")

    monkeypatch.setattr(check.urllib.request, "urlopen", down)
    assert check.post("http://x", "/p", b"", {})["status"] is None


def test_readiness_fails_when_the_node_exits_and_succeeds_on_ready(monkeypatch):
    class Dead:
        returncode = 3

        def poll(self):
            return 3

    with pytest.raises(check.CheckError):
        check.wait_ready(1, 1, Dead())

    class Alive:
        def poll(self):
            return None

    class Ok:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(check.urllib.request, "urlopen", lambda url, timeout: Ok())
    check.wait_ready(1, 1, Alive())
    monkeypatch.setattr(check.urllib.request, "urlopen", lambda url, timeout: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(check.time, "sleep", lambda seconds: None)
    with pytest.raises(check.CheckError):
        check.wait_ready(1, 0.01, Alive())


def test_stopping_a_group_ends_it_and_leftovers_are_found_by_marker():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    assert check.group_alive(process.pid)
    check.stop_group(process)
    assert not check.group_alive(process.pid)
    check.stop_group(None)
    commands = lambda: [(os.getpid(), "marker"), (1, "x marker y"), (2, "other")]  # noqa: E731
    assert check.leftover_processes("marker", commands) == [1]
    assert any(pid == os.getpid() for pid, _ in check.listed_commands())
    assert check.free_port() > 0


def test_the_full_check_exits_zero_on_a_real_pack():
    pack = os.environ.get("ASTRAL_NODE_PACK")
    if not pack:
        pytest.skip("ASTRAL_NODE_PACK names no pack folder")
    done = subprocess.run([sys.executable, str(SCRIPT), "--pack", pack], capture_output=True, text=True, timeout=1500)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]
