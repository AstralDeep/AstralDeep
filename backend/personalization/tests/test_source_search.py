"""Tests for personalization/source_search.py: bounded owner-scoped federation over
memory, notes, and conversations with honest per-source availability, cursor
pagination, and source resolution.
"""

import asyncio
import base64
from types import SimpleNamespace

import pytest

from personalization import source_search as module


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeNotePage:
    def __init__(self, notes, cursor):
        self.notes = notes
        self.next_cursor = cursor


def note(value, note_id="n1", revision=1, updated_at=1000):
    return SimpleNamespace(value=value, metadata=SimpleNamespace(
        note_id=note_id, revision=revision, updated_at=updated_at, category="preference"))


class FakeNoteService:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def list(self, *, caller, after_id=None, limit=50, search=""):
        self.calls.append({"after_id": after_id, "limit": limit, "search": search})
        return self.pages.pop(0)


class FakeConversationRow:
    def __init__(self, conversation_id, title, updated_at):
        self.conversation_id = conversation_id
        self.title = title
        self.updated_at = updated_at


class FakeMessageRow:
    def __init__(self, content, timestamp):
        self.content = content
        self.timestamp = timestamp


class FakeHistoryRepository:
    def __init__(self, conversations=(), messages=None):
        self.conversations = SimpleNamespace(
            list_recent_nonempty=lambda tx, *, owner_id, limit: tuple(conversations)[:limit],
            get=lambda tx, *, owner_id, conversation_id: next(
                (chat for chat in conversations if chat.conversation_id == conversation_id), None))
        self.messages = SimpleNamespace(
            list_visible=lambda tx, *, owner_id, conversation_id, limit=None,
            through_render_revision=None: tuple((messages or {}).get(conversation_id, ()))[:limit or 10])


class FakeHistoryContext:
    def __init__(self, repository):
        self.repository = repository

    def call(self, operation, **kwargs):
        return operation(None, **kwargs)


def conversation_orch(conversations=(), messages=None):
    orch = SimpleNamespace(personalization_service=None, explicit_notes=None,
                           runtime_composition=None)
    fake = FakeHistoryContext(FakeHistoryRepository(conversations, messages))
    module_patch = fake
    orch._fake_history = module_patch
    return orch, fake


def test_normalize_query_trims_and_bounds():
    assert module.normalize_query("  find   it  ") == "find it"
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_query("   ")
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_query(None)
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_query("a\x00b")
    assert module.normalize_query("x" * 200) == "x" * 200
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_query("x" * 201)
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_query("é" * 200)


def test_cursor_roundtrip_and_refusals():
    assert module.decode_cursor(None) == 0
    assert module.decode_cursor("") == 0
    cursor = module.encode_cursor(40)
    assert module.decode_cursor(cursor) == 40
    for bad in ("!!!!", base64.urlsafe_b64encode(b'{"o": -1}').decode().rstrip("="),
                base64.urlsafe_b64encode(b'{"o": true}').decode().rstrip("="),
                base64.urlsafe_b64encode(b'{"o": 10001}').decode().rstrip("="),
                "A" * 80, "éé"):
        with pytest.raises(module.SearchQueryInvalid):
            module.decode_cursor(bad)


def test_form_params_merges_fields_and_preserves_errors():
    merged = module.form_params({"q": "old"}, {"q": "  new query ", "source": "notes"})
    assert merged == {"q": "new query", "source": "notes"}
    assert "error" not in merged
    paged = module.form_params(merged, {"cursor": module.encode_cursor(20)})
    assert module.decode_cursor(paged["cursor"]) == 20
    broken = module.form_params({}, {"q": "x" * 500})
    assert broken["error"] and broken["q"] == "x" * 500 and "cursor" not in broken
    bad_source = module.form_params({}, {"q": "ok", "source": "elsewhere"})
    assert "source" not in bad_source and bad_source["q"] == "ok"
    bad_cursor = module.form_params({"q": "ok"}, {"cursor": "!!!!"})
    assert bad_cursor["error"] and "cursor" not in bad_cursor
    keeps = module.form_params({"q": "kept", "source": "memory", "cursor": module.encode_cursor(4)}, {})
    assert keeps == {"q": "kept", "source": "memory", "cursor": module.encode_cursor(4)}


def test_score_ranks_phrase_over_overlap_and_recency():
    assert module._score("prefers concise answers", "concise", 0, 0) > 0
    assert module._score("concise stuff", "concise", 500, 500) > module._score("other stuff", "concise", 500, 500)
    assert module._score("concise", "concise", 100, 500) < module._score("concise", "concise", 500, 500)
    assert module._score("nothing", "missing", 0, 0) == 0.0


