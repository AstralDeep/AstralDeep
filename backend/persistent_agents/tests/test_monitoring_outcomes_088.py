"""Typed monitoring outcomes are exact, bounded and never invent equality (T042)."""

from uuid import uuid4

import pytest

from persistent_agents.monitoring_observation import (
    INCORPORATED_KINDS,
    KINDS,
    MAX_OBSERVATION_BYTES,
    build_initial_result,
    classify_observation,
    event_observation,
    extraction_facts,
    extractive_passages,
    normalize_url,
    observation_shape,
    prior_observation,
    valid_observation_record,
)
from persistent_agents.research_result import page_passages, retain_page_observation
from persistent_agents.runtime_values import canonical, digest

URL = "https://example.org/releases"
SOURCE = {"profile": "public_page", "agent_id": "web-research-1", "tool_name": "fetch_page",
          "arguments": {"url": URL}, "linked_document_urls": []}
SOURCE_DIGEST = digest(SOURCE)
ACTION = str(uuid4())


def legacy(text="Release version 2 published.", *, truncated=False, redacted=False):
    return {"text": text, "revision_digest": digest({"text": text, "data": None}),
            "truncated": truncated, "redacted": redacted}


def page(text="The release is stable.", **flags):
    raw = {"version": 1, "requested_url": URL, "final_url": "https://Example.org:443/releases",
           "retrieved_at": "2026-09-16T10:00:00+00:00", "media_type": "text/plain",
           "extraction_profile": "plain_text_v1", "title": "", "text": text,
           "body_complete": True, "extraction_complete": True, "excerpt_complete": True,
           "redacted": False}
    raw.update(flags)
    return retain_page_observation(raw, source_action_id=ACTION, redacted=False)


def binding(observed, sequence=1):
    return {"revision_digest": observed["revision_digest"], "result_digest": digest(observed),
            "sequence": sequence}


def classify(prior, observed):
    return classify_observation(prior, observed, extraction_facts(observed, SOURCE))


def test_shapes_are_closed():
    assert observation_shape(legacy()) == "legacy" and observation_shape(page()) == "page"
    for value in [None, {}, {"text": "x", "revision_digest": "a" * 64},
                  {**legacy(), "extra": 1}, {**legacy(), "truncated": 1}, {**page(), "body_complete": None}]:
        assert observation_shape(value) is None
    with pytest.raises(ValueError, match="assignment_observation_invalid"):
        extraction_facts({"text": "x", "revision_digest": "a" * 64}, SOURCE)


def test_initial_observation_starts_the_sequence_without_a_prior_binding():
    record = classify(None, legacy())
    assert record["kind"] == "initial" and record["reason"] is None
    assert record["observation_sequence"] == 1
    assert record["prior_result_digest"] is None and record["prior_revision_digest"] is None
    assert record["revision_digest"] == legacy()["revision_digest"]
    assert record["context_digest"] == digest(legacy())
    assert record["complete_source_set"] == [URL] and record["normalized_final_urls"] == []
    assert record["completeness"] == {"body_complete": None, "extraction_complete": None,
                                      "excerpt_complete": True}
    assert record["source_configuration_digest"] == SOURCE_DIGEST
    assert valid_observation_record(record)


def test_byte_identical_content_is_unchanged_and_reordered_content_is_changed():
    first = legacy("Alpha released. Beta planned.")
    identical = legacy("Alpha released. Beta planned.")
    reordered = legacy("Beta planned. Alpha released.")
    prior = binding(first, sequence=3)
    unchanged = classify(prior, identical)
    assert unchanged["kind"] == "unchanged" and unchanged["observation_sequence"] == 3
    assert unchanged["prior_result_digest"] == digest(first)
    assert unchanged["prior_revision_digest"] == first["revision_digest"]
    changed = classify(prior, reordered)
    assert changed["kind"] == "changed" and changed["observation_sequence"] == 4
    assert changed["prior_result_digest"] == digest(first)
    assert changed["revision_digest"] == reordered["revision_digest"] != first["revision_digest"]


def test_page_observations_compare_by_content_revision_not_by_read_identity():
    first = page()
    again = retain_page_observation({key: value for key, value in first.items()
                                     if key not in {"source_action_id", "revision_digest"}},
                                    source_action_id=str(uuid4()), redacted=False)
    assert again["revision_digest"] == first["revision_digest"] and digest(again) != digest(first)
    record = classify(binding(first), again)
    assert record["kind"] == "unchanged"
    assert record["normalized_final_urls"] == ["https://example.org/releases"]
    assert record["complete_source_set"] == [URL]
    assert record["completeness"] == {"body_complete": True, "extraction_complete": True,
                                      "excerpt_complete": True}


@pytest.mark.parametrize("flags", [{"body_complete": False}, {"extraction_complete": False},
                                   {"body_complete": False, "extraction_complete": False}])
