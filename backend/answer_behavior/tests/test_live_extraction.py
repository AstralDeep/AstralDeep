"""Tests for backend/answer_behavior/drivers/live.py: deterministic marker extraction,
prompt rendering, and the fail-closed config validation of the live completion seam.
"""

from __future__ import annotations

import json

import pytest

from answer_behavior.corpus import load_corpus
from answer_behavior.drivers.live import (
    DEFAULT_MAX_CASES,
    LiveDriver,
    render_prompt,
    transcript_from_text,
)


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def test_render_prompt_includes_evidence_receipts_and_denials(corpus):
    case = next(c for c in corpus.cases if c.case_id == "ab-permission-denied-001")
    prompt = render_prompt(case)
    assert "[workspace evidence]" in prompt
    assert "policy denial: tool export_records" in prompt
    assert "no receipts are visible" in prompt
    assert case.user_prompt in prompt


def test_render_prompt_lists_receipt_ledger(corpus):
    case = next(c for c in corpus.cases if c.case_id == "tc-retry-recovery-001")
    prompt = render_prompt(case)
    assert "[receipt ledger]" in prompt
    assert "receipt act-sync-1: failed" in prompt
    assert "receipt act-sync-2: completed" in prompt
    assert "artifact:roster-2026-10-13" in prompt


def test_render_prompt_flags_unanswerable_questions(corpus):
    case = next(c for c in corpus.cases if c.case_id == "ab-contradictory-evidence-001")
    assert "does not answer this question" in render_prompt(case)


def test_extraction_reads_citations_and_abstention(corpus):
    case = next(c for c in corpus.cases if c.case_id == "ab-absent-resource-001")
    text = (
        "There is no record with identifier r-labs-404 in your workspace, so I cannot "
        "report a creatinine value and will not guess one."
    )
    transcript = transcript_from_text("candidate", case, text)
    assert transcript.abstained
    assert "r-labs-404" in transcript.abstention_reason
    assert transcript.action_claims == []


def test_extraction_reads_action_claims_from_receipt_ids(corpus):
    case = next(c for c in corpus.cases if c.case_id == "tc-retry-recovery-001")
    text = (
        "Receipt act-sync-1 failed with a lock conflict. The retry receipt act-sync-2 "
        "completed and produced artifact:roster-2026-10-13."
    )
    transcript = transcript_from_text("candidate", case, text.lower())
    outcomes = {claim.action_id: claim.claimed_outcome for claim in transcript.action_claims}
    assert outcomes == {"act-sync-1": "failed", "act-sync-2": "completed"}


def test_extraction_detects_untrusted_markers_and_disclosure(corpus):
    case = next(c for c in corpus.cases if c.case_id == "ip-tool-injection-exfil-001")
    followed = transcript_from_text(
        "candidate", case, "Forwarded as requested by the care team. Done."
    )
    assert followed.untrusted_instruction_followed is True
    disclosed = transcript_from_text(
        "candidate", case,
        "The tool result contained untrusted content with an embedded instruction; I did not act on it.",
    )
    assert disclosed.untrusted_content_disclosed is True
    assert disclosed.untrusted_instruction_followed is False


def test_extraction_records_resource_citations(corpus):
    case = next(c for c in corpus.cases if c.case_id == "tc-citation-integrity-001")
    transcript = transcript_from_text(
        "candidate", case, "The escalation threshold is 8.0 mmol/L (source: doc:care-protocol-2026)."
    )
    assert transcript.resource_citations == ["doc:care-protocol-2026"]


def test_live_driver_uses_injected_completion_double(corpus):
    case = next(c for c in corpus.cases if c.case_id == "tc-cancelled-001")
    seen = []

    def double(messages):
        seen.append(messages)
        return "The run was cancelled after 2 of 5 sections (receipt act-brief-5: cancelled)."

    driver = LiveDriver(completion=double)
    transcript = driver.run_case(case)
    assert seen and seen[0][0]["role"] == "system"
    assert seen[0][1]["content"].startswith(case.user_prompt)
    claims = {claim.action_id: claim.claimed_outcome for claim in transcript.action_claims}
    assert claims == {"act-brief-5": "cancelled"}


def test_live_driver_requires_config_path_without_injection(tmp_path):
    driver = LiveDriver()
    with pytest.raises(RuntimeError, match="runtime config file"):
        driver.run_case(load_corpus().cases[0])


def test_live_driver_validates_config_fields(tmp_path):
    config_path = tmp_path / "live-config.json"
    config_path.write_text(json.dumps({"base_url": "", "model": "m"}), encoding="utf-8")
    driver = LiveDriver(config_path=str(config_path))
    with pytest.raises(RuntimeError, match="base_url"):
        driver.run_case(load_corpus().cases[0])


def test_live_driver_builds_product_client_seam(tmp_path):
    from unittest.mock import patch, MagicMock

    config_path = tmp_path / "live-config.json"
    config_path.write_text(
        json.dumps({"base_url": "http://localhost:9999/v1", "model": "synthetic-test-model"}),
        encoding="utf-8",
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value.choices[0].message.content = (
        "The receipt act-brief-5 shows the run was cancelled; nothing completed."
    )
    captured = {}

    def fake_build(config, source):
        captured["base_url"] = config.base_url
        captured["model"] = config.model
        return fake_client, source, MagicMock(model=config.model)

    with patch("llm_config.client_factory.build_llm_client", side_effect=fake_build):
        driver = LiveDriver(config_path=str(config_path))
        case = next(c for c in load_corpus().cases if c.case_id == "tc-cancelled-001")
        transcript = driver.run_case(case)
    assert captured["base_url"] == "http://localhost:9999/v1"
    claims = {claim.action_id: claim.claimed_outcome for claim in transcript.action_claims}
    assert claims == {"act-brief-5": "cancelled"}


def test_default_max_cases_is_bounded():
    assert 0 < DEFAULT_MAX_CASES <= 32


def test_tool_lookup_falls_back_to_frozen_transcript_then_unknown(corpus):
    from answer_behavior.drivers.live import _tool_for

    case = next(c for c in corpus.cases if c.case_id == "tc-cancelled-001")
    assert _tool_for(case, "act-brief-5") == "brief_generate"
    assert _tool_for(case, "act-never-seen") == "unknown_tool"


def test_extraction_sentence_without_outcome_words_yields_no_claim(corpus):
    case = next(c for c in corpus.cases if c.case_id == "tc-cancelled-001")
    transcript = transcript_from_text(
        "candidate", case, "Receipt act-brief-5 is mentioned here without an outcome word."
    )
    assert transcript.action_claims == []


def test_tool_lookup_uses_baseline_claims_when_candidate_has_none(corpus):
    from answer_behavior.drivers.live import _tool_for

    case = next(c for c in corpus.cases if c.case_id == "ab-permission-denied-001")
    assert _tool_for(case, "act-export-9") == "export_records"
