"""Unit tests for start.py's orchestrator readiness poll (feature 052, FR-029).

``_wait_for_orchestrator`` must proceed on the first healthy /healthz
response, stop early when the orchestrator process dies, and fail closed
after the timeout. The module import itself is side-effect free beyond dotenv
loading, so importing it here is safe.
"""

from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import start  # noqa: E402


class _Proc:
    """Fake subprocess handle with a fixed poll() result."""

    def __init__(self, poll_result=None):
        self._poll_result = poll_result
        self.returncode = poll_result

    def poll(self):
        return self._poll_result


class _Resp:
    """Context-manager stand-in for urllib's HTTP response."""

    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_returns_true_on_first_healthy_response(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: _Resp(200))
    assert (
        start._wait_for_orchestrator(8001, _Proc(None), timeout_s=5.0, interval_s=0.01)
        is True
    )


def test_returns_false_when_process_exits_early(monkeypatch):
    def _refuse(url, timeout=0):
        raise ConnectionError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    assert (
        start._wait_for_orchestrator(8001, _Proc(78), timeout_s=5.0, interval_s=0.01)
        is False
    )


def test_returns_false_after_timeout_with_unreachable_endpoint(monkeypatch):
    def _refuse(url, timeout=0):
        raise ConnectionError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    assert (
        start._wait_for_orchestrator(8001, _Proc(None), timeout_s=0.05, interval_s=0.01)
        is False
    )


def test_non_200_response_keeps_polling_until_timeout(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: _Resp(503))
    assert (
        start._wait_for_orchestrator(8001, _Proc(None), timeout_s=0.05, interval_s=0.01)
        is False
    )


def test_polls_the_configured_port(monkeypatch):
    seen = {}

    def _capture(url, timeout=0):
        seen["url"] = url
        return _Resp(200)

    monkeypatch.setattr(urllib.request, "urlopen", _capture)
    assert (
        start._wait_for_orchestrator(9123, _Proc(None), timeout_s=5.0, interval_s=0.01)
        is True
    )
    assert seen["url"] == "http://localhost:9123/healthz"


def test_module_import_is_side_effect_free():
    assert callable(start.main)
    assert os.path.basename(start.__file__) == "start.py"


def test_main_propagates_orchestrator_exit_78_after_supervisor_cleanup(
    monkeypatch, tmp_path
):
    backend_dir = tmp_path / "backend"
    (backend_dir / "agents").mkdir(parents=True)
    monkeypatch.setattr(start, "__file__", str(backend_dir / "start.py"))

    class _ExitedOrchestrator:
        returncode = 78

        @staticmethod
        def poll():
            return 78

    class _Supervisor:
        def __init__(self):
            self.spawned = []
            self.termination_reason = None

        def spawn(self, **kwargs):
            self.spawned.append(kwargs)
            return _ExitedOrchestrator()

        def terminate_all(self, *, reason):
            self.termination_reason = reason
            return ()

    supervisor = _Supervisor()
    with pytest.raises(SystemExit) as exited:
        start.main(process_supervisor=supervisor)

    assert exited.value.code == 78
    assert len(supervisor.spawned) == 1
    assert supervisor.spawned[0]["owner"].owner_id == "orchestrator"
    assert supervisor.termination_reason.value == "quit"


def test_main_times_out_before_spawning_dependent_agents(monkeypatch, tmp_path):
    backend_dir = tmp_path / "backend"
    agent_dir = backend_dir / "agents" / "external_agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "external_agent.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(start, "__file__", str(backend_dir / "start.py"))
    monkeypatch.setattr(start, "_wait_for_orchestrator", lambda *_args, **_kwargs: False)

    class _RunningOrchestrator:
        returncode = None

        @staticmethod
        def poll():
            return None

    class _Supervisor:
        def __init__(self):
            self.spawned = []
            self.termination_reason = None

        def spawn(self, **kwargs):
            self.spawned.append(kwargs)
            return _RunningOrchestrator()

        def terminate_all(self, *, reason):
            self.termination_reason = reason
            return ()

    supervisor = _Supervisor()
    with pytest.raises(SystemExit) as exited:
        start.main(process_supervisor=supervisor)

    assert exited.value.code == start.EX_UNAVAILABLE
    assert len(supervisor.spawned) == 1
    assert supervisor.spawned[0]["owner"].owner_id == "orchestrator"
    assert supervisor.termination_reason.value == "quit"


