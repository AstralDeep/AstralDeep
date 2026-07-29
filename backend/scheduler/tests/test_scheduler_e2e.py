"""030 T007 — grant-backed scheduler run end-to-end (US1).

Drives JobRunner.run_job on a NON-dreaming job (dreaming is covered by
test_runner_dreaming.py): valid grant → scope-intersected run via
orch.run_scheduled_turn + success notification; missing/invalid grant →
skipped_auth + pause + warning, never executing.
"""
import asyncio
import sys
import types
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scheduler.runner import JobRunner  # noqa: E402


class _Grants:
    def __init__(self, valid=True):
        self._valid = valid

    def is_valid(self, grant_id):
        return self._valid

    async def mint_access_token(self, grant_id):
        return f"tok-{grant_id}"


class _Store:
    def __init__(self):
        self.runs, self.finished, self.statuses, self.after = [], [], [], []

    def start_run(self, job_id, user_id, correlation_id):
        rid = f"run{len(self.runs)}"
        self.runs.append(rid)
        return rid

    def finish_run(self, run_id, *, outcome, summary=None, auth_ref=None):
        self.finished.append((run_id, outcome, summary))

    def set_status(self, user_id, job_id, status):
        self.statuses.append((job_id, status))
        return True

    def update_after_run(self, job_id, *, last_run_at, next_run_at, completed):
        self.after.append((job_id, next_run_at, completed))


def _orch(current_scopes):
    calls = {"notify": []}

    async def run_scheduled_turn(*, user_id, chat_id, instruction, agent_id,
                                 access_token, allowed_scopes, correlation_id,
                                 authority=None):
        # 056 US2: the runner now threads the consent-derived MachineAuthority.
        calls["run"] = {"allowed_scopes": list(allowed_scopes), "access_token": access_token,
                        "instruction": instruction, "chat_id": chat_id, "user_id": user_id,
                        "authority_principal": getattr(authority, "principal", None)}
        return "did the thing"

    async def notify_user(user_id, payload):
        calls["notify"].append(payload)

    orch = types.SimpleNamespace(
        run_scheduled_turn=run_scheduled_turn,
        notify_user=notify_user,
        # Derivation reads the EFFECTIVE scope list, not the raw rows, so the
        # safe-agent baseline is not mistaken for "no grants" (see
        # test_machine_turn_authority.py::test_safe_baseline_*).
        tool_permissions=types.SimpleNamespace(
            get_agent_scopes=lambda uid, aid: dict(current_scopes),
            get_enabled_scope_names=lambda uid, aid: [
                s for s, on in current_scopes.items() if on]),
    )
    return orch, calls


_JOB = {
    "id": "j1", "user_id": "u1", "agent_id": "web-research-1", "name": "Digest",
    "instruction": "summarize recent findings", "schedule_kind": "interval",
    "schedule_expr": "1d", "timezone": "UTC", "offline_grant_id": "g1",
    "target_chat_id": "c1", "consented_scopes": ["tools:read", "tools:search"],
}


def test_grant_backed_run_intersects_scopes_and_notifies():
    # current scopes: read enabled, search disabled → allowed = consented ∩ current
    orch, calls = _orch({"tools:read": True, "tools:search": False, "tools:write": True})
    store = _Store()
    runner = JobRunner(orch, store, _Grants(valid=True))
    outcome = asyncio.run(runner.run_job(dict(_JOB)))
    assert outcome == "success"
    assert calls["run"]["allowed_scopes"] == ["tools:read"]  # SC-008 intersection
    assert calls["run"]["access_token"] == "tok-g1"          # minted from the grant
    assert store.finished[0][1] == "success"
    assert any(p.get("level") == "success" for p in calls["notify"])


def test_missing_grant_skips_auth_and_pauses_without_executing():
    orch, calls = _orch({"tools:read": True})
    store = _Store()
    runner = JobRunner(orch, store, _Grants(valid=False))
    outcome = asyncio.run(runner.run_job(dict(_JOB)))
    assert outcome == "skipped_auth"
    assert ("j1", "paused") in store.statuses
    assert "run" not in calls  # never executed under stale authority
    assert any(p.get("level") == "warning" for p in calls["notify"])
    assert store.finished[0][1] == "skipped_auth"
