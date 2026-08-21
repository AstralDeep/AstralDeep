"""Feature 027 — T014: personalization surface (tabs, forms, handlers).

Structural/behavioral tests against a minimal fake orchestrator — no
Postgres required. The PHI gate singleton is replaced with a deterministic
fake (clean analyzer; the pure-Python prefilter still applies) so PHI
rejections are exercised via obvious identifiers (SSN pattern).
"""
import asyncio
import html as html_module
import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from personalization import phi_gate as pg
from shared.feature_flags import flags
from orchestrator.projection_surfaces import personalization as surf


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _CleanAnalyzer:
    """Presidio stand-in that reports no entities (prefilter still active)."""

    def analyze(self, text, language, entities, score_threshold):
        return []


@pytest.fixture(autouse=True)
def _fake_phi_gate():
    """Install a deterministic PHI gate; restore the previous one after."""
    prev = pg._GATE
    pg.set_phi_gate(pg.PHIGate(analyzer=_CleanAnalyzer(), build_if_missing=False))
    yield
    pg.set_phi_gate(prev)


class FakeRepo:
    """PersonalizationRepository stand-in (profile, memory, sweeps, signals)."""

    def __init__(self):
        self.profile = {
            "user_id": "u1",
            "profession": "researcher",
            "goals": ["write more"],
            "personality": {"tone": "warm", "notes": "be brief"},
            "dreaming_enabled": True,
        }
        self.memory = [{
            "id": "m1", "user_id": "u1", "category": "preference",
            "value": "prefers tables", "source": "explicit", "salience": 1.0,
            "created_at": 1700000000000, "updated_at": 1700000000000,
        }]
        self.sweeps = [{
            "id": "s1", "ran_at": 1700000000000, "candidates_considered": 3,
            "promoted_count": 1, "summary": "Reviewed 3 recent signal(s).",
            "trigger": "manual",
        }]
        self.signals = []
        self.upsert_calls = []
        self.dreaming_calls = []

    def get_profile(self, user_id):
        return dict(self.profile)

    def upsert_profile(self, user_id, **kwargs):
        self.upsert_calls.append(kwargs)
        return dict(self.profile)

    def list_memory(self, user_id):
        return [dict(m) for m in self.memory]

    def update_memory_value(self, user_id, mem_id, value):
        for m in self.memory:
            if m["id"] == mem_id:
                m["value"] = value
                return True
        return False

    def delete_memory(self, user_id, mem_id):
        before = len(self.memory)
        self.memory = [m for m in self.memory if m["id"] != mem_id]
        return len(self.memory) < before

    def set_dreaming_enabled(self, user_id, enabled):
        self.dreaming_calls.append(enabled)

    def list_sweeps(self, user_id, limit=20):
        return [dict(s) for s in self.sweeps]

    def list_signals(self, user_id):
        return [dict(s) for s in self.signals]

    def delete_signal(self, user_id, sig_id):
        self.signals = [s for s in self.signals if s["id"] != sig_id]

    def create_memory(self, user_id, category, value, *, source="explicit",
                      salience=0.0):
        item = {"id": f"new-{len(self.memory)}", "category": category,
                "value": value, "created_at": 0, "updated_at": 0}
        self.memory.append(item)
        return item

    def record_sweep(self, sweep):
        self.sweeps.insert(0, dict(sweep))


