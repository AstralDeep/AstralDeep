"""Verifies bounded exact observation capture, strict host grants, and fail-closed recall.
Synthetic clocks and coordinated threads cover expiry, isolation, paging, and capacity.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, tzinfo
import json
import math
import os
from threading import Barrier

import pytest

from orchestrator.evidence_archive import (
    EvidenceArchive,
    EvidenceCaptureError,
    EvidenceDenied,
    EvidenceLimit,
    EvidencePageError,
    EvidencePolicyError,
    EvidenceUnavailable,
    MAX_CONVERSATION_BYTES,
    MAX_OBSERVATION_BYTES,
    MAX_PAGE_BYTES,
    RetentionGrant,
    load_grants,
    match_grant,
)


NOW = datetime(2026, 10, 8, 16, tzinfo=UTC)
BINDINGS = {"owner_id": "owner-a", "conversation_id": "chat-a", "audience_id": "audience-a"}


class Clock:
    def __init__(self):
        self.now = NOW
        self.elapsed = 0.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.elapsed


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def grant():
    return RetentionGrant(
        **BINDINGS, source_agent="reader-1", source_tool="read_source",
        expires_at=NOW + timedelta(hours=48),
    )


@pytest.fixture
def archive(clock):
    return EvidenceArchive(clock=clock, monotonic_clock=clock.monotonic)


def capture(archive, grant, text="beginning middle ending", **overrides):
    args = {
        "grant": grant, "operation_id": "operation-a",
        "source_args": {"query": "synthetic"}, "outcome": "success",
    }
    args.update(overrides)
    return archive.capture(text, **args)


def serialized_grant(grant):
    return {
        **BINDINGS, "source_agent": grant.source_agent, "source_tool": grant.source_tool,
        "expires_at": grant.expires_at.isoformat(),
    }


def test_exact_reconstruction_and_unicode_byte_pages(clock, grant):
    archive = EvidenceArchive(clock=clock, monotonic_clock=clock.monotonic, page_limit_bytes=17)
    text = "beginning 🙂€中 e\u0301\r\n\x00middle\u2028ending " * 3
    observation = capture(archive, grant, text)
    pieces, offset = [], 0
    while True:
        page = archive.read(observation.reference, offset, grant=grant, **BINDINGS)
        assert page.start == offset
        assert page.end - page.start == len(page.text.encode("utf-8")) <= 17
        assert page.total == len(text.encode("utf-8"))
        assert page.digest == observation.digest
        assert page.reference == observation.reference
        assert page.partial
        assert archive.read(observation.reference, offset, grant=grant, **BINDINGS) == page
        pieces.append(page.text)
        if page.at_end:
            assert page.next_offset is None
            break
        assert page.next_offset == page.end > offset
        offset = page.next_offset
    assert "".join(pieces) == text
    terminal = archive.read(observation.reference, page.total, grant=grant, **BINDINGS)
    assert terminal.text == "" and terminal.at_end and terminal.next_offset is None


@pytest.mark.parametrize("text", ["", "ordinary", "🙂", "e\u0301", "\r\n", "中€🙂"])
def test_full_and_empty_sources_are_honest(archive, grant, text):
    observation = capture(archive, grant, text)
    page = archive.read(observation.reference, grant=grant, **BINDINGS)
    assert page.text == text
    assert page.start == 0 and page.end == page.total == len(text.encode("utf-8"))
    assert page.at_end and not page.partial
    payload = page.to_dict()
    assert payload["text"] == text and payload["at_end"] is True
    assert not {"owner_id", "source_args", "grant_fingerprint"} & payload.keys()


@pytest.mark.parametrize("offset", [-1, True, 1.0, "0", None, 999])
def test_invalid_offsets_are_bounded(archive, grant, offset):
    observation = capture(archive, grant, "🙂abc")
    with pytest.raises(EvidencePageError):
        archive.read(observation.reference, offset, grant=grant, **BINDINGS)


@pytest.mark.parametrize("offset", [1, 2, 3])
def test_unicode_interior_offset_is_refused(archive, grant, offset):
    observation = capture(archive, grant, "🙂abc")
    with pytest.raises(EvidencePageError):
        archive.read(observation.reference, offset, grant=grant, **BINDINGS)


@pytest.mark.parametrize("binding", ["owner_id", "conversation_id", "audience_id"])
def test_foreign_bindings_and_unknown_reference_are_uniform(archive, grant, binding):
    observation = capture(archive, grant)
    changed = {**BINDINGS, binding: "foreign"}
    errors = []
    for reference, bindings in [(observation.reference, changed), ("unknown", BINDINGS)]:
        with pytest.raises(EvidenceDenied) as error:
            archive.inspect(reference, **bindings)
        errors.append(str(error.value))
        with pytest.raises(EvidenceDenied):
            archive.read(reference, grant=grant, **bindings)
        with pytest.raises(EvidenceDenied):
            archive.delete(reference, **bindings)
    assert errors[0] == errors[1]
    assert not any(value in errors[0] for value in BINDINGS.values())


@pytest.mark.parametrize("reference", [None, 0, [], {}, "x" * 513])
def test_invalid_reference_is_nondisclosing(archive, reference):
    with pytest.raises(EvidenceDenied):
        archive.inspect(reference, **BINDINGS)


def test_distinct_operations_and_arguments_are_immutable(archive, grant):
    arguments = {"filters": ["synthetic"]}
    first = capture(archive, grant, source_args=arguments)
    second = capture(archive, grant, operation_id="operation-b")
    arguments["filters"].append("changed")
    exposed = first.source_args
    exposed["filters"].append("also changed")
    assert first.reference != second.reference and second.order == first.order + 1
    assert first.digest == second.digest
    assert first.source_args == {"filters": ["synthetic"]}
    assert first.operation_id == "operation-a" and second.operation_id == "operation-b"
    assert not hasattr(first, "text") and not hasattr(first, "content")
    assert "filters" not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.outcome = "failure"


@pytest.mark.parametrize("outcome", ["success", "error", "denial", "denied", "failure", "incomplete", "cancelled", "interrupted"])
def test_actual_outcome_is_preserved(archive, grant, outcome):
    observation = capture(archive, grant, outcome=outcome)
    assert archive.inspect(observation.reference, **BINDINGS).outcome == outcome


def test_absolute_expiry_does_not_renew(archive, grant, clock):
    observation = capture(archive, grant)
    assert observation.expires_at == NOW + timedelta(hours=24)
    clock.now += timedelta(hours=23)
    assert archive.read(observation.reference, grant=grant, **BINDINGS).text
    clock.now += timedelta(hours=1)
    for call in (
        lambda: archive.inspect(observation.reference, **BINDINGS),
        lambda: archive.read(observation.reference, grant=grant, **BINDINGS),
    ):
        with pytest.raises(EvidenceUnavailable) as error:
            call()
        assert error.value.reason == "expired"
    assert archive.retained_bytes == 0
    with pytest.raises(EvidenceDenied):
        archive.inspect(observation.reference, **{**BINDINGS, "owner_id": "foreign"})


@pytest.mark.parametrize("field", ["expires_at", "source_deadline", "conversation_deadline"])
def test_every_earlier_deadline_applies(archive, grant, clock, field):
    grant = replace(grant, **{field: NOW + timedelta(minutes=20)})
    observation = capture(archive, grant)
    assert observation.expires_at == NOW + timedelta(minutes=20)
    clock.now = observation.expires_at
    assert archive.cleanup() == (observation,)
    assert archive.cleanup() == ()
    with pytest.raises(EvidenceUnavailable, match="expired"):
        archive.read(observation.reference, grant=grant, **BINDINGS)


def test_monotonic_lifetime_prevents_clock_regression_extension(archive, grant, clock):
    observation = capture(archive, grant)
    clock.now -= timedelta(hours=1)
    clock.elapsed = 86400.0
    with pytest.raises(EvidenceUnavailable, match="expired"):
        archive.read(observation.reference, grant=grant, **BINDINGS)


@pytest.mark.parametrize("field,value", [
    ("expires_at", NOW + timedelta(hours=47)),
    ("source_deadline", NOW + timedelta(hours=23)),
    ("source_agent", "other-reader"), ("source_tool", "other-tool"),
])
def test_policy_change_invalidates_without_content(archive, grant, field, value):
    observation = capture(archive, grant)
    with pytest.raises(EvidenceDenied):
        archive.read(observation.reference, grant=replace(grant, **{field: value}), **BINDINGS)


def test_deletion_and_revocation_remove_content(archive, grant):
    first, second = capture(archive, grant), capture(archive, grant)
    assert archive.delete(first.reference, **BINDINGS) == first
    assert archive.revoke(owner_id="foreign") == ()
    assert archive.revoke(owner_id=grant.owner_id, conversation_id="foreign") == ()
    assert archive.revoke(owner_id=grant.owner_id, grant_fingerprint="0" * 64) == ()
    assert archive.revoke(owner_id=grant.owner_id, grant_fingerprint=grant.fingerprint) == (second,)
    assert archive.observation_count == 0 and archive.retained_bytes == 0
    for reference in (first.reference, second.reference):
        with pytest.raises(EvidenceUnavailable):
            archive.read(reference, grant=grant, **BINDINGS)


def test_revocation_selects_only_bound_conversation(archive, grant):
    first = capture(archive, grant)
    other_grant = replace(grant, conversation_id="chat-b")
    second = capture(archive, other_grant)
    assert archive.revoke(owner_id=grant.owner_id, conversation_id=grant.conversation_id) == (first,)
    assert archive.read(second.reference, grant=other_grant,
                        **{**BINDINGS, "conversation_id": "chat-b"}).text


def test_restart_does_not_reconstruct_or_renew(archive, grant, clock):
    observation = capture(archive, grant)
    restarted = EvidenceArchive(clock=clock, monotonic_clock=clock.monotonic)
    with pytest.raises(EvidenceDenied):
        restarted.read(observation.reference, grant=grant, **BINDINGS)


def test_integrity_failure_removes_source_before_disclosure(archive, grant):
    observation = capture(archive, grant)
    archive._records[observation.reference].content = b"forged content"
    with pytest.raises(EvidenceUnavailable) as error:
        archive.read(observation.reference, grant=grant, **BINDINGS)
    assert error.value.reason == "integrity_failed"
    assert archive.observation_count == 0 and archive.retained_bytes == 0


def test_metadata_binding_integrity_is_verified(archive, grant):
    observation = capture(archive, grant)
    object.__setattr__(observation, "operation_id", "forged-operation")
    with pytest.raises(EvidenceUnavailable, match="integrity_failed"):
        archive.read(observation.reference, grant=grant, **BINDINGS)


@pytest.mark.parametrize("field,value", [("size_bytes", 999), ("owner_id", "foreign"),
                                         ("_source_args_json", None)])
def test_corrupt_metadata_does_not_break_cleanup_or_disclose(archive, grant, field, value):
    observation = capture(archive, grant)
    object.__setattr__(observation, field, value)
    with pytest.raises(EvidenceUnavailable, match="integrity_failed"):
        archive.inspect(observation.reference, **BINDINGS)
    assert archive.retained_bytes == 0 and archive.observation_count == 0
    with pytest.raises(EvidenceDenied):
        archive.inspect(observation.reference, **{**BINDINGS, "owner_id": "foreign"})


def test_invalid_content_type_is_fail_closed(archive, grant):
    observation = capture(archive, grant)
    archive._records[observation.reference].content = None
    with pytest.raises(EvidenceUnavailable, match="integrity_failed"):
        archive.read(observation.reference, grant=grant, **BINDINGS)


def test_reference_collision_refuses_without_replacing_live_source(archive, grant, monkeypatch):
    monkeypatch.setattr("orchestrator.evidence_archive.secrets.token_urlsafe", lambda _: "synthetic-reference")
    first = capture(archive, grant)
    with pytest.raises(EvidenceCaptureError):
        capture(archive, grant, "other text")
    assert archive.read(first.reference, grant=grant, **BINDINGS).text == "beginning middle ending"


def test_maximum_observation_boundary_is_enforced(archive, grant):
    observation = capture(archive, grant, "x" * MAX_OBSERVATION_BYTES)
    assert archive.inspect(observation.reference, **BINDINGS).size_bytes == MAX_OBSERVATION_BYTES
    with pytest.raises(EvidenceLimit):
        capture(archive, grant, "x" * (MAX_OBSERVATION_BYTES + 1))
    assert archive.observation_count == 1


def test_observation_limit_measures_utf8_bytes(clock, grant):
    archive = EvidenceArchive(clock=clock, observation_limit_bytes=4)
    observation = capture(archive, grant, "🙂")
    with pytest.raises(EvidenceLimit):
        capture(archive, grant, "🙂x")
    assert archive.inspect(observation.reference, **BINDINGS).size_bytes == 4


def test_conversation_and_global_capacity_never_evict_live_sources(clock, grant):
    archive = EvidenceArchive(clock=clock, observation_limit_bytes=4,
                              conversation_limit_bytes=4, global_limit_bytes=8)
    first = capture(archive, grant, "1234")
    with pytest.raises(EvidenceLimit):
        capture(archive, grant, "1")
    other = replace(grant, conversation_id="chat-b")
    second = capture(archive, other, "1234")
    with pytest.raises(EvidenceLimit):
        capture(archive, replace(grant, conversation_id="chat-c"), "1")
    assert archive.read(first.reference, grant=grant, **BINDINGS).text == "1234"
    assert archive.read(second.reference, grant=other,
                        **{**BINDINGS, "conversation_id": "chat-b"}).text == "1234"
    assert archive.retained_bytes == 8


def test_empty_sources_have_record_and_tombstone_bounds(clock, grant):
    archive = EvidenceArchive(clock=clock, max_records=1)
    first = capture(archive, grant, "")
    with pytest.raises(EvidenceLimit):
        capture(archive, grant, "")
    archive.delete(first.reference, **BINDINGS)
    second = capture(archive, grant, "")
    archive.delete(second.reference, **BINDINGS)
    assert len(archive._tombstones) == 1
    with pytest.raises(EvidenceDenied):
        archive.inspect(first.reference, **BINDINGS)
    with pytest.raises(EvidenceUnavailable):
        archive.inspect(second.reference, **BINDINGS)


def test_expired_cleanup_frees_admission_capacity(clock, grant):
    archive = EvidenceArchive(clock=clock, max_records=1)
    expired_grant = replace(grant, expires_at=NOW + timedelta(seconds=1))
    first = capture(archive, expired_grant)
    clock.now += timedelta(seconds=1)
    assert archive.cleanup() == (first,)
    assert capture(archive, grant).reference != first.reference


def test_expiry_is_rechecked_at_capture_admission(archive, grant, clock, monkeypatch):
    from orchestrator import evidence_archive

    grant = replace(grant, expires_at=NOW + timedelta(seconds=1))
    original = evidence_archive._arguments

    def serialize(arguments):
        result = original(arguments)
        clock.now = grant.expires_at
        return result

    monkeypatch.setattr(evidence_archive, "_arguments", serialize)
    with pytest.raises(EvidencePolicyError):
        capture(archive, grant)
    assert archive.observation_count == 0


def test_multiple_empty_sources_can_be_cleaned_without_counter_drift(archive, grant, clock):
    observations = [capture(archive, grant, ""), capture(archive, grant, "")]
    clock.now += timedelta(hours=24)
    assert archive.cleanup() == tuple(observations)
    assert archive.retained_bytes == 0 and archive.observation_count == 0


def test_concurrent_admission_is_atomic(clock, grant):
    archive = EvidenceArchive(clock=clock, observation_limit_bytes=4,
                              conversation_limit_bytes=4, global_limit_bytes=4)
    barrier = Barrier(2)

    def admitted(operation):
        barrier.wait()
        try:
            return capture(archive, grant, "1234", operation_id=operation)
        except EvidenceLimit:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(admitted, ["operation-a", "operation-b"]))
    assert sum(value is not None for value in outcomes) == 1
    assert archive.retained_bytes == 4 and archive.observation_count == 1


@pytest.mark.parametrize("text", [None, {}, [], b"text", 1, "\ud800"])
def test_unsupported_text_never_creates_source(archive, grant, text):
    with pytest.raises(EvidenceCaptureError):
        capture(archive, grant, text)
    assert archive.observation_count == 0


@pytest.mark.parametrize("overrides", [
    {"operation_id": ""}, {"operation_id": "x" * 513}, {"outcome": "invented"},
    {"source_args": []}, {"source_args": {"value": math.nan}},
    {"source_args": {"value": object()}}, {"source_args": {"value": "x" * 65537}},
    {"source_args": {"_credentials": "secret"}},
    {"source_args": {"nested": {"api_key": "secret"}}},
])
def test_invalid_capture_metadata_is_refused_safely(archive, grant, overrides):
    with pytest.raises(EvidenceCaptureError) as error:
        capture(archive, grant, **overrides)
    assert "secret" not in str(error.value)
    assert archive.observation_count == 0


def test_argument_structure_limits_refuse_unbounded_metadata(archive, grant):
    nested = "leaf"
    for _ in range(17):
        nested = [nested]
    for arguments in ({"nested": nested}, {"values": [None] * 4097}, {1: "bad key"},
                      {"value": "🙂" * 20000}, {"value": "\ud800"}):
        with pytest.raises(EvidenceCaptureError):
            capture(archive, grant, source_args=arguments)
    assert archive.observation_count == 0


def test_expired_grant_and_invalid_clocks_refuse_capture(archive, grant, clock):
    with pytest.raises(EvidencePolicyError):
        capture(archive, replace(grant, expires_at=NOW))
    clock.now = NOW.replace(tzinfo=None)
    with pytest.raises(EvidenceUnavailable):
        capture(archive, grant)
    clock.now = NOW
    clock.elapsed = math.nan
    with pytest.raises(EvidenceUnavailable):
        capture(archive, grant)


def test_unrepresentable_monotonic_expiry_refuses_capture(archive, grant, clock):
    clock.elapsed = 1e308
    with pytest.raises(EvidenceUnavailable):
        capture(archive, grant)
    assert archive.observation_count == 0


def test_invalid_grant_type_is_refused(archive):
    with pytest.raises(EvidencePolicyError):
        capture(archive, None)


@pytest.mark.parametrize("field,value", [
    ("owner_id", ""), ("conversation_id", " "), ("audience_id", "x" * 513),
    ("source_agent", None), ("source_tool", "bad\x00tool"),
    ("expires_at", NOW.replace(tzinfo=None)), ("source_deadline", "tomorrow"),
    ("conversation_deadline", NOW.replace(tzinfo=None)),
    ("owner_id", "🙂" * 129), ("owner_id", "\ud800"),
])
def test_invalid_grant_fields_fail_closed(grant, field, value):
    with pytest.raises(EvidencePolicyError):
        replace(grant, **{field: value})


def test_undefined_timezone_is_not_a_retention_deadline(grant):
    class UndefinedTimezone(tzinfo):
        def utcoffset(self, value):
            return None

    with pytest.raises(EvidencePolicyError):
        replace(grant, expires_at=NOW.replace(tzinfo=UndefinedTimezone()))


def test_fingerprint_is_canonical_and_binds_every_field(grant):
    assert grant.fingerprint == replace(grant).fingerprint
    for field in ("owner_id", "conversation_id", "audience_id", "source_agent", "source_tool"):
        assert replace(grant, **{field: "other"}).fingerprint != grant.fingerprint
    assert replace(grant, expires_at=grant.expires_at + timedelta(seconds=1)).fingerprint != grant.fingerprint


def test_policy_loader_and_exact_unique_match(tmp_path, grant):
    path = tmp_path / "policy.json"
    value = serialized_grant(grant)
    value.update(source_deadline=(NOW + timedelta(hours=12)).isoformat(),
                 conversation_deadline=(NOW + timedelta(hours=6)).isoformat())
    path.write_text(json.dumps([value]), encoding="utf-8")
    grants = load_grants(path)
    found = match_grant(grants, **BINDINGS, source_agent=grant.source_agent,
                        source_tool=grant.source_tool, now=NOW)
    assert found.source_deadline == NOW + timedelta(hours=12)
    assert found.conversation_deadline == NOW + timedelta(hours=6)
    with pytest.raises(EvidencePolicyError):
        match_grant(grants, **BINDINGS, source_agent="foreign", source_tool=grant.source_tool, now=NOW)
    with pytest.raises(EvidencePolicyError):
        match_grant([found, found], **BINDINGS, source_agent=grant.source_agent,
                    source_tool=grant.source_tool, now=NOW)
    with pytest.raises(EvidencePolicyError):
        match_grant(grants, **BINDINGS, source_agent=grant.source_agent,
                    source_tool=grant.source_tool, now=found.conversation_deadline)


@pytest.mark.parametrize("grants", [None, {}, [None], []])
def test_invalid_or_missing_policy_inventory_cannot_match(grants):
    with pytest.raises(EvidencePolicyError):
        match_grant(grants, **BINDINGS, source_agent="reader-1", source_tool="read_source", now=NOW)


def test_match_rejects_invalid_bindings_and_clock(grant):
    with pytest.raises(EvidencePolicyError):
        match_grant([grant], **{**BINDINGS, "owner_id": ""}, source_agent="reader-1",
                    source_tool="read_source", now=NOW)
    with pytest.raises(EvidencePolicyError):
        match_grant([grant], **BINDINGS, source_agent="reader-1", source_tool="read_source",
                    now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("document", [
    "", "{invalid}", "{}", "null", "[null]", '[{"owner_id":"a","owner_id":"b"}]',
    "[{}]", "x" * (1024 * 1024 + 1),
    pytest.param("[" * 5000 + "0" + "]" * 5000, id="excessive-nesting"),
])
def test_malformed_policy_file_refuses_all_grants(tmp_path, document):
    path = tmp_path / "policy.json"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(EvidencePolicyError):
        load_grants(path)


@pytest.mark.parametrize("mutation", [
    {"unknown": True}, {"expires_at": "2026-10-09"}, {"expires_at": "2026-10-09T00:00:00"},
    {"source_deadline": None}, {"expires_at": 1},
    {"expires_at": "2026-02-31T00:00:00Z"},
    {"expires_at": "0001-01-01T00:00:00+01:00"},
])
def test_policy_fields_are_strict(tmp_path, grant, mutation):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps([{**serialized_grant(grant), **mutation}]), encoding="utf-8")
    with pytest.raises(EvidencePolicyError):
        load_grants(path)


def test_missing_binary_and_oversized_policy_inventory_fail_closed(tmp_path, grant):
    with pytest.raises(EvidencePolicyError):
        load_grants(tmp_path / "missing.json")
    with pytest.raises(EvidencePolicyError):
        load_grants(tmp_path)
    path = tmp_path / "policy.json"
    path.write_bytes(b"\xff")
    with pytest.raises(EvidencePolicyError):
        load_grants(path)
    path.write_text(json.dumps([serialized_grant(grant)] * 257), encoding="utf-8")
    with pytest.raises(EvidencePolicyError):
        load_grants(path)


def test_nonregular_policy_source_is_refused():
    with pytest.raises(EvidencePolicyError):
        load_grants(os.devnull)


@pytest.mark.parametrize("kwargs", [
    {"observation_limit_bytes": 0}, {"observation_limit_bytes": MAX_OBSERVATION_BYTES + 1},
    {"conversation_limit_bytes": MAX_CONVERSATION_BYTES + 1},
    {"page_limit_bytes": 3}, {"page_limit_bytes": MAX_PAGE_BYTES + 1},
    {"global_limit_bytes": 0}, {"max_records": True}, {"max_records": 0},
])
def test_invalid_archive_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        EvidenceArchive(**kwargs)


@pytest.mark.parametrize("kwargs", [{"clock": 1}, {"monotonic_clock": 1}])
def test_invalid_clock_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        EvidenceArchive(**kwargs)


def test_default_clock_and_limits_are_operational():
    archive = EvidenceArchive()
    grant = RetentionGrant(**BINDINGS, source_agent="reader-1", source_tool="read_source",
                           expires_at=datetime.now(UTC) + timedelta(minutes=5))
    observation = capture(archive, grant)
    assert archive.read(observation.reference, grant=grant, **BINDINGS).text
    assert archive.cleanup() == ()
