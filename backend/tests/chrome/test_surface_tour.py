"""Feature 027 — T017: ``tour`` surface render + ``chrome_tour_event`` handler.

Structural/behavioral tests against a minimal fake orchestrator (no
Postgres): the render path mirrors ``GET /api/tutorial/steps`` (audience
filtering, escaped ``data-tour-steps`` JSON holder) and the handler
mirrors the onboarding-state endpoint internals (replay/in_progress,
completed, skipped, dismiss). Audit recorder calls are monkeypatched at
the surface module's namespace and asserted per event transition.
"""
import html
import json
import re
from types import SimpleNamespace

import pytest

from orchestrator.projection_surfaces import tour


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _step(i, slug, audience="user", title=None, body=None,
          target_kind="static", target_key=None):
    return SimpleNamespace(
        id=i, slug=slug, audience=audience, display_order=i * 10,
        target_kind=target_kind, target_key=target_key,
        title=title if title is not None else f"Title {i}",
        body=body if body is not None else f"Body {i}",
    )


class FakeRepo:
    """Just the OnboardingRepository surface area tour.py touches."""

    def __init__(self, steps=None, state_status="not_started",
                 prior_status="not_started", audiences=None):
        self.steps = steps or []
        self.state_status = state_status
        self.prior_status = prior_status
        self.audiences = audiences or {}
        self.calls = []

    def list_steps_for_user(self, *, include_admin):
        self.calls.append(("list_steps_for_user", include_admin))
        if include_admin:
            return list(self.steps)
        return [s for s in self.steps if s.audience == "user"]

    def get_state(self, user_id):
        self.calls.append(("get_state", user_id))
        return SimpleNamespace(status=self.state_status, last_step_slug=None,
                               dismiss_count=0)

    def upsert_state(self, *, user_id, status, last_step_id):
        self.calls.append(("upsert_state", user_id, status, last_step_id))
        new_state = SimpleNamespace(status=status, last_step_slug="some-slug",
                                    dismiss_count=0)
        return new_state, self.prior_status

    def record_dismissal(self, user_id, max_dismissals=2):
        self.calls.append(("record_dismissal", user_id, max_dismissals))
        return SimpleNamespace(status="not_started", dismiss_count=1,
                               last_step_slug=None)

    def get_step_audience(self, step_id):
        self.calls.append(("get_step_audience", step_id))
        return self.audiences.get(step_id)


def _orch(repo):
    return SimpleNamespace(onboarding_repo=repo, ui_sessions={})


class _AuditSpy:
    """Capture the onboarding audit-recorder calls tour.py makes."""

    def __init__(self, monkeypatch):
        self.events = []
        for name in ("record_onboarding_replayed", "record_onboarding_started",
                     "record_onboarding_completed", "record_onboarding_skipped"):
            monkeypatch.setattr(tour, name, self._make(name))

    def _make(self, name):
        async def _spy(**kwargs):
            self.events.append((name, kwargs))
        return _spy

    def names(self):
        return [n for n, _ in self.events]


def _holder_json(rendered):
    m = re.search(r"data-tour-steps='([^']*)'", rendered)
    assert m, "missing data-tour-steps holder"
    return json.loads(html.unescape(m.group(1)))


WS = object()


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def test_module_contract():
    assert tour.TITLE == "Take the tour"
    assert getattr(tour, "ADMIN_ONLY", False) is False
    assert set(tour.HANDLERS) == {"chrome_tour_event"}


async def test_render_intro_and_step_holder():
    repo = FakeRepo(steps=[_step(1, "welcome", target_key="topbar.brand"),
                           _step(2, "settings")])
    out = await tour.render(_orch(repo), "u1", ["user"], {})
    assert "<p" in out and "guided tour" in out
    steps = _holder_json(out)
    assert [s["slug"] for s in steps] == ["welcome", "settings"]
    # Exactly the contracted fields, audience filtered server-side.
    assert set(steps[0]) == {"id", "slug", "title", "body", "target_kind",
                             "target_key", "display_order"}
    assert steps[0]["target_key"] == "topbar.brand"
    assert ("list_steps_for_user", False) in repo.calls


async def test_render_includes_admin_steps_only_for_admin():
    repo = FakeRepo(steps=[_step(1, "u-step"), _step(2, "a-step", audience="admin")])
    out_user = await tour.render(_orch(repo), "u1", ["user"], {})
    assert [s["slug"] for s in _holder_json(out_user)] == ["u-step"]
    out_admin = await tour.render(_orch(repo), "a1", ["admin", "user"], {})
    assert [s["slug"] for s in _holder_json(out_admin)] == ["u-step", "a-step"]
    assert ("list_steps_for_user", True) in repo.calls


async def test_render_escapes_step_content_and_round_trips():
    evil = '<script>alert("x")</script>'
    repo = FakeRepo(steps=[_step(1, "s", title=evil, body="it's & <b>fine</b>")])
    out = await tour.render(_orch(repo), "u1", ["user"], {})
    assert "<script>" not in out
    steps = _holder_json(out)  # client JSON.parse sees the original text
    assert steps[0]["title"] == evil
    assert steps[0]["body"] == "it's & <b>fine</b>"


async def test_render_no_steps_renders_notice_without_holder():
    out = await tour.render(_orch(FakeRepo(steps=[])), "u1", ["user"], {})
    assert "data-tour-steps" not in out
    assert "No tour steps are available" in out


async def test_render_without_repo_renders_error_notice():
    out = await tour.render(SimpleNamespace(ui_sessions={}), "u1", ["user"], {})
    assert "data-tour-steps" not in out
    assert "unavailable" in out