@pytest.mark.parametrize("has_prior", [False, True])
def test_incomplete_extraction_is_never_initial_unchanged_or_changed(flags, has_prior):
    complete = page()
    incomplete = page(**flags)
    prior = binding(complete, sequence=2) if has_prior else None
    record = classify(prior, incomplete)
    assert record["kind"] == "insufficient_evidence" and record["reason"] == "extraction_incomplete"
    assert record["observation_sequence"] == (2 if has_prior else 0)
    assert record["complete_source_set"] == []
    assert record["prior_result_digest"] == (digest(complete) if has_prior else None)
    # The same bytes classified as unchanged only when the extractor was complete.
    assert classify(binding(incomplete), incomplete)["kind"] == "insufficient_evidence"
    assert classify(binding(complete), complete)["kind"] == "unchanged"


def test_excerpt_truncation_is_recorded_scope_not_a_gate():
    truncated = legacy("Long page text. " * 400, truncated=True)
    record = classify(None, truncated)
    assert record["kind"] == "initial" and record["completeness"]["excerpt_complete"] is False
    assert classify(binding(truncated), truncated)["kind"] == "unchanged"
    cut = page(excerpt_complete=False)
    assert classify(binding(cut), cut)["kind"] == "unchanged"


@pytest.mark.parametrize("prior", [
    {"revision_digest": None, "result_digest": None, "sequence": 2},
    {"revision_digest": "b" * 64, "result_digest": None, "sequence": 2},
    {"revision_digest": None, "result_digest": "c" * 64, "sequence": 2},
])
def test_missing_prior_result_bytes_are_insufficient_evidence_never_equality(prior):
    observed = legacy()
    prior = {**prior, "revision_digest": observed["revision_digest"]
             if prior["revision_digest"] is not None else None}
    record = classify(prior, observed)
    assert record["kind"] == "insufficient_evidence" and record["reason"] == "prior_result_missing"
    assert record["observation_sequence"] == 2
    assert record["prior_result_digest"] == prior["result_digest"]


def test_observation_without_revision_identity_is_insufficient_evidence():
    observed = {**legacy(), "revision_digest": "not-a-digest"}
    record = classify(binding(legacy()), observed)
    assert record["kind"] == "insufficient_evidence" and record["reason"] == "revision_unavailable"
    assert record["revision_digest"] is None and record["context_digest"] == digest(observed)


@pytest.mark.parametrize("prior", [
    {"revision_digest": "a" * 64, "sequence": 1}, {"revision_digest": "zz", "result_digest": None, "sequence": 1},
    {"revision_digest": None, "result_digest": None, "sequence": -1}, "prior", [],
])
def test_malformed_prior_binding_is_refused(prior):
    with pytest.raises(ValueError, match="assignment_observation_invalid"):
        classify(prior, legacy())


def test_records_are_bounded_text_free_and_typed():
    for observed in (legacy("Private looking prose " * 100), page("Exact page text " * 300)):
        record = classify(None, observed)
        assert record["kind"] in KINDS and set(INCORPORATED_KINDS) <= KINDS
        assert "text" not in record and observed["text"][:40] not in canonical(record)
        assert len(canonical(record).encode("utf-8")) <= MAX_OBSERVATION_BYTES
        assert valid_observation_record(record)
    for broken in [{**classify(None, legacy()), "kind": "other"}, {**classify(None, legacy()), "extra": 1},
                   {**classify(None, legacy()), "revision_digest": None},
                   {**classify(None, legacy()), "observation_sequence": "1"}, None, "record"]:
        assert not valid_observation_record(broken)


def test_prior_observation_reads_typed_records_before_legacy_cursors():
    first = classify(None, legacy())
    assert prior_observation({"observation": first}, source_configuration_digest=SOURCE_DIGEST) == binding(legacy())
    unchanged = classify(binding(legacy()), legacy())
    assert prior_observation({"observation": unchanged}, source_configuration_digest=SOURCE_DIGEST) == binding(legacy())
    insufficient = classify({"revision_digest": "b" * 64, "result_digest": None, "sequence": 1}, legacy())
    assert prior_observation({"observation": insufficient}, source_configuration_digest=SOURCE_DIGEST) is None
    # Another source configuration or a malformed record is not a prior.
    assert prior_observation({"observation": first}, source_configuration_digest="0" * 64) is None
    assert prior_observation({"observation": {**first, "kind": "weird"}, "cursor": {"revision": first["revision_digest"], "sequence": 1}},
                             source_configuration_digest=SOURCE_DIGEST) is None
    assert prior_observation(None, source_configuration_digest=SOURCE_DIGEST) is None
    assert prior_observation({}, source_configuration_digest=SOURCE_DIGEST) is None


