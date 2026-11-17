"""Shows the automated exit criteria of running a personal node without Docker: draft agents are never reported confined, a sandboxed tool call reaches a loopback executor through the real dispatch path, tool definition hashes are unchanged, the conformance suite passes from the node's own interpreter, and a production boot with a placeholder secret exits 78.
It reuses the fixtures of the draft, executor, mesh call and posture suites and runs the conformance suite as a subprocess exactly as a packed node would.
"""

from __future__ import annotations

import http.server
import json
import logging
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import httpx
import pytest

from orchestrator import node_mesh_calls as nmcalls
from orchestrator import node_stdio_exec, session_store, vessel_executor as ve
from orchestrator.session_store import assert_production_posture
from shared import node_identity as ni
from tests.test_draft_agent_refusal import prepared
from tests.test_node_mesh_calls import OWNER, Orch, many_tools
from tests.test_vessel_executor import AGENT, CATALOG, dispatching_orchestrator, ticket_for

BACKEND = Path(__file__).resolve().parents[1]
RUN_PY = BACKEND / "astral_exec" / "run.py"
DEFINITION_GOLDENS = {
    "lookup": (
        ("lookup", "Looks up", {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}, {}),
        "sha256:f4c2e2e415af6a403e03d8e9fe9befe3ccb78d0b579ad4bc0e44aa5fbeddb2b6",
    ),
    "generate_dynamic_chart": (
        (
            "generate_dynamic_chart",
            "Draws a chart",
            {"type": "object", "properties": {"title": {"type": "string"}, "values": {"type": "array", "items": {"type": "number"}}}},
            {"readOnlyHint": True},
        ),
        "sha256:ed9c096a6fb9e4fcf4604e1ab0f763bfcfb21b252725c3e869c4ba4bacec59b4",
    ),
    "save_note": (("save_note", "Saves a note", {}, {}), "sha256:354587cda16e318f018d992e167cb1d41288acde1b15792bdf40c7c8ef17985f"),
}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("orchestrator.agent_lifecycle.asyncio.sleep", no_sleep)


async def test_a_draft_agent_on_the_personal_profile_is_never_reported_confined_or_published(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    events: list[str] = []
    with caplog.at_level(logging.WARNING):
        result = await prepared(tmp_path, events).start_draft_agent("draft-a")
    assert result["confinement"]["confined"] is False
    assert "runs unconfined" in result["confinement"]["notice"]
    assert any("runs unconfined" in record.getMessage() for record in caplog.records)
    orch = Orch()
    pairs = many_tools(monkeypatch, orch, count=4, agents=("dice-roller-1", "draft-1"))
    orch.drafts = {"draft-1"}
    monkeypatch.setenv(nmcalls.ALLOWLIST_ENV, ",".join(f"{a}__{s.id}" for a, s in pairs))
    tools, _ = nmcalls.OrchestratorCalls(orch).published_selection(OWNER)
    assert tools and len(tools) == len([1 for a, _ in pairs if a == "dice-roller-1"])


class LoopbackExecutor(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.requests: list[dict] = []
        super().__init__(("127.0.0.1", 0), self.Handler)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
            reply = json.dumps({"output": "answered by the loopback executor", "error": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *_args):
            return


async def test_a_sandboxed_tool_call_over_the_executor_seam_is_answered_by_a_loopback_executor():
    server = LoopbackExecutor()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        executor = ve.VesselExecutor(ve.ExecutorSettings(url, "operator", agent_tokens={AGENT: "agent-secret"}), httpx.AsyncClient())
        orch, seen = dispatching_orchestrator(executor)
        ticket = ticket_for()
        with ve.carrying(ticket):
            response = await orch._dispatch_tool_call(AGENT, "lookup", {"q": "x"}, 30.0, None, protected_owner_id=OWNER, protected_channel="background")
        await executor._client.aclose()
    finally:
        server.shutdown()
        server.server_close()
    assert (response.result, response.error) == ("answered by the loopback executor", None)
    assert seen == [(AGENT, "lookup", {"q": "x"}, OWNER)]
    (request,) = server.requests
    assert request["path"] == "/run" and request["authorization"] == "Bearer agent-secret"
    assert request["body"]["ticket_json"] == ticket.ticket_json and request["body"]["arguments_json"] == ticket.arguments_json


def test_tool_definition_hashes_are_byte_identical_to_the_deployed_kernel_era():
    for name, (fields, golden) in DEFINITION_GOLDENS.items():
        assert nmcalls.definition_hash(*fields) == golden, name
    pinned = ve.build_cards(CATALOG)[1]
    assert pinned and all(value.startswith("sha256:") and len(value) == 71 for value in pinned.values())


def test_the_conformance_suite_runs_from_the_nodes_own_interpreter():
    ran = subprocess.run(
        [sys.executable, "-I", "-B", str(RUN_PY), "conformance", "--json"], capture_output=True, text=True, timeout=600, cwd=BACKEND,
    )
    rows = [json.loads(line) for line in ran.stdout.splitlines() if line.startswith("{")]
    assert rows, ran.stderr
    outcomes = {str(row.get("status") or row.get("outcome") or row.get("result")).upper() for row in rows}
    assert "FAIL" not in outcomes
    if node_stdio_exec.default_backend(BACKEND) is not None:
        assert ran.returncode == 0 and outcomes == {"PASS"}
    else:
        assert ran.returncode == 3 and "REFUSED" in outcomes


@pytest.fixture
def production(tmp_path, monkeypatch):
    for name, value in {
        "ASTRAL_ENV": "production", "WEB_SESSION_ENC_KEY": "x" * 44, "CREDENTIAL_ENCRYPTION_KEY": "y" * 44,
        "AUDIT_HMAC_SECRET": f"high-entropy-{uuid.uuid4()}", ni.PROFILE_ENV: ni.PERSONAL, ni.DATA_DIR_ENV: str(tmp_path / "node"),
        "USE_MOCK_AUTH": "false",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("KEYCLOAK_CLIENT_SECRET", ni.OWNER_ENV, ni.ISSUERS_ENV):
        monkeypatch.delenv(name, raising=False)
    ni.reset_cache()
    yield
    ni.reset_cache()


def test_a_production_boot_with_a_placeholder_secret_still_exits_78(production, monkeypatch):
    assert_production_posture()
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "change-me")
    with pytest.raises(SystemExit) as stopped:
        session_store.assert_production_posture()
    assert stopped.value.code == 78