class FakeToolPermissions:
    """ToolPermissionManager stand-in with one authorized + one denied scope."""

    def __init__(self):
        self._tool_scope_map = {
            "helper": {"search_docs": "tools:read", "wipe_disk": "tools:system"},
        }
        self._scope_grants = {("u1", "helper", "tools:read"): True,
                              ("u1", "helper", "tools:system"): False}
        self._allowed = {("u1", "helper", "search_docs"): True,
                         ("u1", "helper", "wipe_disk"): False}
        self.override_calls = []

    def get_tool_scope_map(self, agent_id):
        return dict(self._tool_scope_map.get(agent_id, {}))

    def get_tool_scope(self, agent_id, tool_name):
        return self._tool_scope_map.get(agent_id, {}).get(tool_name, "tools:read")

    def is_scope_enabled(self, user_id, agent_id, scope):
        return self._scope_grants.get((user_id, agent_id, scope), False)

    def is_tool_allowed(self, user_id, agent_id, tool_name):
        return self._allowed.get((user_id, agent_id, tool_name), False)

    def set_tool_overrides(self, user_id, agent_id, overrides):
        self.override_calls.append((user_id, agent_id, dict(overrides)))

    def set_skill_enabled(self, user_id, agent_id, tool_name, enabled):
        # 027 fix: the handler now writes the winning per-(tool, kind) row
        # through this method instead of the outranked NULL-kind row.
        self.skill_calls = getattr(self, "skill_calls", [])
        self.skill_calls.append((user_id, agent_id, tool_name, enabled))


class _Cursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeDB:
    """Database stand-in for ScheduledJobStore's SQL (jobs + runs)."""

    def __init__(self, jobs=None, runs=None):
        self.jobs = jobs or []
        self.runs = runs or []
        self.executed = []

    def fetch_all(self, sql, params=()):
        if "FROM scheduled_job" in sql:
            return [dict(j) for j in self.jobs]
        if "FROM job_run" in sql:
            job_id = params[0]
            return [dict(r) for r in self.runs if r.get("job_id") == job_id]
        return []

    def fetch_one(self, sql, params=()):
        if "FROM scheduled_job" in sql:
            for j in self.jobs:
                if j["id"] == params[0]:
                    return dict(j)
        return None

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if "UPDATE scheduled_job SET status" in sql:
            for j in self.jobs:
                if j["id"] == params[2]:
                    j["status"] = params[0]
                    return _Cursor(1)
            return _Cursor(0)
        return _Cursor(1)


class FakeScheduleActionStore:
    """Action-aware store fake; direct SQL remains observable on ``db``."""

    def __init__(self, db):
        self.db = db
        self.run_now_calls = []
        self.atomic_status_calls = []
        self.legacy_status_calls = []
        self._occurrences = {}

    def list_jobs(self, user_id):
        return [dict(job) for job in self.db.jobs if job["user_id"] == user_id]

    def list_runs(self, user_id, job_id):
        return [
            dict(run)
            for run in self.db.runs
            if run.get("user_id") == user_id and run.get("job_id") == job_id
        ]

    def get_job(self, user_id, job_id):
        for job in self.db.jobs:
            if job["user_id"] == user_id and job["id"] == job_id:
                return dict(job)
        return None

    def materialize_run_now(
        self, *, user_id, job_id, submission_id, eligibility
    ):
        self.run_now_calls.append(
            {
                "user_id": user_id,
                "job_id": job_id,
                "submission_id": submission_id,
                "eligibility": eligibility,
            }
        )
        key = (user_id, submission_id)
        prior = self._occurrences.get(key)
        if prior is not None:
            return SimpleNamespace(**{**prior.__dict__, "created": False})
        result = SimpleNamespace(
            occurrence_id=uuid.uuid4(),
            job_id=uuid.UUID(job_id) if _is_uuid(job_id) else job_id,
            owner_user_id=user_id,
            scheduled_for=datetime(2026, 7, 16, 15, 30, tzinfo=UTC),
            state="pending",
            created=True,
        )
        self._occurrences[key] = result
        return result

    def set_status_and_cancel_unstarted(
        self, *, user_id, job_id, status, terminal_code
    ):
        self.atomic_status_calls.append(
            {
                "user_id": user_id,
                "job_id": job_id,
                "status": status,
                "terminal_code": terminal_code,
            }
        )
        for job in self.db.jobs:
            if job["user_id"] == user_id and job["id"] == job_id:
                job["status"] = status
                return True
        return False

    def set_status(self, user_id, job_id, status):
        self.legacy_status_calls.append((user_id, job_id, status))
        for job in self.db.jobs:
            if job["user_id"] == user_id and job["id"] == job_id:
                job["status"] = status
                return True
        return False


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _job(job_id, status, name="Daily digest"):
    return {
        "id": job_id, "user_id": "u1", "agent_id": "helper", "name": name,
        "instruction": "do it", "schedule_kind": "cron",
        "schedule_expr": "0 9 * * *", "timezone": "UTC",
        "consented_scopes": ["tools:read"], "status": status,
        "target_chat_id": None, "next_run_at": 1700000400000,
        "last_run_at": 1700000000000, "created_at": 1, "updated_at": 1,
    }


