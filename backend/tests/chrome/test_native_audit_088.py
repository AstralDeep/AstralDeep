"""088 native audit parity through the authorized host adapter and dispatcher."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from orchestrator import chrome_events
from orchestrator.projection_surfaces import audit as audit_surface
from tests.chrome.test_chrome_surface import FakeOrch
from tests.chrome.test_surface_audit import (
    EVENT_ID,
    EVENT_ID_2,
    FakeRepo,
    make_dto,
    make_orch,
    recorder as recorder,
)


def _nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ("children", "content"):
            yield from _nodes(value.get(key))
        for tab in value.get("tabs") or []:
            yield from _nodes(tab.get("content"))


def _button(components, label):
    return next(node for node in _nodes(components)
                if node.get("type") == "button" and node.get("label") == label)


def _fields(components):
    form = next(node for node in _nodes(components) if node.get("type") == "param_picker")
    return {field["name"]: field for field in form["fields"]}


async def test_native_and_web_share_owner_date_query_and_availability_resolution():
    params = {
        "event_class": "auth", "outcome": "failure", "q": "  matching query  ",
        "from": "2026-09-01", "to": "2026-09-11", "cursor": "second-page",
        "history": '[""]', "owner_id": "another-owner", "user_id": "another-owner",
    }
    repo = FakeRepo(items=[make_dto()])
    orch = make_orch(repo)
    resolutions = []
    orch.attachment_repository = SimpleNamespace(
        get_by_id=lambda artifact, owner: resolutions.append((artifact, owner)))
    await audit_surface.render(orch, "verified-owner", [], params)
    components = await audit_surface.components(orch, "verified-owner", [], params)
    assert [call[0] for call in repo.list_calls] == ["verified-owner"] * 2
    web_query, native_query = [dict(call[1]) for call in repo.list_calls]
    for query in (web_query, native_query):
        resolver = query.pop("availability_resolver")
        assert resolver({"store": "user_attachments", "artifact_id": "attachment-1"}) is False
    assert resolutions == [("attachment-1", "verified-owner")] * 2
    assert native_query == web_query == {
        "limit": 50, "cursor": "second-page", "event_classes": ["auth"],
        "outcomes": ["failure"], "keyword": "matching query",
        "from_ts": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "to_ts": datetime(2026, 9, 12, tzinfo=timezone.utc),
    }
    assert _fields(components)["q"]["default"] == "matching query"
    assert "another-owner" not in json.dumps(components)


async def test_native_navigation_and_detail_back_keep_original_filtered_page():
    repo = FakeRepo(items=[make_dto()], next_cursor="following-page", detail=make_dto())
    orch = make_orch(repo)
    params = {
        "cursor": "current-page", "history": '["", "previous-page"]',
        "from": "2026-09-01", "to": "2026-09-11", "q": "sign in", "event_class": "auth",
    }
    components = await audit_surface.components(orch, "owner", [], params)
    previous = _button(components, "Previous")["payload"]["fields"]
    following = _button(components, "Next")["payload"]["fields"]
    newest = _button(components, "Newest")["payload"]["fields"]
    assert previous["cursor"] == "previous-page"
    assert json.loads(previous["history"]) == [""]
    assert following["cursor"] == "following-page"
    assert json.loads(following["history"]) == ["", "previous-page", "current-page"]
    assert "cursor" not in newest and "history" not in newest
    for page in (previous, following, newest):
        assert {key: page[key] for key in ("from", "to", "q", "event_class")} == {
            key: params[key] for key in ("from", "to", "q", "event_class")}
    detail_params = _button(components, "View details")["payload"]["params"]
    detail = await audit_surface.components(orch, "owner", [], detail_params)
    assert _button(detail, "Back to audit log")["payload"]["params"] == detail_params["return_to"]
    assert repo.get_calls == [("owner", EVENT_ID)]


@pytest.mark.parametrize("return_to,expected", [
    ({"q": "needle", "owner_id": "other-owner"}, {"q": "needle"}),
    ({"q": "x" * 257}, {}),
    ({"q": {"user_id": "other-owner"}, "history": "broken"}, {"history": "[]"}),
    (["malformed"], {}),
])
async def test_missing_or_foreign_detail_has_same_denial_and_safe_back(return_to, expected, recorder):
    repo = FakeRepo(detail=None)
    components = await audit_surface.components(make_orch(repo), "owner", ["admin"], {
        "event_id": EVENT_ID_2, "owner_id": "other-owner", "return_to": return_to,
    })
    assert repo.get_calls == [("owner", EVENT_ID_2)]
    assert _button(components, "Back to audit log")["payload"]["params"] == expected
    alerts = [node for node in _nodes(components) if node.get("type") == "alert"]
    assert alerts == [{"type": "alert", "message": "Audit event not found.", "variant": "error"}]
    assert recorder.events == []
    assert "other-owner" not in json.dumps(components)


@pytest.mark.parametrize("params", [
    {"q": "x" * 257}, {"cursor": "x" * 513}, {"q": "null\x00query"},
    {"from": "not-a-date"}, {"from": "20260911"}, {"to": "9999-12-31"},
    {"from": "2026-09-12", "to": "2026-09-11"},
])
async def test_invalid_filters_refuse_before_query_without_false_empty_result(params, recorder):
    repo = FakeRepo()
    components = await audit_surface.components(make_orch(repo), "owner", [], params)
    assert repo.list_calls == [] and recorder.events == []
    assert components[0]["type"] == "alert" and components[0]["variant"] == "error"
    assert {"event_class", "outcome", "q", "from", "to"} <= _fields(components).keys()
    assert "No audit entries match" not in json.dumps(components)


async def test_invalid_dates_preserve_valid_filters_for_correction():
    repo = FakeRepo()
    params = {"from": "2026-09-12", "to": "2026-09-11", "q": "correct me",
              "event_class": "auth", "outcome": "failure"}
    components = await audit_surface.components(make_orch(repo), "owner", [], params)
    assert repo.list_calls == []
    assert {key: field["default"] for key, field in _fields(components).items()} == params


@pytest.mark.parametrize("params", [None, [], "bad", {
    "q": ["nested"], "cursor": {"owner_id": "other-owner"},
    "event_class": "bogus", "outcome": "bogus", "owner_id": "other-owner",
}])
async def test_nonscalar_or_unknown_filters_never_retarget_owner(params):
    repo = FakeRepo()
    components = await audit_surface.components(make_orch(repo), "owner", [], params)
    assert repo.list_calls[0][0] == "owner"
    query = repo.list_calls[0][1]
    assert query["keyword"] is None and query["cursor"] is None
    assert query["event_classes"] is None and query["outcomes"] is None
    assert "other-owner" not in json.dumps(components)


async def test_stale_cursor_retries_same_owner_and_dates_then_resets_history(recorder):
    repo = FakeRepo(items=[make_dto()], next_cursor="new-next", fail_on_cursor=True)
    components = await audit_surface.components(make_orch(repo), "owner", [], {
        "cursor": "expired", "history": '["old-page"]', "q": "filter",
        "from": "2026-09-01", "to": "2026-09-11",
    })
    assert [call[0] for call in repo.list_calls] == ["owner", "owner"]
    first, retry = [dict(call[1]) for call in repo.list_calls]
    assert first.pop("cursor") == "expired" and retry.pop("cursor") is None
    assert first == retry
    assert components[0]["variant"] == "error"
    assert "Invalid page cursor" in components[0]["message"]
    assert not any(node.get("label") == "Previous" for node in _nodes(components))
    next_fields = _button(components, "Next")["payload"]["fields"]
    assert json.loads(next_fields["history"]) == [""]
    assert next_fields["q"] == "filter" and next_fields["from"] == "2026-09-01"
    assert len(recorder.events) == 1
    assert recorder.events[0].inputs_meta["filters"]["has_cursor"] is False


@pytest.mark.parametrize("cursor,expected_calls", [(None, 1), ("old", 2)])
async def test_repository_valueerror_propagates_instead_of_false_success(cursor, expected_calls, recorder):
    class UnavailableRepo(FakeRepo):
        def list_for_user(self, user_id, **kwargs):
            self.list_calls.append((user_id, kwargs))
            raise ValueError("repository unavailable")

    repo = UnavailableRepo()
    with pytest.raises(ValueError, match="repository unavailable"):
        await audit_surface.components(make_orch(repo), "owner", [], {"cursor": cursor})
    assert len(repo.list_calls) == expected_calls and recorder.events == []


async def test_native_reads_self_audit_without_keyword_or_record_contents(recorder):
    repo = FakeRepo(items=[make_dto()], detail=make_dto())
    orch = make_orch(repo)
    await audit_surface.components(orch, "owner", [], {
        "q": "private phrase", "outcome": "failure", "event_class": "auth",
        "from": "2026-09-01", "to": "2026-09-11",
    })
    await audit_surface.components(orch, "owner", [], {"event_id": EVENT_ID})
    assert [event.action_type for event in recorder.events] == ["audit_view.list", "audit_view.detail"]
    assert all(event.actor_user_id == event.auth_principal == "owner" for event in recorder.events)
    listed, detailed = recorder.events
    assert listed.inputs_meta["filters"] == {
        "event_class": ["auth"], "outcome": ["failure"],
        "from": "2026-09-01T00:00:00+00:00", "to": "2026-09-12T00:00:00+00:00",
        "has_q": True, "has_cursor": False,
    }
    assert listed.outputs_meta == {"returned_count": 1}
    assert detailed.inputs_meta == {"event_id": EVENT_ID}
    assert "private phrase" not in json.dumps([event.model_dump(mode="json") for event in recorder.events])


async def test_detail_exposes_same_public_fields_and_artifact_metadata_as_web():
    dto = make_dto(
        agent_id="research", conversation_id="conversation-1", outcome_detail="All good",
        inputs_meta={"label": "<not markup>"}, outputs_meta={"count": 3},
        artifact_pointers=[{"artifact_id": "attachment-1", "store": "user_attachments",
                            "extension": ".pdf", "size_bytes": 123456, "available": False}],
    )
    repo = FakeRepo(detail=dto)
    orch = make_orch(repo)
    native = await audit_surface.components(orch, "owner", [], {"event_id": EVENT_ID})
    html = await audit_surface.render(orch, "owner", [], {"event_id": EVENT_ID})
    serialized = json.dumps(native)
    for value in (EVENT_ID, dto.correlation_id, "research", "conversation-1", "All good",
                  "auth.login_interactive", "attachment-1", "user_attachments", ".pdf",
                  "123456", "no longer available", "2026-06-01 12:00:00"):
        assert value in html and value in serialized
    assert "<not markup>" in serialized
    assert not any(node.get("type") in ("html", "raw_html") for node in _nodes(native))


async def test_native_groups_utc_dates_without_hiding_failed_navigation():
    recorded = datetime(2026, 9, 10, 22, tzinfo=timezone(timedelta(hours=-4)))
    rows = [make_dto(recorded_at=recorded, action_type="ws.chrome_open"),
            make_dto(event_id=EVENT_ID_2, recorded_at=recorded, action_type="ws.chrome_close"),
            make_dto(recorded_at=recorded, action_type="ws.chrome_open", outcome="failure", description="Denied navigation")]
    components = await audit_surface.components(make_orch(FakeRepo(items=rows)), "owner", [], {})
    disclosures = [node for node in _nodes(components) if node.get("type") == "collapsible"]
    assert len(disclosures) == 1 and disclosures[0]["default_open"] is False
    assert "2 navigation and audit views" in disclosures[0]["title"]
    assert "Denied navigation" not in json.dumps(disclosures[0])
    assert "Denied navigation" in json.dumps(components)
    assert "2026-09-11 (UTC)" in json.dumps(components)
    assert "2026-09-11 02:00:00" in json.dumps(components)


@pytest.mark.parametrize("device", ["android", "ios", "macos"])
async def test_actual_chrome_dispatch_delivers_native_audit_and_page_actions(device, recorder, monkeypatch):
    orch = FakeOrch(device=device)
    repo = FakeRepo(items=[make_dto()], next_cursor="page-two", detail=make_dto())
    orch.audit_repo = repo
    orch.attachment_repository = make_orch(repo).attachment_repository
    monkeypatch.setattr(chrome_events, "_HANDLERS", None)
    assert await chrome_events.handle_chrome_event(orch, orch.ws, "chrome_open", {
        "surface": "audit", "params": {"q": "first", "user_id": "forged"},
    }, "verified-owner") is True
    frame = orch.sent[-1]
    assert frame["type"] == "chrome_surface" and frame["surface_key"] == "audit"
    assert frame["admin_only"] is False and frame["title"] == "Audit log"
    assert "No audit entries match" not in json.dumps(frame)
    assert repo.list_calls[-1][0] == "verified-owner"
    next_button = _button(frame["components"], "Next")
    await chrome_events.handle_chrome_event(orch, orch.ws, next_button["action"], next_button["payload"], "verified-owner")
    assert repo.list_calls[-1][1]["cursor"] == "page-two"
    assert repo.list_calls[-1][1]["keyword"] == "first"
    detail = _button(orch.sent[-1]["components"], "View details")
    await chrome_events.handle_chrome_event(orch, orch.ws, detail["action"], detail["payload"], "verified-owner")
    assert repo.get_calls == [("verified-owner", EVENT_ID)]
    assert _button(orch.sent[-1]["components"], "Back to audit log")["payload"]["params"]["cursor"] == "page-two"
    assert all(frame["type"] != "chrome_render" for frame in orch.sent)


async def test_actual_native_dispatch_repository_failure_is_visible_and_not_success(recorder):
    class UnavailableRepo(FakeRepo):
        def list_for_user(self, user_id, **kwargs):
            raise ValueError("private internal storage detail")

    orch = FakeOrch(device="android")
    orch.audit_repo = UnavailableRepo()
    orch.attachment_repository = make_orch(orch.audit_repo).attachment_repository
    await chrome_events.handle_chrome_event(orch, orch.ws, "chrome_open", {"surface": "audit"}, "owner")
    frame = orch.sent[-1]
    assert frame["type"] == "chrome_surface"
    assert frame["components"] == [{"type": "alert", "message": "This surface failed to load. Please retry.", "variant": "error"}]
    assert recorder.events == []
    assert "private internal storage detail" not in json.dumps(frame)
