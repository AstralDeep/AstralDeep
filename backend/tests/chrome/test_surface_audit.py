"""Tests for orchestrator/projection_surfaces/audit.py: list and detail rendering,
filter and cursor handling, and self-recording via audit.recorder, against a fake
orchestrator exposing only audit_repo.
"""

from datetime import datetime, timedelta, timezone
import html as html_module
import json
import re
from types import SimpleNamespace

import pytest

from audit.recorder import set_recorder
from audit.schemas import EVENT_CLASSES, OUTCOMES, AuditEventDTO
from orchestrator.projection_surfaces import audit as audit_surface

EVENT_ID = "11111111-1111-1111-1111-111111111111"
EVENT_ID_2 = "33333333-3333-3333-3333-333333333333"


def make_dto(**overrides):
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
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)
        return None


def make_orch(repo):
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


def test_module_contract():
    assert audit_surface.TITLE == "Audit log"
    assert not getattr(audit_surface, "ADMIN_ONLY", False)
    assert "chrome_audit_page" in audit_surface.HANDLERS
    assert callable(audit_surface.HANDLERS["chrome_audit_page"])


async def test_list_renders_filter_bar_rows_and_row_actions():
    repo = FakeRepo(items=[make_dto(), make_dto(event_id=EVENT_ID_2, outcome="failure")])
    html = await audit_surface.render(make_orch(repo), "user-1", ["user"], {})

    assert "data-ui-form" in html
    assert 'name="event_class"' in html and 'name="outcome"' in html and 'name="q"' in html
    for ec in EVENT_CLASSES:
        assert f">{ec}</option>" in html, f"missing event_class option: {ec}"
    for oc in OUTCOMES:
        assert f">{oc}</option>" in html, f"missing outcome option: {oc}"
    assert 'data-ui-action="chrome_audit_page"' in html
    assert 'data-ui-collect="true"' in html

    assert "2026-06-01 12:00:02" in html
    assert "auth.login_interactive" in html
    assert ">success</span>" in html and ">failure</span>" in html
    assert "Interactive login" in html

    assert 'data-ui-action="chrome_open"' in html
    assert f"&quot;event_id&quot;: &quot;{EVENT_ID}&quot;" in html
    assert f"&quot;event_id&quot;: &quot;{EVENT_ID_2}&quot;" in html

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
    assert "astral-chrome-notice" in html and "Invalid page cursor" in html
    assert len(repo.list_calls) == 2
    assert repo.list_calls[1][1]["cursor"] is None
    assert "Interactive login" in html


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
        "22222222-2222-2222-2222-222222222222",
        "grants", "conv-9", "all good",
        "2026-06-01 12:00:00", "2026-06-01 12:00:02",
    ):
        assert needle in html, f"detail missing: {needle}"
    assert "<pre" in html
    assert "&lt;value&gt;" in html and "<value>" not in html
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


def _payloads(html):
    return [json.loads(html_module.unescape(value)) for value in
            re.findall(r"data-ui-payload='([^']+)'", html)]


async def test_dates_are_inclusive_utc_and_recorded(recorder):
    repo = FakeRepo()
    rendered = await audit_surface.render(make_orch(repo), "user-1", [], {
        "from": "2026-09-01", "to": "2026-09-11",
    })
    kwargs = repo.list_calls[0][1]
    assert kwargs["from_ts"] == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert kwargs["to_ts"] == datetime(2026, 9, 12, tzinfo=timezone.utc)
    assert 'value="2026-09-01"' in rendered
    assert recorder.events[0].inputs_meta["filters"]["from"] == "2026-09-01T00:00:00+00:00"
    assert recorder.events[0].inputs_meta["filters"]["to"] == "2026-09-12T00:00:00+00:00"


@pytest.mark.parametrize("params", [
    {"from": "not a date"}, {"to": "10000-01-01"},
    {"from": "20260911"}, {"to": "9999-12-31"},
    {"from": "2026-09-12", "to": "2026-09-11"},
])
async def test_invalid_dates_preserve_filter_form_without_query(params):
    repo = FakeRepo()
    rendered = await audit_surface.render(make_orch(repo), "user-1", [], params)
    assert "Choose valid dates" in rendered
    assert 'name="from"' in rendered
    assert repo.list_calls == []