def test_snippet_windows_and_fallback():
    long = "word " * 60 + "needle" + " word" * 60
    window = module._snippet(long, "needle")
    assert "needle" in window and window.startswith("...") and window.endswith("...")
    assert module._snippet("no match here", "needle") == "no match here"
    short = "tiny needle text"
    assert module._snippet(short, "needle") == short


def test_message_text_extracts_strings_parts_and_dicts():
    assert module._message_text("plain") == "plain"
    assert module._message_text(["a", {"text": "b"}, {"other": 1}, 5]) == "a b"
    assert module._message_text({"text": "dict"}) == "dict"
    assert module._message_text({"nope": 1}) == ""
    assert module._message_text(None) == ""


def test_memory_hits_report_match_and_availability():
    repo = SimpleNamespace(list_memory=lambda user_id: [
        {"id": "m1", "value": "prefers concise answers", "category": "preference",
         "keywords": "concise", "updated_at": 1700000000000},
        {"id": "m2", "value": "unrelated", "category": "goal", "keywords": "", "updated_at": 1},
    ])
    orch = SimpleNamespace(personalization_service=SimpleNamespace(repo=repo))
    hits, report = module._memory_hits(orch, "u1", "concise", bound=10)
    assert [hit.ref["memory_id"] for hit in hits] == ["m1"]
    assert report.available and report.complete and report.matched == 1 and report.scanned == 2
    missing = SimpleNamespace(personalization_service=None)
    hits, report = module._memory_hits(missing, "u1", "concise", bound=10)
    assert not hits and not report.available and not report.complete
    broken = SimpleNamespace(personalization_service=SimpleNamespace(
        repo=SimpleNamespace(list_memory=lambda user_id: (_ for _ in ()).throw(RuntimeError))))
    hits, report = module._memory_hits(broken, "u1", "concise", bound=10)
    assert not hits and not report.available


def test_conversation_hits_search_titles_and_messages(monkeypatch):
    chats = [FakeConversationRow("c1", "Grant planning", 1700000000000),
             FakeConversationRow("c2", "Other", 1690000000000)]
    messages = {"c1": [FakeMessageRow("we discussed the NSF budget", 1699999999000),
                       FakeMessageRow([{"text": "no match"}, "parts"], 1)]}
    orch, fake = conversation_orch(chats, messages)
    monkeypatch.setattr(module, "_history_context", lambda _orch: fake)
    hits, report = module._conversation_hits(orch, "u1", "grant", bound=10)
    assert [hit.ref["chat_id"] for hit in hits] == ["c1"]
    assert report.available and report.complete
    hits, report = module._conversation_hits(orch, "u1", "budget", bound=10)
    assert len(hits) == 1 and hits[0].snippet == "we discussed the NSF budget"
    hits, report = module._conversation_hits(orch, "u1", "zzz-nothing", bound=10)
    assert not hits and report.available and report.matched == 0
    broken_orch, _ = conversation_orch()
    monkeypatch.setattr(module, "_history_context", lambda _orch: (_ for _ in ()).throw(RuntimeError()))
    hits, report = module._conversation_hits(broken_orch, "u1", "grant", bound=10)
    assert not hits and not report.available


def test_note_hits_traverses_pages_until_cursor_ends(monkeypatch):
    service = FakeNoteService([
        FakeNotePage([note("first mention", "n1")], "cursor-1"),
        FakeNotePage([note("second mention", "n2", revision=2, updated_at=2000)], None),
    ])
    orch = SimpleNamespace(explicit_notes=service)
    monkeypatch.setattr("personalization.explicit_note_service.ExplicitNoteService", FakeNoteService)
    hits, report = run(module._note_hits(orch, object(), "mention", bound=10))
    assert [hit.ref["note_id"] for hit in hits] == ["n1", "n2"]
    assert service.calls[0]["after_id"] is None and service.calls[1]["after_id"] == "cursor-1"
    assert report.available and report.complete and report.matched == 2


def test_note_hits_unavailable_without_service_or_caller_or_on_failure(monkeypatch):
    monkeypatch.setattr("personalization.explicit_note_service.ExplicitNoteService", FakeNoteService)
    hits, report = run(module._note_hits(SimpleNamespace(explicit_notes=None), object(), "x", bound=5))
    assert not hits and not report.available
    hits, report = run(module._note_hits(SimpleNamespace(explicit_notes=FakeNoteService([])), None, "x", bound=5))
    assert not hits and not report.available
    failing = FakeNoteService([])
    async def boom(**kwargs):
        raise RuntimeError("scan failed")
    failing.list = boom
    hits, report = run(module._note_hits(SimpleNamespace(explicit_notes=failing), object(), "x", bound=5))
    assert not hits and not report.available