def make_orch(jobs=None, runs=None):
    repo = FakeRepo()
    schedule_db = FakeDB(jobs=jobs, runs=runs)
    runner = SimpleNamespace(
        assess_job=lambda _job: SimpleNamespace(eligible=True, code=None)
    )
    return SimpleNamespace(
        personalization_service=SimpleNamespace(repo=repo),
        tool_permissions=FakeToolPermissions(),
        history=SimpleNamespace(db=schedule_db),
        scheduled_job_store=FakeScheduleActionStore(schedule_db),
        work_admission=object(),
        _scheduler_loop=SimpleNamespace(runner=runner),
        ui_sessions={},
    )


def render(orch, params=None, roles=("user",)):
    return asyncio.run(surf.render(orch, "u1", list(roles), params or {}))


def call(handler, orch, payload):
    return asyncio.run(handler(orch, object(), "u1", ["user"], payload))


def _html_action_payloads(markup, action):
    encoded = re.findall(
        rf'data-ui-action="{re.escape(action)}"[^>]*data-ui-payload=\'([^\']*)\'',
        markup,
    )
    return [json.loads(html_module.unescape(value)) for value in encoded]


def _sdui_action_payloads(value, action):
    found = []

    def visit(node):
        if isinstance(node, dict):
            if node.get("action") == action:
                found.append(node.get("payload") or {})
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


# ---------------------------------------------------------------------------
# Render — tabs and escaping
# ---------------------------------------------------------------------------

def test_render_defaults_to_soul_tab_with_form_and_precedence_note():
    html = render(make_orch())
    for label in ("Soul", "Memory", "Skills", "Schedule", "Dreaming"):
        assert label in html
    assert "data-ui-form" in html
    for name in ("profession", "goals", "personality_notes"):
        assert f'name="{name}"' in html
    assert 'data-ui-action="chrome_profile_save"' in html
    assert 'data-ui-collect="true"' in html
    # 025 precedence note: personality is style-only.
    assert "tone and voice only" in html
    # Profile values are prefilled.
    assert 'value="researcher"' in html
    assert "write more" in html
    assert "be brief" in html


def test_tab_bar_buttons_are_chrome_open_with_tab_params():
    html = render(make_orch())
    assert 'data-ui-action="chrome_open"' in html
    for tab in ("soul", "memory", "skills", "schedule", "dreaming"):
        assert f"&quot;tab&quot;: &quot;{tab}&quot;" in html, f"missing tab payload: {tab}"
    assert 'aria-current="true"' in html


def test_unknown_tab_falls_back_to_soul():
    html = render(make_orch(), {"tab": "bogus"})
    assert 'name="profession"' in html


def test_soul_escapes_profile_values():
    orch = make_orch()
    orch.personalization_service.repo.profile["profession"] = "<script>alert(1)</script>"
    html = render(orch)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_memory_tab_lists_items_with_edit_and_delete_actions():
    orch = make_orch()
    html = render(orch, {"tab": "memory"})
    assert 'value="prefers tables"' in html
    assert 'name="value"' in html
    assert 'data-ui-action="chrome_memory_update"' in html
    assert 'data-ui-action="chrome_memory_delete"' in html
    assert "&quot;id&quot;: &quot;m1&quot;" in html
    assert "2023" in html  # created timestamp rendered


def test_memory_tab_escapes_values():
    orch = make_orch()
    orch.personalization_service.repo.memory[0]["value"] = '<img onerror="x">'
    html = render(orch, {"tab": "memory"})
    assert "<img" not in html
    assert "&lt;img" in html


