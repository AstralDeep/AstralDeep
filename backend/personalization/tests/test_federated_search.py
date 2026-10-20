"""Focused tests for personalization/federated_search.py: query/source/type normalization,
owner- and filter-bound cursors, deterministic ranking, deduplication, partial-source
reports, delivery rechecks, and cancellation over deterministic source stubs.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from personalization import federated_search as module
from personalization.federated_search import SearchHit, SourceReport


def _hit(source="memory", ref_id="m1", *, updated=10, phrase=True, score=100,
         rank_key=None):
    if source == "notes":
        ref = {"kind": "note", "note_id": ref_id, "expected_revision": 1,
               "type": "preference"}
    elif source == "conversations":
        ref = {"kind": "conversation", "conversation_id": ref_id,
               "message_id": 1, "type": "conversation"}
    elif source == "atlas":
        ref = {"kind": "atlas_page", "page_id": ref_id, "project_id": None,
               "revision": 1, "type": "atlas_page"}
    else:
        ref = {"kind": "memory", "memory_id": ref_id, "type": "preference"}
    if rank_key is None:
        rank_key = (-int(phrase), 0, 0, 1, -updated,
                    module.SOURCE_ORDER[source], hashlib.sha256(module._stable_ref(
                        {key: value for key, value in ref.items() if key != "type"}
                    ).encode("utf-8")).hexdigest())
    return SearchHit(source=source, type="preference", ref=ref, title="preference",
                     snippet="concise result", updated_at=updated, score=score,
                     rank_key=rank_key)


class StubSearch(module.FederatedSearchService):
    def __init__(self, scans, checks=None):
        self.scans = scans
        self.checks = checks or {}
        self.scan_calls = []
        self.check_calls = []

    async def _scan_source(self, source, owner_id, query, types, project_id):
        self.scan_calls.append((source, owner_id, query, types, project_id))
        value = self.scans[source]
        if isinstance(value, Exception):
            raise value
        return value

    async def _recheck(self, hit, owner_id):
        self.check_calls.append((hit, owner_id))
        value = self.checks.get(hit.ref.get("memory_id"), True)
        if callable(value):
            return await value(hit, owner_id)
        if isinstance(value, Exception):
            raise value
        return value


def test_normalize_query_sources_types_and_project():
    assert module.normalize_query("  find   it  ") == "find it"
    for bad in (None, "", "   ", "a\x00b", "x" * 201, "é" * 129):
        with pytest.raises(module.SearchQueryInvalid):
            module.normalize_query(bad)
    assert module.normalize_sources("memory,notes") == ("notes", "memory")
    assert module.normalize_sources(["atlas", "memory", "memory"]) == ("memory", "atlas")
    assert module.normalize_sources(None) == module.SOURCES
    for bad in ("bogus", 3, ["notes", 3], ",,"):
        with pytest.raises(module.SearchQueryInvalid):
            module.normalize_sources(bad)
    assert module.normalize_types("preference,atlas_page") == ("atlas_page", "preference")
    assert module.normalize_types("") == ()
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_types("unknown")
    assert module.normalize_project_filter(None) is None
    assert module.normalize_project_filter(" - ") == "-"
    assert module.normalize_project_filter(" project-a ") == "project-a"
    for bad in ("", "   ", 3, "x" * 129, "a\x00b"):
        with pytest.raises(module.SearchQueryInvalid):
            module.normalize_project_filter(bad)


def test_sanitize_and_snippet_bound_markup_and_control_characters():
    assert module.sanitize_text(' keep <script>alert("x")</script> & tidy ') == (
        'keep alert("x") & tidy')
    assert module.sanitize_text("line\nbreak\ttext\x01") == "line break text"
    assert module.sanitize_text(None) == ""
    long = "word " * 60 + "needle" + " word" * 60
    window = module.make_snippet(long, query="needle")
    assert "needle" in window and window.startswith("...") and window.endswith("...")
    assert module.make_snippet("tiny needle text", query="needle") == "tiny needle text"
    assert module.make_snippet("<b>hidden</b>", query="missing") == "hidden"


def test_cursors_bind_owner_query_filters_and_rank_key():
    args = {"owner_id": "owner-a", "query": "concise", "sources": ("notes", "memory"),
            "types": ("preference",), "project_id": "p1"}
    key = (-1, 0, 0, 1, -10, 0, "a" * 64)
    cursor = module.encode_cursor(**args, rank_key=key)
    assert module.decode_cursor(cursor, **args) == key
    assert module.decode_cursor(None, **args) is None
    assert module.decode_cursor("", **args) is None
    for changed in ({"owner_id": "owner-b"}, {"query": "other"},
                    {"sources": ("notes",)}, {"types": ()}, {"project_id": None}):
        altered = dict(args)
        altered.update(changed)
        with pytest.raises(module.SearchQueryInvalid) as refused:
            module.decode_cursor(cursor, **altered)
        assert refused.value.code == "search_cursor_scope_invalid"
    for bad in ("!!!!", "A" * 2049, "éé", base64.urlsafe_b64encode(b"[]").decode(),
                base64.urlsafe_b64encode(json.dumps({
                    "v": 2, "o": module._owner_digest("owner-a"),
                    "q": module._query_digest("concise"),
                    "f": module._filter_digest(*args["sources"][:1], (), None),
                    "k": [True, 0, 0, 1, -10, 0, "x"],
                }).encode()).decode().rstrip("=")):
        with pytest.raises(module.SearchQueryInvalid):
            module.decode_cursor(bad, **args)


def test_matched_parts_is_deterministic_and_phrase_first():
    ref = {"kind": "memory", "memory_id": "m1", "type": "preference"}
    phrase = module._matched_parts("concise answers", ["concise answers here"],
                                   "preference", 10, "memory", ref)
    overlap = module._matched_parts("concise answers", ["answers remain helpful"],
                                    "preference", 20, "memory", ref)
    assert phrase is not None and overlap is not None
    assert phrase.rank_key < overlap.rank_key
    assert phrase.rank_key[6] == hashlib.sha256(
        module._stable_ref(ref).encode("utf-8")).hexdigest()
    assert phrase.type == "preference" and "type" not in phrase.ref
    assert module._matched_parts("missing", ["unrelated"], "preference", 1,
                                 "memory", ref) is None
    empty = module._matched_parts("...", ["concise answers"], "preference", 1,
                                  "memory", ref)
    assert empty is None


@pytest.mark.asyncio
async def test_search_merges_filters_deduplicates_and_paginates():
    first = _hit(updated=10)
    duplicate = _hit(updated=11, score=50,
                     rank_key=(-1, 0, 0, 1, -11, 1, "a" * 64))
    note = _hit(source="notes", ref_id="n1", updated=9)
    service = StubSearch({
        "memory": ([duplicate, first], SourceReport("memory", True, True, 2, 2)),
        "notes": ([note], SourceReport("notes", True, True, 1, 1)),
        "conversations": ([], SourceReport("conversations", True, True, 0, 0)),
        "atlas": ([], SourceReport("atlas", True, True, 0, 0)),
    })
    outcome = await service.search(owner_id="owner-a", query="concise", limit=2)
    assert [hit.ref["memory_id"] if hit.source == "memory" else hit.ref["note_id"]
            for hit in outcome.hits] == ["m1", "n1"]
    assert outcome.total == 2
    assert outcome.next_cursor is None
    assert outcome.reports[0].returned == 1 and outcome.reports[1].returned == 1
    assert service.scan_calls[0][0] == "notes"
    filtered = await service.search(owner_id="owner-a", query="concise",
                                    sources="memory", types="preference")
    assert all(hit.source == "memory" for hit in filtered.hits)
    before = len(service.scan_calls)
    skipped = await service.search(owner_id="owner-a", query="concise", types="atlas_page")
    assert [report.source for report in skipped.reports] == list(module.SOURCES)
    assert service.scan_calls[before][0] == "atlas"
    assert all(report.available and report.matched == 0 for report in skipped.reports)


@pytest.mark.asyncio
async def test_mixed_source_cursor_keeps_source_priority_when_relevance_and_recency_tie():
    scans = {}
    for source in module.SOURCES:
        ref = {"kind": source, "id": source}
        hit = module._matched_parts("needle", ["needle"], "ordinary", 10, source, ref)
        scans[source] = ([hit], SourceReport(source, True, True, 1, 1))
    service = StubSearch(scans)
    collected = []
    cursor = None
    for _ in module.SOURCES:
        page = await service.search(owner_id="owner", query="needle", limit=1, cursor=cursor)
        assert len(page.hits) == 1
        collected.append(page.hits[0].source)
        cursor = page.next_cursor
    assert collected == list(module.SOURCES)
    assert cursor is None


@pytest.mark.asyncio
async def test_cursor_continues_after_deleted_ranked_result_without_duplicates():
    first = _hit(ref_id="m1", updated=10)
    second = _hit(ref_id="m2", updated=9)
    third = _hit(ref_id="m3", updated=8)
    scans = [
        ([first, second, third], SourceReport("memory", True, True, 3, 3)),
        ([second, third], SourceReport("memory", True, True, 2, 2)),
    ]
    service = StubSearch({"memory": scans[0], "notes": ([], SourceReport(
        "notes", True, True, 0, 0)), "conversations": ([], SourceReport(
        "conversations", True, True, 0, 0)), "atlas": ([], SourceReport(
        "atlas", True, True, 0, 0))})
    page = await service.search(owner_id="owner-a", query="concise", sources="memory",
                                limit=1)
    assert page.hits[0].ref["memory_id"] == "m1" and page.next_cursor
    service.scans["memory"] = scans[1]
    rest = await service.search(owner_id="owner-a", query="concise", sources="memory",
                                limit=1, cursor=page.next_cursor)
    assert rest.hits[0].ref["memory_id"] == "m2" and rest.next_cursor
    final = await service.search(owner_id="owner-a", query="concise", sources="memory",
                                 limit=1, cursor=rest.next_cursor)
    assert final.hits[0].ref["memory_id"] == "m3" and final.next_cursor is None


@pytest.mark.asyncio
async def test_partial_source_failure_and_stale_delivery_are_reported():
    good = _hit(updated=10)
    deleted = _hit(ref_id="m2", updated=9)
    service = StubSearch({
        "memory": ([deleted, good], SourceReport("memory", True, True, 2, 2)),
        "notes": ([], SourceReport("notes", False, False, 0, 0)),
        "conversations": ([], SourceReport("conversations", True, False, 1, 0)),
        "atlas": ([], SourceReport("atlas", True, True, 0, 0)),
    }, checks={"m1": True, "m2": False})
    outcome = await service.search(owner_id="owner-a", query="concise", limit=10)
    assert [hit.ref["memory_id"] for hit in outcome.hits] == ["m1"]
    reports = {report.source: report for report in outcome.reports}
    assert not reports["notes"].available and not reports["conversations"].complete
    assert reports["memory"].stale == 1 and reports["memory"].matched == 1
    assert outcome.total == 1 and outcome.partial


@pytest.mark.asyncio
async def test_recheck_failure_drops_the_whole_source_and_can_fill_from_others():
    memory_hit = _hit(updated=10)
    note_hit = _hit(source="notes", ref_id="n1", updated=9)
    service = StubSearch({
        "memory": ([memory_hit], SourceReport("memory", True, True, 1, 1)),
        "notes": ([note_hit], SourceReport("notes", True, True, 1, 1)),
        "conversations": ([], SourceReport("conversations", True, True, 0, 0)),
        "atlas": ([], SourceReport("atlas", True, True, 0, 0)),
    }, checks={"m1": RuntimeError("storage offline"), "n1": True})
    outcome = await service.search(owner_id="owner-a", query="concise", limit=2)
    reports = {report.source: report for report in outcome.reports}
    assert not reports["memory"].available and reports["memory"].matched == 0
    assert [hit.ref["note_id"] for hit in outcome.hits] == ["n1"]


@pytest.mark.asyncio
async def test_scan_exception_is_isolated_per_source():
    class BrokenScan(module.FederatedSearchService):
        def __init__(self):
            pass

        def _scan_memory(self, owner_id, query, types, project_id):
            raise RuntimeError("broken")

    outcome = await BrokenScan().search(owner_id="owner-a", query="concise",
                                        sources="memory")
    report = outcome.reports[0]
    assert not report.available and report.source == "memory"
    assert not outcome.any_available


@pytest.mark.asyncio
async def test_cancellation_propagates_without_partial_delivery():
    started = asyncio.Event()

    class Blocking(StubSearch):
        async def _scan_source(self, source, owner_id, query, types, project_id):
            started.set()
            await asyncio.Event().wait()

    service = Blocking({})
    task = asyncio.create_task(service.search(owner_id="owner-a", query="concise"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_message_text_and_payload_helpers():
    assert module._message_text("plain") == "plain"
    assert module._message_text(["a", {"text": "b"}, {"other": 1}, 5]) == "a b"
    assert module._message_text({"text": "dict"}) == "dict"
    assert module._message_text({"nope": 1}) == ""
    assert module._message_text(None) == ""
    hit = _hit()
    payload = module.hit_payload(hit)
    assert payload["source"] == "memory" and payload["type"] == "preference"
    assert payload["ref"]["memory_id"] == "m1" and payload["snippet"] == "concise result"
    report = module.report_payload(SourceReport("memory", True, False, 3, 2, 1, 1))
    assert report == {"source": "memory", "available": True, "complete": False,
                      "scanned": 3, "matched": 2, "returned": 1, "stale": 1}


def test_invalid_arguments_and_small_normalizers():
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_types(["preference", 3])
    assert module.normalize_types({"preference", "goal"}) == ("goal", "preference")
    assert module.normalize_sources({"memory"}) == ("memory",)
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_sources(4)
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_project_filter(4)
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_project_filter("x" * 129)
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_project_filter("a\x00b")
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_query("a\x01b")
    with pytest.raises(module.SearchQueryInvalid):
        module.normalize_query("x" * 201)
    assert module.SourceUnavailable("reason").reason == "reason"
    assert module.make_snippet("", query="x") == ""
    assert module._updated_at("bad") == 0 and module._updated_at(-4) == 0
    assert module._category_types("notes") == frozenset(
        {"profession", "goal", "preference", "workflow_tag", "context"})
    assert "context" in module._category_types("memory")
    assert module.FederatedSearchService._source_supports_types("notes", ("goal",))
    assert not module.FederatedSearchService._source_supports_types("notes", ("atlas_page",))
    assert module.FederatedSearchService._source_supports_types("conversations",
                                                                 ("conversation",))
    assert module.FederatedSearchService._source_supports_types("atlas", ("atlas_page",))


@pytest.mark.asyncio
async def test_service_rejects_invalid_owner_and_limit():
    service = StubSearch({})
    with pytest.raises(module.SearchQueryInvalid):
        await service.search(owner_id="", query="x")
    with pytest.raises(module.SearchQueryInvalid):
        await service.search(owner_id="owner", query="x", limit=0)
    with pytest.raises(module.SearchQueryInvalid):
        await service.search(owner_id="owner", query="x", limit=True)


def test_cursor_rejects_wrong_shape_and_keys():
    args = {"owner_id": "owner-a", "query": "q", "sources": ("memory",),
            "types": (), "project_id": None}
    def encoded(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
    good = {"v": 2, "o": module._owner_digest("owner-a"),
            "q": module._query_digest("q"),
            "f": module._filter_digest(("memory",), (), None),
            "k": [0, 0, 0, 0, 0, 1, "a" * 64]}
    assert module.decode_cursor(encoded(good), **args) == (0, 0, 0, 0, 0, 1, "a" * 64)
    for value in (
        {**good, "extra": 1},
        {**good, "v": 1, "k": [0, 0, 0, 0, 0, 1,
                                 '{"kind":"memory","memory_id":"m1"}']},
        *({**good, "k": [0, 0, 0, 0, 0, 1, digest]}
          for digest in ("a" * 63, "a" * 65, "z" * 64, "A" * 64)),
        {**good, "k": [0, 0, 0, 0, 0, 1]},
        {**good, "k": [0, 0, 0, 0, 0, 1, ""]},
        {**good, "k": [False, 0, 0, 0, 0, 1, "ref"]},
        {**good, "k": [0, 0, 0, 0, 0, 1, 2]},
    ):
        with pytest.raises(module.SearchQueryInvalid):
            module.decode_cursor(encoded(value), **args)


class _Transaction:
    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False


def _bare_service(**attributes):
    service = object.__new__(module.FederatedSearchService)
    service.runtime = SimpleNamespace(transaction=lambda: _Transaction())
    service.repositories = SimpleNamespace()
    service.repo = None
    service.credentials = None
    service.atlas = None
    service.orch = SimpleNamespace()
    for key, value in attributes.items():
        setattr(service, key, value)
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expiry", "revocation", "future", "supersession",
                                    "deletion", "tamper"])
async def test_memory_delivery_rejects_newly_ineligible_records(monkeypatch, change):
    from personalization import memory_guard

    monkeypatch.setenv("FF_MEMORY_TEMPORAL", "true")
    monkeypatch.setenv("FF_MEMORY_GUARD", "true")
    monkeypatch.setenv("MEMORY_HMAC_KEY", "federated-search-test-key")
    clock = [1.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0]))
    record = {"id": "m1", "user_id": "owner", "category": "preference",
              "value": "concise result", "source": "explicit", "updated_at": 1,
              "superseded_at": None, "valid_from": None, "valid_to": 2000}
    record["signature"] = memory_guard.sign_fields(
        "m1", "owner", "preference", "concise result", "explicit")
    current = [record]
    service = _bare_service(repo=SimpleNamespace(
        list_memory=lambda owner, **kwargs: [dict(record)],
        get_memory=lambda owner, memory_id: current[0],
    ))
    scan = service._scan_source

    async def scan_then_change(*args):
        result = await scan(*args)
        assert len(result[0]) == 1
        if change == "expiry":
            clock[0] = 2.0
        elif change == "revocation":
            record["valid_to"] = 1
        elif change == "future":
            record["valid_from"] = 2000
        elif change == "supersession":
            record["superseded_at"] = 1000
        elif change == "deletion":
            current[0] = None
        else:
            record["value"] = "mutated without resigning"
        return result

    monkeypatch.setattr(service, "_scan_source", scan_then_change)
    outcome = await service.search(owner_id="owner", query="concise", sources="memory")
    assert outcome.hits == ()
    assert outcome.total == 0 and outcome.next_cursor is None
    assert outcome.reports == (SourceReport("memory", True, True, 1, 0, 0, 1),)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["scan", "delivery"])
@pytest.mark.parametrize("policy", ["temporal", "integrity"])
async def test_memory_policy_failure_makes_source_unavailable(monkeypatch, phase, policy):
    from personalization import living_memory, memory_guard

    monkeypatch.setenv("FF_MEMORY_TEMPORAL", "true")
    monkeypatch.setenv("FF_MEMORY_GUARD", "true")
    record = {"id": "m1", "user_id": "owner", "category": "preference",
              "value": "concise result", "source": "explicit", "updated_at": 1}
    service = _bare_service(repo=SimpleNamespace(
        list_memory=lambda owner, **kwargs: [dict(record)],
        get_memory=lambda owner, memory_id: dict(record),
    ))

    def unavailable(*args):
        raise ValueError("policy unavailable")

    def fail_policy():
        if policy == "temporal":
            monkeypatch.setattr(living_memory, "is_valid_at", unavailable)
        else:
            monkeypatch.setattr(memory_guard, "trust_of", unavailable)

    if phase == "scan":
        fail_policy()
    else:
        scan = service._scan_source

        async def scan_then_fail(*args):
            result = await scan(*args)
            assert len(result[0]) == 1
            fail_policy()
            return result

        monkeypatch.setattr(service, "_scan_source", scan_then_fail)
    outcome = await service.search(owner_id="owner", query="concise", sources="memory")
    assert outcome.hits == () and outcome.total == 0
    assert not outcome.any_available and outcome.partial
    assert not outcome.reports[0].available


@pytest.mark.asyncio
async def test_memory_disabled_policies_preserve_legacy_delivery(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_TEMPORAL", "false")
    monkeypatch.setenv("FF_MEMORY_GUARD", "false")
    monkeypatch.setenv("MEMORY_HMAC_KEY", "federated-search-test-key")
    record = {"id": "m1", "user_id": "owner", "category": "preference",
              "value": "concise result", "source": "explicit", "updated_at": 1,
              "valid_to": 1, "signature": "invalid"}
    service = _bare_service(repo=SimpleNamespace(
        list_memory=lambda owner, **kwargs: [dict(record)],
        get_memory=lambda owner, memory_id: dict(record),
    ))
    outcome = await service.search(owner_id="owner", query="concise", sources="memory")
    assert [hit.snippet for hit in outcome.hits] == ["preference concise result"]
    assert outcome.reports == (SourceReport("memory", True, True, 1, 1, 1, 0),)


def test_note_scan_unavailable_and_note_recheck_branches():
    service = _bare_service()
    with pytest.raises(module.SourceUnavailable):
        service._scan_notes("owner", "q", ())
    with pytest.raises(module.SourceUnavailable):
        service._recheck_note(_hit(source="notes", ref_id="n1"), "owner")

    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Record:
        owner_id: str
        note_id: str
        revision: int
        format_version: int
        category: str
        enabled: bool
        created_at: int
        updated_at: int
        expires_at: int | None
        ciphertext: bytes

    import uuid
    note_id = str(uuid.uuid4())
    record = Record("owner", note_id, 1, 1, "context", True, 1, 1, None, b"x")
    service._note_cipher = lambda: SimpleNamespace(open=lambda *a, **k: None)
    service._note_repository = lambda: SimpleNamespace(
        get_explicit_note=lambda *a, **k: record)
    assert service._recheck_note(_hit(source="notes", ref_id=note_id), "owner") is True
    service._note_cipher = lambda: SimpleNamespace(
        open=lambda *a, **k: (_ for _ in ()).throw(
            module.ExplicitNoteUnavailable()))
    assert service._recheck_note(_hit(source="notes", ref_id=note_id), "owner") is False
    service._note_cipher = lambda: SimpleNamespace(open=lambda *a, **k: None)
    hit = _hit(source="notes", ref_id=note_id)
    hit = replace(hit, ref={**hit.ref, "expected_revision": 1})
    assert service._recheck_note(hit, "owner") is True
    service._note_repository = lambda: SimpleNamespace(
        get_explicit_note=lambda *a, **k: None)
    assert service._recheck_note(hit, "owner") is False


def test_memory_scan_skips_unusable_rows_and_rechecks(monkeypatch):
    service = _bare_service()
    with pytest.raises(module.SourceUnavailable):
        service._memory_items("owner", None)
    items = [
        "not-a-dict",
        {"id": "m1", "category": "preference", "value": '{"body":"x","kind":"page"}',
         "updated_at": 2},
        {"id": "m2", "category": "goal", "value": "unrelated", "updated_at": 2},
        {"category": "preference", "value": "missing id needle", "updated_at": 2},
        {"id": "m3", "category": "preference", "value": "needle memory", "updated_at": 2},
    ]
    monkeypatch.setattr(service, "_memory_items", lambda owner, project: (items, len(items)))
    hits, report = service._scan_memory("owner", "needle", ("preference",), None)
    assert [hit.ref["memory_id"] for hit in hits] == ["m3"] and report.available
    with pytest.raises(module.SourceUnavailable):
        service._recheck_memory(_hit(), "owner")
    service.repo = SimpleNamespace(get_memory=lambda owner, memory_id: None)
    assert service._recheck_memory(_hit(), "owner") is False
    service.repo = SimpleNamespace(get_memory=lambda owner, memory_id: {
        "id": memory_id, "superseded_at": 1, "value": "v"})
    assert service._recheck_memory(_hit(), "owner") is False
    envelope = json.dumps({
        "body": "x", "format": "astral.atlas-page/v1", "kind": "page",
        "page_id": "p1", "owner_id": "owner", "revision": 1,
    })
    service.repo = SimpleNamespace(get_memory=lambda owner, memory_id: {
        "id": memory_id, "superseded_at": None, "value": envelope})
    assert service._recheck_memory(_hit(), "owner") is False
    service.repo = SimpleNamespace(get_memory=lambda owner, memory_id: {
        "id": memory_id, "superseded_at": None, "value": "v"})
    assert service._recheck_memory(_hit(), "owner") is True
    monkeypatch.setattr(module, "MAX_HITS_PER_SOURCE", 1)
    monkeypatch.setattr(service, "_memory_items", lambda owner, project: ([
        {"id": "a", "category": "preference", "value": "needle", "updated_at": 1},
        {"id": "b", "category": "preference", "value": "needle", "updated_at": 1},
    ], 2))
    hits, report = service._scan_memory("owner", "needle", (), None)
    assert len(hits) == 1 and not report.complete


class _ConversationContext:
    def __init__(self, conversations, messages, *, fail_messages=False):
        self.repository = SimpleNamespace(
            conversations=SimpleNamespace(
                list_recent_nonempty=lambda *a, **k: tuple(conversations),
                get=lambda *a, **k: next(
                    (chat for chat in conversations
                     if chat.conversation_id == k.get("conversation_id")), None),
            ),
            messages=SimpleNamespace(
                list_visible=self._messages(messages, fail_messages),
                get=lambda *a, **k: next(
                    (message for message in messages
                     if getattr(message, "message_id", None) == k.get("message_id")), None),
            ),
        )

    @staticmethod
    def _messages(messages, fail):
        def load(*args, **kwargs):
            if fail:
                raise RuntimeError("messages failed")
            return tuple(messages)
        return load

    def call(self, operation, **kwargs):
        return operation(None, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation_id", [
    "c" * 36, "c" * 440, "c" * 512, "\\" * 256, "\\" * 512,
    '"' * 512, "😀" * 512, "\x01" * 512,
], ids=["ordinary", "ascii440", "ascii512", "slash256", "slash512",
        "quote512", "unicode512", "control512"])
async def test_conversation_boundary_ids_have_complete_bounded_cursor_traversal(
    conversation_id,
):
    service = _bare_service()
    chats = [SimpleNamespace(conversation_id=conversation_id, title="ordinary", updated_at=1)]
    messages = [SimpleNamespace(message_id=index, content="needle", timestamp=1)
                for index in (1, 2, 3)]
    service._history_context = lambda: _ConversationContext(chats, [*messages, messages[0]])
    arguments = {"owner_id": "owner", "query": "needle", "sources": "conversations",
                 "types": "conversation"}
    complete = await service.search(**arguments)
    assert len(complete.hits) == complete.total == 3
    assert {hit.ref["message_id"] for hit in complete.hits} == {1, 2, 3}
    assert all(hit.ref["conversation_id"] == conversation_id for hit in complete.hits)
    collected = []
    cursor = None
    for index in range(3):
        page = await service.search(**arguments, limit=1, cursor=cursor)
        assert len(page.hits) == 1 and page.total == 3
        assert page.reports[0].complete and page.reports[0].available
        collected.extend(hit.ref for hit in page.hits)
        cursor = page.next_cursor
        if index < 2:
            assert cursor and cursor.isascii() and len(cursor) <= 2048
        else:
            assert cursor is None
    assert collected == [hit.ref for hit in complete.hits]
    repeated = await service.search(**arguments)
    assert [hit.ref for hit in repeated.hits] == collected


def test_conversation_scan_fallbacks_limits_and_recheck(monkeypatch):
    service = _bare_service()
    hits, report = service._scan_conversations("owner", "needle", ("preference",))
    assert not hits and report.complete
    service._history_context = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    with pytest.raises(module.SourceUnavailable):
        service._scan_conversations("owner", "needle", ())
    chats = [
        SimpleNamespace(conversation_id="", title="empty", updated_at=1),
        SimpleNamespace(conversation_id="c1", title="needle title", updated_at=2),
        SimpleNamespace(conversation_id="c2", title="other", updated_at=3),
    ]
    messages = [SimpleNamespace(message_id=1, content="", timestamp=2),
                SimpleNamespace(message_id=2, content="a needle message", timestamp=2)]
    service._history_context = lambda: _ConversationContext(chats[:2], messages)
    hits, report = service._scan_conversations("owner", "needle", ())
    assert [hit.ref.get("message_id") for hit in hits] == [2]
    service._history_context = lambda: _ConversationContext(
        [chats[2]], messages, fail_messages=True)
    hits, report = service._scan_conversations("owner", "other", ())
    assert hits and not report.complete
    service._history_context = lambda: _ConversationContext([], [])
    assert service._recheck_conversation(
        _hit(source="conversations", ref_id="gone"), "owner") is False
    service._history_context = lambda: _ConversationContext(chats, messages)
    assert service._recheck_conversation(
        _hit(source="conversations", ref_id="c1"), "owner") is True
    bad = _hit(source="conversations", ref_id="c1")
    bad = replace(bad, ref={**bad.ref, "message_id": "1"})
    assert service._recheck_conversation(bad, "owner") is False
    assert service._recheck_conversation(
        replace(bad, ref={**bad.ref, "message_id": 2}), "owner") is True


def test_atlas_scan_and_recheck_branches(monkeypatch):
    from personalization.atlas_service import AtlasNotFound

    service = _bare_service()
    hits, report = service._scan_atlas("owner", "needle", ("preference",), None)
    assert not hits and report.complete
    broken = _bare_service()
    broken.orch = SimpleNamespace(runtime_composition=None, credential_manager=None,
                                  audit_repo=None)
    with pytest.raises(Exception):
        broken._atlas_service()

    class Page:
        page_id = "p1"
        project_id = None
        revision = 1
        title = "needle page"
        slug = "needle-page"
        body = "needle body"
        sources = ("https://example.invalid",)
        updated_at = 3

    fake = SimpleNamespace(
        scan_pages=lambda **kwargs: SimpleNamespace(
            pages=(Page(), Page()), scanned=2, complete=True),
        get_page=lambda **kwargs: Page(),
    )
    service.atlas = fake
    monkeypatch.setattr(module, "MAX_HITS_PER_SOURCE", 1)
    hits, report = service._scan_atlas("owner", "needle", (), None)
    assert len(hits) == 1 and not report.complete
    hit = _hit(source="atlas", ref_id="p1")
    hit = replace(hit, ref={**hit.ref, "page_id": "p1", "revision": 1})
    assert service._recheck_atlas(hit, "owner") is True
    fake.get_page = lambda **kwargs: (_ for _ in ()).throw(AtlasNotFound())
    assert service._recheck_atlas(hit, "owner") is False


@pytest.mark.asyncio
async def test_failed_source_recovers_on_the_next_search_without_stale_cache():
    service = StubSearch({"memory": ([], SourceReport("memory", False, False, 0, 0))})
    failed = await service.search(owner_id="owner-a", query="concise", sources="memory")
    assert not failed.any_available and failed.hits == ()
    service.scans["memory"] = ([_hit()], SourceReport("memory", True, True, 1, 1))
    recovered = await service.search(owner_id="owner-a", query="concise", sources="memory")
    assert recovered.any_available
    assert [hit.ref["memory_id"] for hit in recovered.hits] == ["m1"]