def test_federated_search_merges_filters_paginates_and_validates(monkeypatch):
    chats = [FakeConversationRow("c1", "concise plans", 1700000000000)]
    messages = {"c1": [FakeMessageRow("keep it concise please", 1699999999000)]}
    orch, fake = conversation_orch(chats, messages)
    monkeypatch.setattr(module, "_history_context", lambda _orch: fake)
    repo = SimpleNamespace(list_memory=lambda user_id: [
        {"id": "m1", "value": "concise preference", "category": "preference",
         "keywords": "", "updated_at": 1700000000001}])
    orch.personalization_service = SimpleNamespace(repo=repo)
    outcome = run(module.federated_search(orch, user_id="u1", query="concise"))
    assert outcome.any_available and not outcome.partial
    assert [hit.source for hit in outcome.hits] == ["memory", "conversations"]
    assert outcome.hits[1].snippet == "keep it concise please"
    assert outcome.next_offset is None
    page_one = run(module.federated_search(orch, user_id="u1", query="concise", limit=1))
    assert len(page_one.hits) == 1 and page_one.next_offset == 1
    page_two = run(module.federated_search(orch, user_id="u1", query="concise", limit=1,
                                           cursor=module.encode_cursor(1)))
    assert len(page_two.hits) == 1 and page_two.next_offset is None
    filtered = run(module.federated_search(orch, user_id="u1", query="concise", source="memory"))
    assert [hit.source for hit in filtered.hits] == ["memory"]
    assert len(filtered.reports) == 1 and filtered.reports[0].source == "memory"
    for changes in ({"query": ""}, {"query": None}, {"query": "x", "source": "bogus"},
                    {"query": "x", "limit": 0}, {"query": "x", "limit": 99},
                    {"query": "x", "cursor": "!!!!"}):
        with pytest.raises(module.SearchQueryInvalid):
            run(module.federated_search(orch, user_id="u1", **changes))


def test_federated_search_reports_unavailable_sources_without_dropping_others(monkeypatch):
    orch, fake = conversation_orch()
    monkeypatch.setattr(module, "_history_context", lambda _orch: (_ for _ in ()).throw(RuntimeError()))
    outcome = run(module.federated_search(orch, user_id="u1", query="anything"))
    assert not outcome.any_available
    assert all(not report.available for report in outcome.reports)
    assert outcome.hits == ()


def test_resolve_source_answers_for_live_and_gone_sources(monkeypatch):
    chats = [FakeConversationRow("c1", "Grant planning", 1700000000000)]
    orch, fake = conversation_orch(chats)
    monkeypatch.setattr(module, "_history_context", lambda _orch: fake)
    repo = SimpleNamespace(get_memory=lambda user_id, memory_id: {"id": memory_id}
                           if memory_id == "m1" else None)
    orch.personalization_service = SimpleNamespace(repo=repo)
    assert run(module.resolve_source(orch, user_id="u1",
                                     ref={"kind": "conversation", "chat_id": "c1"})) is None
    reason = run(module.resolve_source(orch, user_id="u1",
                                       ref={"kind": "conversation", "chat_id": "gone"}))
    assert reason == "That conversation is no longer available."
    assert run(module.resolve_source(orch, user_id="u1",
                                     ref={"kind": "memory", "memory_id": "m1"})) is None
    reason = run(module.resolve_source(orch, user_id="u1",
                                       ref={"kind": "memory", "memory_id": "gone"}))
    assert reason == "That memory is no longer available."
    assert run(module.resolve_source(orch, user_id="u1",
                                     ref={"kind": "note", "note_id": "n1",
                                          "expected_revision": 3})) is None
    assert run(module.resolve_source(orch, user_id="u1", ref={"kind": "weird"}))
    assert run(module.resolve_source(orch, user_id="u1", ref="not-a-dict"))
    broken, _ = conversation_orch()
    monkeypatch.setattr(module, "_history_context", lambda _orch: (_ for _ in ()).throw(RuntimeError()))
    assert run(module.resolve_source(broken, user_id="u1",
                                     ref={"kind": "conversation", "chat_id": "c1"}))


def test_memory_hits_skip_unusable_rows():
    repo = SimpleNamespace(list_memory=lambda user_id: [
        "not-a-dict", {"value": "orphan needle", "category": "preference"},
        {"id": "m1", "value": "real needle", "category": "preference", "updated_at": 5}])
    orch = SimpleNamespace(personalization_service=SimpleNamespace(repo=repo))
    hits, report = module._memory_hits(orch, "u1", "needle", bound=10)
    assert [hit.ref["memory_id"] for hit in hits] == ["m1"]
    assert report.available


def test_note_hits_bound_stops_mid_page(monkeypatch):
    service = FakeNoteService([FakeNotePage(
        [note("needle one", "n1"), note("needle two", "n2")], "cursor")])
    monkeypatch.setattr("personalization.explicit_note_service.ExplicitNoteService", FakeNoteService)
    hits, report = run(module._note_hits(SimpleNamespace(explicit_notes=service),
                                         object(), "needle", bound=1))
    assert len(hits) == 1 and not report.complete