@pytest.mark.parametrize("history", ["not json", "{}", '[1]', json.dumps([""] * 101),
                                      json.dumps(["x" * 513]), None])
def test_untrusted_history_is_bounded(history):
    assert audit_surface._history(history) == []


async def test_previous_next_and_detail_preserve_filtered_page():
    repo = FakeRepo(items=[make_dto()], next_cursor="next-page")
    params = {"cursor": "current-page", "history": '["", "prior-page"]',
              "from": "2026-09-01", "event_class": "auth", "q": "sign in"}
    rendered = await audit_surface.render(make_orch(repo), "user-1", [], params)
    payloads = _payloads(rendered)
    previous = next(p["fields"] for p in payloads if p.get("fields", {}).get("cursor") == "prior-page")
    following = next(p["fields"] for p in payloads if p.get("fields", {}).get("cursor") == "next-page")
    assert previous["from"] == following["from"] == "2026-09-01"
    assert previous["q"] == following["q"] == "sign in"
    assert json.loads(previous["history"]) == [""]
    assert json.loads(following["history"]) == ["", "prior-page", "current-page"]
    detail = next(p for p in payloads if p.get("params", {}).get("event_id"))
    repo.detail = make_dto()
    body = await audit_surface.render(make_orch(repo), "user-1", [], detail["params"])
    assert _payloads(body)[0]["params"] == detail["params"]["return_to"]
    assert repo.get_calls[-1] == ("user-1", EVENT_ID)


async def test_invalid_cursor_resets_navigation_history():
    repo = FakeRepo(items=[make_dto()], next_cursor="next", fail_on_cursor=True)
    rendered = await audit_surface.render(make_orch(repo), "user-1", [], {
        "cursor": "bad", "history": '["old"]',
    })
    assert ">Previous</button>" not in rendered
    next_page = next(p["fields"] for p in _payloads(rendered)
                     if p.get("fields", {}).get("cursor") == "next")
    assert json.loads(next_page["history"]) == [""]


async def test_repeated_navigation_is_disclosed_and_failures_stay_visible():
    repo = FakeRepo(items=[
        make_dto(action_type="ws.chrome_open"),
        make_dto(event_id=EVENT_ID_2, action_type="ws.chrome_close"),
        make_dto(action_type="ws.chrome_open", outcome="failure", description="Denied"),
    ])
    rendered = await audit_surface.render(make_orch(repo), "user-1", [], {})
    assert "2 navigation and audit views" in rendered
    assert '<details class="astral-collapsible ' in rendered
    assert rendered.count('class="astral-audit-row') == 3
    assert rendered.index("</details>") < rendered.index("Denied")
    assert "2026-06-01 (UTC)" in rendered


async def test_missing_detail_keeps_back_navigation_and_rejects_unknown_keys():
    rendered = await audit_surface.render(make_orch(FakeRepo()), "user-1", [], {
        "event_id": EVENT_ID, "return_to": {"q": "<script>", "owner_id": "other"},
    })
    assert _payloads(rendered)[0]["params"] == {"q": "<script>"}
    assert "<script>" not in rendered
    rendered = await audit_surface.render(make_orch(FakeRepo()), "user-1", [], {
        "event_id": EVENT_ID, "return_to": ["bad"],
    })
    assert _payloads(rendered)[0]["params"] == {}


async def test_page_handler_carries_date_bounds_and_cursor_stack():
    result = await audit_surface._handle_audit_page(None, None, "user-1", [], {
        "fields": {"from": "2026-09-01", "to": "2026-09-11", "history": '[""]'},
    })
    assert result[1] == {"from": "2026-09-01", "to": "2026-09-11", "history": '[""]'}


async def test_through_date_includes_last_microsecond_and_excludes_next_day():
    last_instant = datetime(2026, 9, 11, 23, 59, 59, 999999, tzinfo=timezone.utc)
    next_day = datetime(2026, 9, 12, tzinfo=timezone.utc)

    class HalfOpenRepo(FakeRepo):
        def list_for_user(self, user_id, **kwargs):
            super().list_for_user(user_id, **kwargs)
            return [dto for dto in self.items if kwargs["from_ts"] <= dto.recorded_at < kwargs["to_ts"]], None

    repo = HalfOpenRepo(items=[make_dto(recorded_at=next_day, description="Next day"),
                              make_dto(recorded_at=last_instant, description="Last instant")])
    body = await audit_surface.render(make_orch(repo), "user-1", [], {
        "from": "2026-09-11", "to": "2026-09-11",
    })
    assert "Last instant" in body and "Next day" not in body