def test_skills_tab_renders_toggle_and_unavailable_reason():
    html = render(make_orch(), {"tab": "skills"})
    # Authorized + enabled skill gets exactly one toggle (to disable it).
    assert html.count('data-ui-action="chrome_skill_toggle"') == 1
    assert "&quot;tool_name&quot;: &quot;search_docs&quot;" in html
    assert "&quot;enabled&quot;: false" in html
    # Unauthorized skill renders the reason, not a toggle.
    assert "tools:system" in html
    assert "haven&#x27;t been granted" in html


def test_schedule_tab_lists_jobs_with_actions_history_and_chat_hint(monkeypatch):
    monkeypatch.setitem(flags._flags, "scheduler_execution", True)
    jobs = [_job("j1", "active"), _job("j2", "paused", name="Weekly report"),
            _job("j3", "disabled", name="Ghost job")]
    runs = [{"id": "r1", "job_id": "j1", "user_id": "u1",
             "started_at": 1700000000000, "outcome": "success",
             "summary": "all good", "correlation_id": "c1"}]
    html = render(make_orch(jobs=jobs, runs=runs), {"tab": "schedule"})
    assert "Daily digest" in html and "Weekly report" in html
    assert "Ghost job" not in html  # soft-deleted jobs hidden
    assert 'data-ui-action="chrome_job_pause"' in html
    assert 'data-ui-action="chrome_job_run_now"' in html
    assert 'data-ui-action="chrome_job_resume"' in html
    assert 'data-ui-action="chrome_job_delete"' in html
    assert "created in chat" in html  # creation hint (jobs created in chat)
    assert "all good" in html and "success" in html  # inline run history


def test_schedule_run_now_html_payload_has_one_stable_client_uuid(monkeypatch):
    monkeypatch.setitem(flags._flags, "scheduler_execution", True)
    job_id = str(uuid.uuid4())
    markup = render(
        make_orch(jobs=[_job(job_id, "active")]),
        {"tab": "schedule"},
    )

    payloads = _html_action_payloads(markup, "chrome_job_run_now")

    assert len(payloads) == 1
    assert payloads[0]["job_id"] == job_id
    submission_id = uuid.UUID(payloads[0]["submission_id"])
    assert submission_id.version == 4


def test_schedule_run_now_sdui_payload_has_one_stable_client_uuid(monkeypatch):
    monkeypatch.setitem(flags._flags, "scheduler_execution", True)
    job_id = str(uuid.uuid4())
    components = asyncio.run(
        surf.components(
            make_orch(jobs=[_job(job_id, "active")]),
            "u1",
            ["user"],
            {"tab": "schedule"},
        )
    )

    payloads = _sdui_action_payloads(components, "chrome_job_run_now")

    assert len(payloads) == 1
    assert payloads[0]["job_id"] == job_id
    submission_id = uuid.UUID(payloads[0]["submission_id"])
    assert submission_id.version == 4


def test_schedule_flag_off_omits_run_now_in_html_and_sdui(monkeypatch):
    monkeypatch.setitem(flags._flags, "scheduler_execution", False)
    job = _job(str(uuid.uuid4()), "active")
    orch = make_orch(jobs=[job])

    markup = render(orch, {"tab": "schedule"})
    components = asyncio.run(
        surf.components(orch, "u1", ["user"], {"tab": "schedule"})
    )

    assert _html_action_payloads(markup, "chrome_job_run_now") == []
    assert _sdui_action_payloads(components, "chrome_job_run_now") == []
    assert "unavailable" in markup.lower()


def test_dreaming_tab_renders_toggle_trigger_and_sweeps():
    html = render(make_orch(), {"tab": "dreaming"})
    assert 'data-ui-action="chrome_dreaming_toggle"' in html
    assert "&quot;enabled&quot;: false" in html  # currently on → toggle turns it off
    assert 'data-ui-action="chrome_dreaming_trigger"' in html
    assert "Reviewed 3 recent signal(s)." in html
    assert "considered 3, promoted 1" in html