def test_conversation_hits_bound_rows_and_message_failures(monkeypatch):
    chats = [FakeConversationRow("", "no id", 1),
             FakeConversationRow("c1", "needle title", 1700000000000),
             FakeConversationRow("c2", "needle too", 1600000000000)]
    orch, fake = conversation_orch(chats)
    monkeypatch.setattr(module, "_history_context", lambda _orch: fake)
    hits, report = module._conversation_hits(orch, "u1", "needle", bound=1)
    assert len(hits) == 1 and not report.complete and report.scanned == 0
    whole, report = module._conversation_hits(orch, "u1", "needle", bound=10)
    assert len(whole) == 2 and report.complete
    failing = FakeHistoryRepository(chats)
    def broken_visible(*args, **kwargs):
        raise RuntimeError("messages unavailable")
    failing.messages = SimpleNamespace(list_visible=broken_visible)
    monkeypatch.setattr(module, "_history_context", lambda _orch: FakeHistoryContext(failing))
    hits, report = module._conversation_hits(orch, "u1", "needle", bound=10)
    assert [hit.snippet for hit in hits] == ["needle title", "needle too"]
    assert report.available


def test_federated_search_falls_back_when_a_scanner_raises(monkeypatch):
    orch, fake = conversation_orch()
    monkeypatch.setattr(module, "_history_context", lambda _orch: fake)
    def boom(*args, **kwargs):
        raise RuntimeError("scanner exploded")
    monkeypatch.setattr(module, "_memory_hits", boom)
    outcome = run(module.federated_search(orch, user_id="u1", query="x"))
    reports = {report.source: report for report in outcome.reports}
    assert not reports["memory"].available
    assert reports["conversations"].available


def test_form_params_refuses_non_string_query():
    merged = module.form_params({}, {"q": 123})
    assert merged["error"] == "Enter a search term." and merged["q"] == ""


def test_resolve_source_memory_failure_is_honest(monkeypatch):
    repo = SimpleNamespace(get_memory=lambda user_id, memory_id: (_ for _ in ()).throw(RuntimeError()))
    orch = SimpleNamespace(personalization_service=SimpleNamespace(repo=repo))
    reason = run(module.resolve_source(orch, user_id="u1",
                                       ref={"kind": "memory", "memory_id": "m1"}))
    assert reason == "That memory is no longer available."


def test_source_unavailable_reason_is_carryable():
    raiseable = module.SourceUnavailable("gone")
    assert raiseable.reason == "gone"


def test_form_params_drops_non_string_query_without_explicit_submit():
    merged = module.form_params({"source": "memory"}, {})
    assert merged == {"source": "memory"}
    dropped = module.form_params({"q": 123, "source": "memory"}, {})
    assert dropped == {"source": "memory"}


def test_conversation_message_caps_and_bounds_mark_incomplete(monkeypatch):
    chats = [FakeConversationRow("c1", "first", 1700000000000),
             FakeConversationRow("c2", "second", 1600000000000)]
    orch, fake = conversation_orch(chats)
    monkeypatch.setattr(module, "_history_context", lambda _orch: fake)
    hits, report = module._conversation_hits(orch, "u1", "first", bound=1)
    assert len(hits) == 1 and not report.complete
    capped = FakeHistoryRepository(chats)
    capped.messages = SimpleNamespace(list_visible=lambda tx, *, owner_id, conversation_id,
                                      limit=None, through_render_revision=None:
                                      tuple(FakeMessageRow("first filler text", 1)
                                            for _ in range(limit or 10)))
    monkeypatch.setattr(module, "_history_context", lambda _orch: FakeHistoryContext(capped))
    hits, report = module._conversation_hits(orch, "u1", "first", bound=1)
    assert hits[0].snippet == "first filler text" and not report.complete
    hits, report = module._conversation_hits(orch, "u1", "first", bound=10)
    assert hits[0].source == "conversations" and not report.complete


def test_federated_search_deduplicates_identical_refs(monkeypatch):
    orch, fake = conversation_orch()
    monkeypatch.setattr(module, "_history_context", lambda _orch: fake)
    duplicate = module.SearchHit(source="memory", ref={"kind": "memory", "memory_id": "m1"},
                                 title="t", snippet="s", updated_at=5, rank=1.0)
    def twin(*args, **kwargs):
        return [duplicate, module.SearchHit(source="memory", ref={"kind": "memory", "memory_id": "m1"},
                                            title="t2", snippet="s2", updated_at=4, rank=0.5)], \
            module.SourceReport("memory", True, True, 2, 2)
    monkeypatch.setattr(module, "_memory_hits", twin)
    outcome = run(module.federated_search(orch, user_id="u1", query="x"))
    assert len(outcome.hits) == 1 and outcome.hits[0].snippet == "s"