# ---------------------------------------------------------------------------
# Agent discovery: only <dir>/<dir>_agent.py is an entrypoint
# ---------------------------------------------------------------------------


def _agents_tree(tmp_path):
    """A bind-mount-shaped agents/ tree: a built-in, the feature-063 verb suites
    under agents/tests (test_remote_compute_agent.py ends in _agent.py), the
    flag-gated remote_compute dir, a draft, an external agent and some noise."""
    backend_dir = tmp_path / "backend"
    agents = backend_dir / "agents"
    agents.mkdir(parents=True)
    (agents / "__init__.py").write_text("", encoding="utf-8")
    (agents / "__pycache__").mkdir()
    (agents / "__pycache__" / "__pycache___agent.py").write_text("", encoding="utf-8")
    tests = agents / "tests"
    tests.mkdir()
    (tests / "test_remote_compute_agent.py").write_text("", encoding="utf-8")
    (tests / "tests_agent.py").write_text("", encoding="utf-8")  # even this must not run
    for name in ("weather", "remote_compute", "external_agent"):
        (agents / name).mkdir()
        (agents / name / f"{name}_agent.py").write_text("", encoding="utf-8")
    # A real-looking dir whose only *_agent.py is NOT named after the dir.
    (agents / "misnamed").mkdir()
    (agents / "misnamed" / "helper_agent.py").write_text("", encoding="utf-8")
    # A test_-prefixed directory that even follows the naming convention.
    (agents / "test_probe").mkdir()
    (agents / "test_probe" / "test_probe_agent.py").write_text("", encoding="utf-8")
    # A draft (on-demand via the UI, never at boot).
    (agents / "drafty").mkdir()
    (agents / "drafty" / "drafty_agent.py").write_text("", encoding="utf-8")
    (agents / "drafty" / ".draft").write_text("", encoding="utf-8")
    # A stray file at the top level.
    (agents / "stray_agent.py").write_text("", encoding="utf-8")
    return backend_dir, agents


def test_agent_entrypoint_accepts_only_dir_named_module(tmp_path):
    _, agents = _agents_tree(tmp_path)
    ok = start._agent_entrypoint(str(agents), "weather")
    assert ok == os.path.join(str(agents), "weather", "weather_agent.py")
    assert start._agent_entrypoint(str(agents), "external_agent").endswith(
        os.path.join("external_agent", "external_agent_agent.py")
    )
    for item in ("tests", "__pycache__", "__init__.py", "misnamed",
                 "test_probe", "stray_agent.py", "missing"):
        assert start._agent_entrypoint(str(agents), item) is None, item


def test_agent_entrypoint_matches_local_agents_convention(tmp_path):
    """start.py and orchestrator/local_agents.py must agree on what an agent
    package is, or the supervisor spawns things the orchestrator never loads."""
    from orchestrator import local_agents

    _, agents = _agents_tree(tmp_path)
    discovered = set(local_agents.discover_built_in_agent_dirs(str(agents)))
    assert discovered == {"weather"}
    for name in local_agents.BUILT_IN_AGENT_DIRS:
        present = start._agent_entrypoint(str(agents), name) is not None
        assert present == (name in discovered)


class _RunningThenExitingOrchestrator:
    """poll() reports alive while agents are being spawned, then a clean exit
    so main() leaves its supervision loop without a SystemExit."""

    returncode = 0

    def __init__(self):
        self._polls = 0

    def poll(self):
        self._polls += 1
        return None if self._polls <= 1 else 0


class _Supervisor:
    def __init__(self):
        self.spawned = []
        self.termination_reason = None

    def spawn(self, **kwargs):
        self.spawned.append(kwargs)
        return _RunningThenExitingOrchestrator()

    def terminate_all(self, *, reason):
        self.termination_reason = reason
        return ()