def test_missing_subsystem_renders_error_notice_not_exception():
    orch = make_orch()
    orch.personalization_service = None
    html = render(orch)
    assert "not available" in html


# ---------------------------------------------------------------------------
# Handlers — soul
# ---------------------------------------------------------------------------

def test_profile_save_success_parses_goals_and_returns_success_notice():
    orch = make_orch()
    repo = orch.personalization_service.repo
    fields = {"profession": "data engineer", "goals": "ship it\nlearn rust",
              "personality_notes": "be brief"}
    key, params, notice = call(surf._handle_profile_save, orch, {"fields": fields})
    assert key == "personalization" and params["tab"] == "soul"
    assert "Profile saved." in notice
    assert repo.upsert_calls[0]["goals"] == ["ship it", "learn rust"]
    assert repo.upsert_calls[0]["profession"] == "data engineer"
    # Notes unchanged vs existing profile → personality untouched (None).
    assert repo.upsert_calls[0]["personality"] is None


def test_profile_save_changed_notes_merge_existing_personality():
    orch = make_orch()
    repo = orch.personalization_service.repo
    fields = {"profession": "researcher", "goals": "write more",
              "personality_notes": "be playful"}
    _, _, notice = call(surf._handle_profile_save, orch, {"fields": fields})
    assert "Profile saved." in notice
    personality = repo.upsert_calls[0]["personality"]
    assert personality["notes"] == "be playful"
    assert personality["tone"] == "warm"  # chat-set trait preserved


def test_profile_save_phi_rejected_preserves_draft_and_skips_persist():
    orch = make_orch()
    repo = orch.personalization_service.repo
    fields = {"profession": "123-45-6789", "goals": "", "personality_notes": ""}
    key, params, notice = call(surf._handle_profile_save, orch, {"fields": fields})
    assert key == "personalization" and params["tab"] == "soul"
    assert "protected health information" in notice
    assert repo.upsert_calls == []
    assert params["draft"]["profession"] == "123-45-6789"
    # The failed-save re-render prefills the submitted values (FR-016).
    html = render(orch, params)
    assert 'value="123-45-6789"' in html


def test_profile_save_without_fields_is_an_error_notice():
    orch = make_orch()
    _, _, notice = call(surf._handle_profile_save, orch, {})
    assert "No form data" in notice
    assert orch.personalization_service.repo.upsert_calls == []


def test_profile_save_validation_error_is_an_error_notice():
    orch = make_orch()
    fields = {"profession": "x" * 300, "goals": "", "personality_notes": ""}
    _, params, notice = call(surf._handle_profile_save, orch, {"fields": fields})
    assert "Couldn&#x27;t save" in notice or "Couldn" in notice
    assert orch.personalization_service.repo.upsert_calls == []
    assert "draft" in params


# ---------------------------------------------------------------------------
# Handlers — memory
# ---------------------------------------------------------------------------

def test_memory_update_success_and_not_found():
    orch = make_orch()
    repo = orch.personalization_service.repo
    key, params, notice = call(
        surf._handle_memory_update, orch,
        {"id": "m1", "fields": {"value": "prefers charts"}})
    assert key == "personalization" and params["tab"] == "memory"
    assert "Memory updated." in notice
    assert repo.memory[0]["value"] == "prefers charts"
    _, _, notice = call(surf._handle_memory_update, orch,
                        {"id": "zz", "fields": {"value": "x"}})
    assert "not found" in notice


def test_memory_update_phi_rejected():
    orch = make_orch()
    _, _, notice = call(surf._handle_memory_update, orch,
                        {"id": "m1", "fields": {"value": "SSN 123-45-6789"}})
    assert "protected health information" in notice
    assert orch.personalization_service.repo.memory[0]["value"] == "prefers tables"


def test_memory_delete_success_then_not_found():
    orch = make_orch()
    _, params, notice = call(surf._handle_memory_delete, orch, {"id": "m1"})
    assert params["tab"] == "memory" and "Memory deleted." in notice
    assert orch.personalization_service.repo.memory == []
    _, _, notice = call(surf._handle_memory_delete, orch, {"id": "m1"})
    assert "not found" in notice


