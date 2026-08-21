"""Feature 027 — T015: audit settings surface structural/behavioral tests.

Runs without Postgres: a minimal fake orchestrator exposes only
``audit_repo`` (the attribute the surface uses), and the audit_view
self-recording is observed through a fake process recorder installed via
``audit.recorder.set_recorder``. Assertion style follows
``backend/tests/chrome/test_topbar.py`` (structural, not byte-exact).
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from audit.recorder import set_recorder
from audit.schemas import EVENT_CLASSES, OUTCOMES, AuditEventDTO
from orchestrator.projection_surfaces import audit as audit_surface

EVENT_ID = "11111111-1111-1111-1111-111111111111"
EVENT_ID_2 = "33333333-3333-3333-3333-333333333333"


def make_dto(**overrides):
    """Build a valid AuditEventDTO with overridable fields."""
    base = dict(
        event_id=EVENT_ID,
        event_class="auth",
        action_type="auth.login_interactive",
        description="Interactive login",
        correlation_id="22222222-2222-2222-2222-222222222222",
        outcome="success",
        inputs_meta={"reason": "test"},
        outputs_meta={"returned_count": 1},
        started_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 6, 1, 12, 0, 1, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 1, 12, 0, 2, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return AuditEventDTO(**base)


class FakeRepo:
    """Stands in for orch.audit_repo; captures call kwargs."""

    def __init__(self, items=None, next_cursor=None, detail=None, fail_on_cursor=False):
        self.items = items or []
        self.next_cursor = next_cursor
        self.detail = detail
        self.fail_on_cursor = fail_on_cursor
        self.list_calls = []
        self.get_calls = []

    def list_for_user(self, user_id, **kwargs):
        self.list_calls.append((user_id, kwargs))
        if self.fail_on_cursor and kwargs.get("cursor"):
            raise ValueError("invalid cursor: boom")
        return list(self.items), self.next_cursor

    def get_for_user(self, user_id, event_id, availability_resolver=None):
        self.get_calls.append((user_id, event_id))
        return self.detail


class FakeRecorder:
    """Captures AuditEventCreate objects passed to record()."""

    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)
        return None


def make_orch(repo):
    # The production surface always builds Plane-backed artifact availability
    # checks before querying the audit repository.  This suite isolates the
    # surface HTML, so inject the same narrow repository seam used by the audit
    # API tests instead of constructing an application Plane runtime.
    attachments = SimpleNamespace(get_by_id=lambda _attachment_id, _user_id: None)
    return SimpleNamespace(
        audit_repo=repo,
        attachment_repository=attachments,
    )


@pytest.fixture
def recorder():
    rec = FakeRecorder()
    set_recorder(rec)
    yield rec
    set_recorder(None)


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------

def test_module_contract():
    assert audit_surface.TITLE == "Audit log"
    assert not getattr(audit_surface, "ADMIN_ONLY", False)
    assert "chrome_audit_page" in audit_surface.HANDLERS
    assert callable(audit_surface.HANDLERS["chrome_audit_page"])


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------

async def test_list_renders_filter_bar_rows_and_row_actions():
    repo = FakeRepo(items=[make_dto(), make_dto(event_id=EVENT_ID_2, outcome="failure")])
    html = await audit_surface.render(make_orch(repo), "user-1", ["user"], {})

    # Filter bar: data-ui-form container, both selects, keyword input, Apply.
    assert "data-ui-form" in html
    assert 'name="event_class"' in html and 'name="outcome"' in html and 'name="q"' in html
    for ec in EVENT_CLASSES:
        assert f">{ec}</option>" in html, f"missing event_class option: {ec}"
    for oc in OUTCOMES:
        assert f">{oc}</option>" in html, f"missing outcome option: {oc}"
    assert 'data-ui-action="chrome_audit_page"' in html
    assert 'data-ui-collect="true"' in html

    # Rows: recorded_at, event_class, action_type, outcome badge, snippet.
    assert "2026-06-01 12:00:02" in html
    assert "auth.login_interactive" in html
    assert ">success</span>" in html and ">failure</span>" in html
    assert "Interactive login" in html

    # Row click opens the detail view via chrome_open with event_id.
    assert 'data-ui-action="chrome_open"' in html
    assert f"&quot;event_id&quot;: &quot;{EVENT_ID}&quot;" in html
    assert f"&quot;event_id&quot;: &quot;{EVENT_ID_2}&quot;" in html

    # Reads stay scoped to the websocket user.
    assert repo.list_calls and repo.list_calls[0][0] == "user-1"


async def test_list_passes_filters_and_cursor_to_repo():
    repo = FakeRepo()
    params = {"event_class": "auth", "outcome": "failure", "q": "login", "cursor": "c|1"}
    await audit_surface.render(make_orch(repo), "user-1", ["user"], params)

    _, kwargs = repo.list_calls[0]
    assert kwargs["event_classes"] == ["auth"]
    assert kwargs["outcomes"] == ["failure"]
    assert kwargs["keyword"] == "login"
    assert kwargs["cursor"] == "c|1"
    assert kwargs["limit"] == 50


async def test_list_drops_invalid_filter_values():
    repo = FakeRepo()
    params = {"event_class": "bogus", "outcome": "nope"}
    await audit_surface.render(make_orch(repo), "user-1", ["user"], params)

    _, kwargs = repo.list_calls[0]
    assert kwargs["event_classes"] is None
    assert kwargs["outcomes"] is None


async def test_list_selected_filters_round_trip_into_form():
    repo = FakeRepo()
    html = await audit_surface.render(
        make_orch(repo), "user-1", ["user"],
        {"event_class": "auth", "q": "needle <tag>"},
    )
    assert 'value="auth" selected' in html
    # Keyword echoes back escaped, preserving the submitted value (FR-016).
    assert "needle &lt;tag&gt;" in html and "<tag>" not in html


async def test_list_next_button_carries_cursor_and_filters():
    repo = FakeRepo(items=[make_dto()], next_cursor="2026-06-01T12:00:02+00:00|" + EVENT_ID)
    html = await audit_surface.render(
        make_orch(repo), "user-1", ["user"], {"event_class": "auth", "q": "x"}
    )
    assert ">Next</button>" in html
    assert "&quot;cursor&quot;:" in html
    assert "&quot;event_class&quot;: &quot;auth&quot;" in html
    assert "&quot;q&quot;: &quot;x&quot;" in html


async def test_list_invalid_cursor_falls_back_to_first_page_with_notice():
    repo = FakeRepo(items=[make_dto()], fail_on_cursor=True)
    html = await audit_surface.render(
        make_orch(repo), "user-1", ["user"], {"cursor": "garbage"}
    )
    # Error notice rendered, then the query retried without the cursor.
    assert "astral-chrome-notice" in html and "Invalid page cursor" in html
    assert len(repo.list_calls) == 2
    assert repo.list_calls[1][1]["cursor"] is None
    assert "Interactive login" in html  # first page still shown


async def test_list_empty_state():
    repo = FakeRepo(items=[])
    html = await audit_surface.render(make_orch(repo), "user-1", ["user"], {})
    assert "No audit entries match the current filters." in html


async def test_list_escapes_dynamic_text():
    dto = make_dto(description="<script>alert(1)</script> & more")
    repo = FakeRepo(items=[dto])
    html = await audit_surface.render(make_orch(repo), "user-1", ["user"], {})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "&amp; more" in html


async def test_list_records_audit_view_list(recorder):
    repo = FakeRepo(items=[make_dto()])
    await audit_surface.render(
        make_orch(repo), "user-1", ["user"], {"event_class": "auth", "q": "z"}
    )
    assert len(recorder.events) == 1
    ev = recorder.events[0]
    assert ev.actor_user_id == "user-1"
    assert ev.event_class == "audit_view"
    assert ev.action_type == "audit_view.list"
    assert ev.outcome == "success"
    assert ev.inputs_meta["filters"]["event_class"] == ["auth"]
    assert ev.inputs_meta["filters"]["has_q"] is True
    assert ev.outputs_meta == {"returned_count": 1}


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------

async def test_detail_renders_full_fields_and_back_link():
    dto = make_dto(
        agent_id="grants",
        conversation_id="conv-9",
        outcome_detail="all good",
        inputs_meta={"key": "<value>"},
    )
    repo = FakeRepo(detail=dto)
    html = await audit_surface.render(
        make_orch(repo), "user-1", ["user"], {"event_id": EVENT_ID}
    )
    assert repo.get_calls == [("user-1", EVENT_ID)]
    for needle in (
        EVENT_ID, "auth.login_interactive", "Interactive login",
        "22222222-2222-2222-2222-222222222222",  # correlation_id
        "grants", "conv-9", "all good",
        "2026-06-01 12:00:00", "2026-06-01 12:00:02",
    ):
        assert needle in html, f"detail missing: {needle}"
    # Pretty-printed, escaped metadata inside <pre> blocks.
    assert "<pre" in html
    assert "&lt;value&gt;" in html and "<value>" not in html
    # Back link re-opens the audit list.
    assert 'data-ui-action="chrome_open"' in html
    assert "&quot;surface&quot;: &quot;audit&quot;" in html
    assert "Back to audit log" in html


async def test_detail_not_found_renders_error_with_back_link():
    repo = FakeRepo(detail=None)
    html = await audit_surface.render(
        make_orch(repo), "user-1", ["user"], {"event_id": "not-a-real-id"}
    )
    assert "astral-chrome-error" in html
    assert "Audit event not found." in html
    assert "Back to audit log" in html


async def test_detail_records_audit_view_detail(recorder):
    repo = FakeRepo(detail=make_dto())
    await audit_surface.render(
        make_orch(repo), "user-1", ["user"], {"event_id": EVENT_ID}
    )
    assert len(recorder.events) == 1
    ev = recorder.events[0]
    assert ev.event_class == "audit_view"
    assert ev.action_type == "audit_view.detail"
    assert ev.inputs_meta == {"event_id": EVENT_ID}


async def test_detail_not_found_records_nothing(recorder):
    repo = FakeRepo(detail=None)
    await audit_surface.render(
        make_orch(repo), "user-1", ["user"], {"event_id": EVENT_ID}
    )
    assert recorder.events == []


# ---------------------------------------------------------------------------
# chrome_audit_page handler
# ---------------------------------------------------------------------------

async def test_handler_builds_params_from_fields():
    handler = audit_surface.HANDLERS["chrome_audit_page"]
    payload = {"fields": {
        "event_class": "auth", "outcome": "", "q": "  spaced  ", "cursor": "c|1",
    }}
    surface, params, notice = await handler(None, None, "user-1", ["user"], payload)
    assert surface == "audit"
    assert params == {"event_class": "auth", "q": "spaced", "cursor": "c|1"}
    assert notice == ""


async def test_handler_tolerates_missing_fields():
    handler = audit_surface.HANDLERS["chrome_audit_page"]
    surface, params, notice = await handler(None, None, "user-1", ["user"], {})
    assert surface == "audit"
    assert params == {}
    assert notice == ""