async def test_day_groups_and_timestamps_are_normalized_to_utc():
    eastern = timezone(timedelta(hours=-4))
    repo = FakeRepo(items=[make_dto(recorded_at=datetime(2026, 9, 10, 22, tzinfo=eastern))])
    body = await audit_surface.render(make_orch(repo), "user-1", [], {})
    assert "2026-09-11 (UTC)" in body
    assert "2026-09-11 02:00:00" in body
    assert "2026-09-10" not in body


@pytest.mark.parametrize("payload", [None, [], "bad", {"fields": []}, {"fields": "bad"},
    {"fields": {"q": ["bad"], "cursor": {"owner_id": "other"}, "owner_id": "other"}}])
async def test_page_handler_discards_nonscalar_and_unknown_navigation(payload):
    assert await audit_surface._handle_audit_page(None, None, "user-1", [], payload) == ("audit", {}, "")


@pytest.mark.parametrize("params", [{"q": "x" * 257}, {"cursor": "x" * 513}, {"q": "bad\x00query"}])
async def test_invalid_filter_sizes_and_nulls_are_rejected_before_query(params):
    repo = FakeRepo()
    body = await audit_surface.render(make_orch(repo), "user-1", [], params)
    assert 'role="status"' in body and not repo.list_calls
    result = await audit_surface._handle_audit_page(None, None, "user-1", [], {"fields": params})
    assert result[1] == {} and result[2]


async def test_detail_return_cannot_amplify_oversized_or_nested_parameters():
    body = await audit_surface.render(make_orch(FakeRepo()), "user-1", [], {
        "event_id": EVENT_ID, "return_to": {"q": "x" * 257},
    })
    assert _payloads(body)[0]["params"] == {}
    body = await audit_surface.render(make_orch(FakeRepo()), "user-1", [], {
        "event_id": EVENT_ID, "return_to": {"q": {"owner_id": "other"}, "history": "not json"},
    })
    assert _payloads(body)[0]["params"] == {"history": "[]"}


def test_history_rejects_large_documents_and_excessive_nesting():
    assert audit_surface._history(" " * 16_385) == []
    assert audit_surface._history("[" * 1500 + "]" * 1500) == []
    assert audit_surface._history([]) == []


async def test_unexpected_query_validation_error_is_not_retried_as_bad_cursor():
    class BrokenRepo(FakeRepo):
        def list_for_user(self, user_id, **kwargs):
            super().list_for_user(user_id, **kwargs)
            raise ValueError("repository unavailable")
    repo = BrokenRepo()
    with pytest.raises(ValueError, match="repository unavailable"):
        await audit_surface.render(make_orch(repo), "user-1", [], {})
    assert len(repo.list_calls) == 1


async def test_missing_page_history_returns_previous_to_newest_with_filters():
    body = await audit_surface.render(make_orch(FakeRepo()), "user-1", [], {"cursor": "later", "q": "needle"})
    pages = [p["fields"] for p in _payloads(body) if p.get("fields", {}).get("q") == "needle"]
    assert any(p.get("cursor") == "" and p.get("history") == "[]" for p in pages)
    assert any("cursor" not in p and "history" not in p for p in pages)


async def test_date_and_navigation_grouping_preserves_order_and_every_event():
    rows = [make_dto(event_id=f"{n:08}-1111-1111-1111-111111111111",
                     recorded_at=datetime(2026, 9, 11 if n < 4 else 10, tzinfo=timezone.utc),
                     action_type="ws.chrome_open" if n in (1, 2, 4, 5) else "tool.run",
                     description=f"Record number {n}") for n in range(1, 6)]
    body = await audit_surface.render(make_orch(FakeRepo(items=rows)), "user-1", [], {})
    positions = [body.index(f"Record number {n}") for n in range(1, 6)]
    assert positions == sorted(positions)
    assert body.count('class="astral-audit-row') == 5
    assert body.count("<details ") == 2