# ---------------------------------------------------------------------------
# Handlers — skills
# ---------------------------------------------------------------------------

def test_skill_toggle_enable_beyond_scope_is_denied():
    orch = make_orch()
    _, params, notice = call(
        surf._handle_skill_toggle, orch,
        {"agent_id": "helper", "tool_name": "wipe_disk", "enabled": True})
    assert params["tab"] == "skills"
    assert "tools:system" in notice
    assert orch.tool_permissions.override_calls == []


def test_skill_toggle_disable_succeeds_and_records_override():
    orch = make_orch()
    _, _, notice = call(
        surf._handle_skill_toggle, orch,
        {"agent_id": "helper", "tool_name": "search_docs", "enabled": False})
    assert "Disabled" in notice
    # 027 fix: must route through set_skill_enabled (per-kind row), NOT the
    # legacy NULL-kind set_tool_overrides path that per-kind rows outrank.
    assert orch.tool_permissions.skill_calls == [("u1", "helper", "search_docs", False)]
    assert orch.tool_permissions.override_calls == []


# ---------------------------------------------------------------------------
# Handlers — schedule
# ---------------------------------------------------------------------------

def test_job_pause_delete_cancel_unstarted_and_resume_stays_definition_only(
    monkeypatch,
):
    orch = make_orch(jobs=[_job("j1", "active")])
    store = FakeScheduleActionStore(orch.history.db)
    monkeypatch.setattr(surf, "_job_store", lambda _orch: store)
    _, params, notice = call(surf._handle_job_pause, orch, {"job_id": "j1"})
    assert params["tab"] == "schedule" and "Job paused." in notice
    assert orch.history.db.jobs[0]["status"] == "paused"
    _, _, notice = call(surf._handle_job_resume, orch, {"job_id": "j1"})
    assert "Job resumed." in notice
    assert orch.history.db.jobs[0]["status"] == "active"
    _, _, notice = call(surf._handle_job_delete, orch, {"job_id": "j1"})
    assert "Job deleted." in notice
    assert orch.history.db.jobs[0]["status"] == "disabled"
    _, _, notice = call(surf._handle_job_pause, orch, {"job_id": "nope"})
    assert "not found" in notice

    assert store.atomic_status_calls == [
        {
            "user_id": "u1",
            "job_id": "j1",
            "status": "paused",
            "terminal_code": "cancelled_job_paused",
        },
        {
            "user_id": "u1",
            "job_id": "j1",
            "status": "disabled",
            "terminal_code": "cancelled_job_deleted",
        },
        {
            "user_id": "u1",
            "job_id": "nope",
            "status": "paused",
            "terminal_code": "cancelled_job_paused",
        },
    ]
    assert store.legacy_status_calls == [("u1", "j1", "active")]


def test_job_run_now_replays_canonical_occurrence_without_direct_sql(monkeypatch):
    monkeypatch.setitem(flags._flags, "scheduler_execution", True)
    job_id = str(uuid.uuid4())
    orch = make_orch(jobs=[_job(job_id, "active")])
    store = FakeScheduleActionStore(orch.history.db)
    monkeypatch.setattr(surf, "_job_store", lambda _orch: store)
    submission_id = uuid.uuid4()
    payload = {"job_id": job_id, "submission_id": str(submission_id)}

    _, _, notice = call(surf._handle_job_run_now, orch, payload)
    assert "queued" in notice.lower()
    _, _, replay_notice = call(surf._handle_job_run_now, orch, payload)

    assert "queued" in replay_notice.lower() or "already" in replay_notice.lower()
    assert [call_["submission_id"] for call_ in store.run_now_calls] == [
        submission_id,
        submission_id,
    ]
    assert orch.history.db.executed == []