def _run_main(monkeypatch, backend_dir, *, inprocess, remote_flag):
    monkeypatch.setattr(start, "__file__", str(backend_dir / "start.py"))
    monkeypatch.setattr(start, "_wait_for_orchestrator", lambda *a, **k: True)
    monkeypatch.setattr(start.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setenv("FF_INPROCESS_AGENTS", "1" if inprocess else "0")
    from shared.feature_flags import flags
    monkeypatch.setattr(
        flags, "is_enabled", lambda name: remote_flag and name == "remote_compute")
    supervisor = _Supervisor()
    start.main(process_supervisor=supervisor)
    agents = [s["owner"].owner_id for s in supervisor.spawned
              if s["owner"].owner_kind == "server_agent"]
    return supervisor, agents


def test_main_never_spawns_the_tests_dir_or_misnamed_modules(monkeypatch, tmp_path):
    backend_dir, _ = _agents_tree(tmp_path)
    supervisor, agents = _run_main(
        monkeypatch, backend_dir, inprocess=True, remote_flag=False)
    assert supervisor.spawned[0]["owner"].owner_id == "orchestrator"
    assert "tests" not in agents
    assert "misnamed" not in agents
    assert "test_probe" not in agents
    assert "drafty" not in agents
    assert "__pycache__" not in agents
    # The only subprocess-startable non-built-in agent in the tree.
    assert agents == ["external_agent"]
    spawned = {s["owner"].owner_id: s for s in supervisor.spawned}
    script = spawned["external_agent"]["argv"][1]
    assert os.path.basename(script) == "external_agent_agent.py"
    assert spawned["external_agent"]["cwd"] == os.path.dirname(script)
    # Every spawned agent script is <dir>/<dir>_agent.py — never a test file.
    for s in supervisor.spawned[1:]:
        base = os.path.basename(s["argv"][1])
        assert not base.startswith("test_")
        assert base == f"{s['owner'].owner_id}_agent.py"


def test_max_agents_count_ignores_tests_dir_and_drafts(monkeypatch, tmp_path):
    backend_dir, _ = _agents_tree(tmp_path)
    supervisor, _ = _run_main(
        monkeypatch, backend_dir, inprocess=True, remote_flag=False)
    # weather + remote_compute + external_agent; NOT tests/misnamed/test_probe/
    # drafty/__pycache__ (drafts are excluded from the port count as before).
    assert supervisor.spawned[0]["env"]["MAX_AGENTS"] == "3"


def test_remote_compute_flag_off_never_starts_the_agent(monkeypatch, tmp_path):
    backend_dir, _ = _agents_tree(tmp_path)
    for inprocess in (True, False):
        _, agents = _run_main(
            monkeypatch, backend_dir, inprocess=inprocess, remote_flag=False)
        assert "remote_compute" not in agents, f"inprocess={inprocess}"


def test_remote_compute_flag_on_inprocess_is_not_spawned_twice(monkeypatch, tmp_path):
    backend_dir, _ = _agents_tree(tmp_path)
    _, agents = _run_main(
        monkeypatch, backend_dir, inprocess=True, remote_flag=True)
    assert "remote_compute" not in agents   # register_built_ins owns it
    assert "weather" not in agents          # built-ins likewise
    assert agents == ["external_agent"]


def test_remote_compute_flag_on_inprocess_off_uses_subprocess_path(monkeypatch, tmp_path):
    backend_dir, _ = _agents_tree(tmp_path)
    _, agents = _run_main(
        monkeypatch, backend_dir, inprocess=False, remote_flag=True)
    # The in-process kill-switch falls back to the networked path for the
    # bundled built-ins AND the flag-enabled remote_compute agent.
    assert sorted(agents) == ["external_agent", "remote_compute", "weather"]


def test_remote_compute_flag_reads_the_feature_flag_singleton(monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "remote_compute")
    assert start._remote_compute_enabled() is True
    monkeypatch.setattr(flags, "is_enabled", lambda name: False)
    assert start._remote_compute_enabled() is False

    def _boom(name):
        raise RuntimeError("flags unavailable")

    monkeypatch.setattr(flags, "is_enabled", _boom)
    assert start._remote_compute_enabled() is False  # fail closed