def test_legacy_cursor_binds_only_to_matching_retained_bytes():
    retained = legacy()
    cursor = {"revision": retained["revision_digest"], "sequence": 2}
    assert prior_observation({"cursor": cursor, "last_observation": retained},
                             source_configuration_digest=SOURCE_DIGEST) == binding(retained, 2)
    without_bytes = prior_observation({"cursor": cursor}, source_configuration_digest=SOURCE_DIGEST)
    assert without_bytes == {"revision_digest": retained["revision_digest"], "result_digest": None, "sequence": 2}
    stale = prior_observation({"cursor": cursor, "last_observation": legacy("other")},
                              source_configuration_digest=SOURCE_DIGEST)
    assert stale["result_digest"] is None
    for cursor in [{"revision": "x", "sequence": 1}, {"revision": "a" * 64, "sequence": 0}, {"revision": "a" * 64}, "cursor"]:
        assert prior_observation({"cursor": cursor}, source_configuration_digest=SOURCE_DIGEST) is None


def test_pending_event_prior_comes_from_the_earlier_retained_observation():
    earlier = legacy("earlier")
    checkpoint = {"cursor": {"revision": legacy("now")["revision_digest"], "sequence": 2}, "last_observation": earlier}
    assert prior_observation(checkpoint, source_configuration_digest=SOURCE_DIGEST, pending_sequence=2) == binding(earlier, 1)
    assert prior_observation(checkpoint, source_configuration_digest=SOURCE_DIGEST, pending_sequence=1) is None
    assert prior_observation({"cursor": checkpoint["cursor"]}, source_configuration_digest=SOURCE_DIGEST,
                             pending_sequence=3) == {"revision_digest": None, "result_digest": None, "sequence": 2}


def test_event_observation_kind_is_fixed_by_the_ledger():
    observed = legacy("now")
    facts = extraction_facts(observed, SOURCE)
    initial = event_observation({}, observed, facts, sequence=1, revision=observed["revision_digest"])
    assert initial["kind"] == "initial" and initial["prior_result_digest"] is None
    earlier = legacy("earlier")
    changed = event_observation({"observation": classify(None, earlier)}, observed, facts,
                                sequence=2, revision=observed["revision_digest"])
    assert changed["kind"] == "changed" and changed["prior_result_digest"] == digest(earlier)
    assert changed["observation_sequence"] == 2
    for sequence, revision in [(0, observed["revision_digest"]), (1, "a" * 64), ("1", observed["revision_digest"])]:
        with pytest.raises(ValueError, match="assignment_observation_invalid"):
            event_observation({}, observed, facts, sequence=sequence, revision=revision)


def test_normalized_final_urls_lowercase_host_drop_fragment_and_default_port():
    assert normalize_url("HTTPS://Example.org:443/Path?q=A#frag") == "https://example.org/Path?q=A"
    assert normalize_url("https://example.org") == "https://example.org/"
    assert normalize_url("https://example.org:8443/x") == "https://example.org:8443/x"
    assert normalize_url("http://Example.org:80/") == "http://example.org/"
    for bad in ["", "example.org/path", "https://", 7, "https://" + "a" * 8200]:
        with pytest.raises(ValueError, match="assignment_observation_invalid"):
            normalize_url(bad)


def test_extractive_passages_match_the_fixed_reader_split_exactly():
    text = "Public release details and notes. " * 120
    retained = page(text)
    assert extractive_passages(retained["text"]) == page_passages(retained)
    assert extractive_passages("") == []
    with pytest.raises(ValueError, match="assignment_research_result_invalid"):
        extractive_passages(None)


def test_initial_result_binds_legacy_excerpts_to_the_read_without_invented_facts():
    observed = legacy("Alpha released. " * 100, truncated=True)
    facts = extraction_facts(observed, SOURCE)
    result = build_initial_result(observed, facts, source_action_id=ACTION, source_result_digest="d" * 64)
    assert result["scope"] == "one_page_excerpts" and result["disposition"] == "evidence"
    assert result["source"] == {"requested_url": URL, "action_id": ACTION, "result_digest": "d" * 64,
                                "revision_digest": observed["revision_digest"],
                                "excerpt_complete": False, "redacted": False}
    assert result["passages"] == extractive_passages(observed["text"])[:8]
    assert len(canonical(result).encode("utf-8")) <= 8192
    assert build_initial_result(legacy("  "), facts, source_action_id=ACTION,
                                source_result_digest="d" * 64)["disposition"] == "insufficient_evidence"
    for action, result_digest in [("not-an-id", "d" * 64), (ACTION, "short"), (ACTION, None)]:
        with pytest.raises(ValueError, match="assignment_research_result_invalid"):
            build_initial_result(observed, facts, source_action_id=action, source_result_digest=result_digest)


def test_initial_result_for_page_observations_uses_the_exact_page_result():
    observed = page("Stable release text. " * 200)
    facts = extraction_facts(observed, SOURCE)
    result = build_initial_result(observed, facts, source_action_id=ACTION, source_result_digest=digest(observed))
    assert result["disposition"] == "evidence" and len(result["passages"]) == 8
    assert result["passages"] == page_passages(observed)[:8]
    assert result["source"]["action_id"] == ACTION and result["source"]["final_url"] == observed["final_url"]
    with pytest.raises(ValueError, match="assignment_research_result_invalid"):
        build_initial_result(observed, facts, source_action_id=str(uuid4()), source_result_digest=digest(observed))