def test_job_run_now_flag_off_refuses_without_store_or_sql(monkeypatch):
    monkeypatch.setitem(flags._flags, "scheduler_execution", False)
    job_id = str(uuid.uuid4())
    orch = make_orch(jobs=[_job(job_id, "active")])
    store = FakeScheduleActionStore(orch.history.db)
    monkeypatch.setattr(surf, "_job_store", lambda _orch: store)

    _, _, notice = call(
        surf._handle_job_run_now,
        orch,
        {"job_id": job_id, "submission_id": str(uuid.uuid4())},
    )

    assert "unavailable" in notice.lower() or "disabled" in notice.lower()
    assert store.run_now_calls == []
    assert orch.history.db.executed == []


def test_job_run_now_requires_client_submission_uuid(monkeypatch):
    monkeypatch.setitem(flags._flags, "scheduler_execution", True)
    job_id = str(uuid.uuid4())
    orch = make_orch(jobs=[_job(job_id, "active")])
    store = FakeScheduleActionStore(orch.history.db)
    monkeypatch.setattr(surf, "_job_store", lambda _orch: store)

    _, _, missing = call(surf._handle_job_run_now, orch, {"job_id": job_id})
    _, _, invalid = call(
        surf._handle_job_run_now,
        orch,
        {"job_id": job_id, "submission_id": "not-a-uuid"},
    )

    assert "submission" in missing.lower()
    assert "submission" in invalid.lower()
    assert store.run_now_calls == []
    assert orch.history.db.executed == []


# ---------------------------------------------------------------------------
# Handlers — dreaming
# ---------------------------------------------------------------------------

def test_dreaming_toggle_persists_flag():
    orch = make_orch()
    _, params, notice = call(surf._handle_dreaming_toggle, orch, {"enabled": False})
    assert params["tab"] == "dreaming" and "Dreaming disabled." in notice
    assert orch.personalization_service.repo.dreaming_calls == [False]
    _, _, notice = call(surf._handle_dreaming_toggle, orch, {"enabled": True})
    assert "Dreaming enabled." in notice


def test_dreaming_trigger_runs_sweep_and_reports_counts():
    orch = make_orch()
    repo = orch.personalization_service.repo
    repo.signals = [{"id": "sig1", "category": "preference",
                     "value": "likes concise tables", "recall_count": 3,
                     "last_seen_at": 1700000000000}]
    _, params, notice = call(surf._handle_dreaming_trigger, orch, {})
    assert params["tab"] == "dreaming"
    assert "considered 1" in notice and "promoted 1" in notice
    assert any(m["value"] == "likes concise tables" for m in repo.memory)
    assert repo.sweeps[0]["trigger"] == "manual"


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------

def test_module_contract_title_and_handlers():
    assert surf.TITLE == "Personalization"
    expected = {
        "chrome_profile_save", "chrome_memory_update", "chrome_memory_delete",
        "chrome_skill_toggle", "chrome_job_pause", "chrome_job_resume",
        "chrome_job_delete", "chrome_job_run_now", "chrome_dreaming_toggle",
        "chrome_dreaming_trigger",
    }
    assert set(surf.HANDLERS) == expected
    assert not getattr(surf, "ADMIN_ONLY", False)
# ---------------------------------------------------------------------------
# 052 -- PHI reject-field labels + SDUI components() schedule tab
# ---------------------------------------------------------------------------

def test_phi_reject_field_labels_each_field(monkeypatch):
    class _Gate:
        def contains_phi(self, value):
            return "PHI" in str(value)

    monkeypatch.setattr(surf, "get_phi_gate", lambda: _Gate())
    body = SimpleNamespace(profession="engineer", goals=["fine", "PHI goal"])
    assert surf._phi_reject_field(body, None) == "goals"
    body = SimpleNamespace(profession=None, goals=[])
    assert surf._phi_reject_field(body, "PHI notes") == "personality notes"
    body = SimpleNamespace(profession="PHI job", goals=[])
    assert surf._phi_reject_field(body, None) == "profession"
    assert surf._phi_reject_field(SimpleNamespace(profession="x", goals=["y"]), "z") is None


def test_components_schedule_tab_via_sdui_entry():
    comps = asyncio.run(surf.components(make_orch(), "u1", ["user"], {"tab": "schedule"}))
    assert comps and len(comps) >= 2