# ---------------------------------------------------------------------------
# chrome_tour_event handler
# ---------------------------------------------------------------------------

async def test_started_records_replay_and_transitions_in_progress(monkeypatch):
    spy = _AuditSpy(monkeypatch)
    repo = FakeRepo(state_status="not_started", prior_status="not_started")
    res = await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": "started"})
    assert res is None  # tour runs outside the modal — no re-render
    assert ("upsert_state", "u1", "in_progress", None) in repo.calls
    assert spy.names() == ["record_onboarding_replayed", "record_onboarding_started"]
    assert spy.events[0][1]["prior_status"] == "not_started"
    assert spy.events[0][1]["actor_user_id"] == "u1"


async def test_started_from_terminal_state_keeps_replay_only(monkeypatch):
    """PUT's terminal -> in_progress 409 guard becomes a no-op upsert here."""
    spy = _AuditSpy(monkeypatch)
    repo = FakeRepo(state_status="completed")
    res = await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": "started"})
    assert res is None
    assert not any(c[0] == "upsert_state" for c in repo.calls)
    assert spy.names() == ["record_onboarding_replayed"]
    assert spy.events[0][1]["prior_status"] == "completed"


async def test_started_in_progress_upserts_without_started_audit(monkeypatch):
    spy = _AuditSpy(monkeypatch)
    repo = FakeRepo(state_status="in_progress", prior_status="in_progress")
    await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": "started"})
    assert ("upsert_state", "u1", "in_progress", None) in repo.calls
    assert spy.names() == ["record_onboarding_replayed"]


@pytest.mark.parametrize("event,recorder", [
    ("completed", "record_onboarding_completed"),
    ("skipped", "record_onboarding_skipped"),
])
async def test_terminal_events_upsert_then_audit(monkeypatch, event, recorder):
    spy = _AuditSpy(monkeypatch)
    repo = FakeRepo(prior_status="in_progress")
    res = await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": event})
    assert res is None
    assert ("upsert_state", "u1", event, None) in repo.calls
    assert spy.names() == [recorder]
    assert spy.events[0][1]["last_step_slug"] == "some-slug"


async def test_completed_idempotent_no_duplicate_audit(monkeypatch):
    spy = _AuditSpy(monkeypatch)
    repo = FakeRepo(prior_status="completed")
    await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": "completed"})
    assert ("upsert_state", "u1", "completed", None) in repo.calls
    assert spy.names() == []


async def test_dismissed_uses_dismiss_internals(monkeypatch):
    spy = _AuditSpy(monkeypatch)
    repo = FakeRepo()
    res = await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": "dismissed"})
    assert res is None
    assert ("record_dismissal", "u1", 2) in repo.calls
    assert not any(c[0] == "upsert_state" for c in repo.calls)
    assert spy.names() == []  # dismissal audits nothing, like POST /dismiss


async def test_unknown_or_missing_event_is_dropped(monkeypatch):
    spy = _AuditSpy(monkeypatch)
    repo = FakeRepo()
    for payload in ({"event": "exploded"}, {}, None):
        res = await tour.HANDLERS["chrome_tour_event"](
            _orch(repo), WS, "u1", ["user"], payload)
        assert res is None
    assert repo.calls == [] and spy.names() == []


async def test_missing_repo_drops_event_without_raising(monkeypatch):
    spy = _AuditSpy(monkeypatch)
    res = await tour.HANDLERS["chrome_tour_event"](
        SimpleNamespace(ui_sessions={}), WS, "u1", ["user"], {"event": "completed"})
    assert res is None and spy.names() == []


async def test_step_id_validated_like_put_state(monkeypatch):
    _AuditSpy(monkeypatch)
    # Valid user-audience step id passes through to the upsert.
    repo = FakeRepo(prior_status="in_progress", audiences={7: "user"})
    await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": "completed", "step_id": 7})
    assert ("upsert_state", "u1", "completed", 7) in repo.calls

    # Unknown/archived step id is dropped, not raised (mid-tour resilience).
    repo = FakeRepo(prior_status="in_progress", audiences={})
    await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": "completed", "step_id": 99})
    assert ("upsert_state", "u1", "completed", None) in repo.calls

    # Admin-only step id is dropped for a non-admin, kept for an admin.
    repo = FakeRepo(prior_status="in_progress", audiences={5: "admin"})
    await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": "skipped", "step_id": 5})
    assert ("upsert_state", "u1", "skipped", None) in repo.calls
    repo = FakeRepo(prior_status="in_progress", audiences={5: "admin"})
    await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "a1", ["admin"], {"event": "skipped", "step_id": 5})
    assert ("upsert_state", "a1", "skipped", 5) in repo.calls

    # Non-integer step id is dropped.
    repo = FakeRepo(prior_status="in_progress", audiences={})
    await tour.HANDLERS["chrome_tour_event"](
        _orch(repo), WS, "u1", ["user"], {"event": "skipped", "step_id": "abc"})
    assert ("upsert_state", "u1", "skipped", None) in repo.calls


async def test_principal_prefers_jwt_claims(monkeypatch):
    spy = _AuditSpy(monkeypatch)
    repo = FakeRepo(state_status="not_started", prior_status="not_started")
    ws = object()
    orch = SimpleNamespace(
        onboarding_repo=repo,
        ui_sessions={ws: {"preferred_username": "sam", "sub": "u1"}},
    )
    await tour.HANDLERS["chrome_tour_event"](
        orch, ws, "u1", ["user"], {"event": "started"})
    assert spy.events[0][1]["auth_principal"] == "sam"
