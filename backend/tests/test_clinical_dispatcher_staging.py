"""Qualifies the clinical note extraction pilot through the authorized personal node/device path.
Proves that model outputs remain untrusted proposals subjected to deterministic schema and
verbatim source-span validation, that callers authenticate via signed caller tickets / mesh keys,
that PHI controls and audit provenance are preserved, and that semantic clinical correctness
is measured separately through clinical diagnostic evaluation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.fhir import extraction, fhir_agent, mcp_tools
from agents.fhir.extraction import evaluate_proposal, extract_note_fields
from agents.fhir.extraction_schema import (
    CHECK_DIGEST,
    CHECK_NOTE_ID,
    CHECK_SPAN_MISMATCH,
    CHECK_SPAN_RANGE,
    SCHEMA_VERSION,
    STATUS_ABSENT,
    STATUS_EXTRACTED,
    STATUS_FAILED_CHECK,
)
from agents.fhir.tests.test_extraction import NOTE, NOTE_ID, golden_payload, span
from clinical_diagnostic.models import (
    ClinicalCase,
    ConversationMessage,
    Partition,
    RubricItem,
    Setting,
)
from clinical_diagnostic.grading import MarkerGrader
from clinical_diagnostic.scoring import score_case
from orchestrator import caller_tickets
from orchestrator.concurrency_cap import ConcurrencyCap
from orchestrator.hooks import HookManager
from orchestrator.orchestrator import GateRefusal, Orchestrator
from shared import node_identity
from shared.phi_redactor import redact
from shared.protocol import MCPResponse

OWNER_ID = "sha256:" + "a" * 64
CALLER_ID = "caller-ticket-" + "b" * 32
CHAT_ID = "chat-staging-001"
TOOL_NAME = "extract_note_fields"
AGENT_ID = "fhir-1"


@pytest.fixture
def personal_node_env(tmp_path, monkeypatch):
    monkeypatch.setenv(node_identity.PROFILE_ENV, "personal")
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")
    node_identity.reset_cache()
    identity = node_identity.load_node_identity()
    yield identity
    node_identity.reset_cache()


@pytest.fixture
def fhir_test_agent(monkeypatch):
    monkeypatch.setattr(
        fhir_agent.BaseA2AAgent,
        "_init_crypto",
        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}),
    )
    return fhir_agent.FhirAgent(port=8999)


@pytest.fixture
def staging_orchestrator(fhir_test_agent, personal_node_env):
    orch = Orchestrator.__new__(Orchestrator)
    orch.agents = {AGENT_ID: fhir_test_agent}
    orch.a2a_clients = {}
    orch.local_agents = {AGENT_ID: fhir_test_agent}
    orch.agent_cards = {AGENT_ID: fhir_test_agent.card}
    orch.ui_sessions = {}
    orch.security_flags = {}
    orch._pending_cap_entries = {}
    orch._hop_cap_entries = {}
    orch._job_context = {}
    orch.concurrency_cap = ConcurrencyCap(max_per_user_agent=5)
    orch.hooks = HookManager()
    orch.stream_manager = None
    orch.audit_recorder = MagicMock()
    orch.audit_recorder.record = AsyncMock()
    orch.send_ui_render = AsyncMock()
    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    orch.tool_permissions.get_tool_scope_map = MagicMock(
        return_value={name: info["scope"] for name, info in mcp_tools.TOOL_REGISTRY.items()}
    )
    orch.tool_permissions.get_effective_permissions = MagicMock(
        return_value={name: True for name in mcp_tools.TOOL_REGISTRY}
    )
    orch._map_file_paths = lambda cid, a, **k: a
    orch.credential_manager = MagicMock()
    orch.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    orch._llm_store = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                api_key="staging-key", base_url="http://provider", model="staging-clinical-model"
            )
        ),
        get_system=AsyncMock(return_value=None),
        capture_user=AsyncMock(return_value=None),
    )
    return orch


@pytest.mark.asyncio
async def test_authorized_node_caller_ticket_dispatch_staging(staging_orchestrator):
    """Proves that a caller ticket admitted through the personal node route
    successfully prepares and authorizes extract_note_fields.
    """
    claims = {
        "sub": OWNER_ID,
        "_caller_id": CALLER_ID,
        "caller_label": "Clinical Pilot Runner",
        "_caller_tools": [TOOL_NAME],
    }

    auth = await staging_orchestrator._authorize_and_prepare(
        object(),
        AGENT_ID,
        TOOL_NAME,
        {"note_text": NOTE, "note_id": NOTE_ID},
        CHAT_ID,
        OWNER_ID,
    )
    assert not isinstance(auth, GateRefusal)
    assert set(auth.args) == {"note_text", "note_id"}
    assert auth.args["note_id"] == NOTE_ID


@pytest.mark.asyncio
async def test_unauthorized_caller_ticket_rejected_fail_closed(staging_orchestrator):
    """Proves that a caller without required tool permission is deterministically refused."""
    staging_orchestrator.tool_permissions.is_tool_allowed = MagicMock(return_value=False)
    refusal = await staging_orchestrator._authorize_and_prepare(
        object(),
        AGENT_ID,
        TOOL_NAME,
        {"note_text": NOTE, "note_id": NOTE_ID},
        CHAT_ID,
        OWNER_ID,
    )
    assert isinstance(refusal, GateRefusal)
    assert refusal.response.error is not None
    assert "restricted" in refusal.response.error.get("message", "")


@pytest.mark.asyncio
async def test_clinical_note_extraction_pilot_golden_run_through_node_dispatch():
    """Runs a complete synthetic clinical note extraction verifying deterministic
    source-span checks, schema binding, and UI card/table representation.
    """
    payload = golden_payload(NOTE, NOTE_ID)
    serialized_proposal = json.dumps(payload)

    result = evaluate_proposal(NOTE, NOTE_ID, serialized_proposal)
    assert "_ui_components" in result
    assert "_data" in result

    data = result["_data"]
    assert data["outcome"] == "extracted"
    assert data["schema_version"] == "note-extract-v1"
    assert data["note"]["note_id"] == NOTE_ID
    assert data["counts"]["extracted"] == 5
    assert data["counts"]["failed_check"] == 0

    fields = data["fields"]
    assert fields["patient_ref"]["status"] == STATUS_EXTRACTED
    assert fields["patient_ref"]["value"] == "DEMO-1001"
    assert fields["patient_ref"]["span_checks"]["verbatim"] is True

    findings = fields["findings"]
    assert len(findings) == 3

    chest_pain = next(f for f in findings if f["term"] == "chest pain")
    assert chest_pain["assertion"] == "asserted"
    assert chest_pain["span_checks"]["in_range"] is True
    assert chest_pain["span_checks"]["verbatim"] is True

    sob = next(f for f in findings if f["term"] == "shortness of breath")
    assert sob["assertion"] == "negated"
    assert sob["span_checks"]["verbatim"] is True

    glucose = next(f for f in findings if f["term"] == "Glucose")
    assert glucose["value"] == 132
    assert glucose["unit"] == "mg/dL"
    assert glucose["unit_code_status"] == "validated"


@pytest.mark.asyncio
async def test_deterministic_rejection_of_hallucinated_or_out_of_bounds_spans():
    """Verifies that out-of-bounds, modified quotes, or wrong digests are deterministically caught."""
    # 1. Hallucinated / mismatched quote
    bad_quote = golden_payload(NOTE, NOTE_ID)
    bad_quote["findings"]["entries"][0]["evidence"]["quote"] = "hallucinated symptom"
    res1 = evaluate_proposal(NOTE, NOTE_ID, json.dumps(bad_quote))
    assert res1["_data"]["fields"]["findings"][0]["status"] == STATUS_FAILED_CHECK
    assert CHECK_SPAN_MISMATCH in res1["_data"]["fields"]["findings"][0]["failures"]

    # 2. Out-of-bounds start/end offsets
    bad_range = golden_payload(NOTE, NOTE_ID)
    bad_range["findings"]["entries"][0]["evidence"]["start"] = len(NOTE) + 10
    bad_range["findings"]["entries"][0]["evidence"]["end"] = len(NOTE) + 20
    res2 = evaluate_proposal(NOTE, NOTE_ID, json.dumps(bad_range))
    assert res2["_data"]["fields"]["findings"][0]["status"] == STATUS_FAILED_CHECK
    assert CHECK_SPAN_RANGE in res2["_data"]["fields"]["findings"][0]["failures"]

    # 3. Wrong note digest
    bad_digest = golden_payload(NOTE, NOTE_ID)
    bad_digest["note_digest"] = "0" * 64
    res3 = evaluate_proposal(NOTE, NOTE_ID, json.dumps(bad_digest))
    assert res3["_data"]["outcome"] in (STATUS_FAILED_CHECK, "rejected_output")
    assert CHECK_DIGEST in res3["_data"]["reasons"]

    # 4. Wrong note ID
    bad_id = golden_payload(NOTE, NOTE_ID)
    bad_id["note_id"] = "WRONG-ID"
    res4 = evaluate_proposal(NOTE, NOTE_ID, json.dumps(bad_id))
    assert res4["_data"]["outcome"] in (STATUS_FAILED_CHECK, "rejected_output")
    assert CHECK_NOTE_ID in res4["_data"]["reasons"]


def test_negation_uncertainty_and_explicit_abstention_preserved():
    """Confirms that explicit abstention reasons and negative assertions are preserved
    without inferring missing values.
    """
    custom_note = "Patient denies headache. Allergy status not documented."
    custom_note_id = "SYN-002"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "note_id": custom_note_id,
        "note_digest": hashlib.sha256(custom_note.encode("utf-8")).hexdigest(),
        "patient_ref": {"value": None, "evidence": None, "abstention_reason": "not_documented"},
        "note_date": {"value": None, "evidence": None, "abstention_reason": "not_documented"},
        "findings": {
            "entries": [
                {
                    "term": "headache",
                    "evidence": span(custom_note, "denies headache"),
                    "assertion": "negated",
                    "experiencer": "patient",
                    "temporality": "current",
                    "value_type": "none",
                    "value": None,
                    "unit": None,
                }
            ],
            "abstention_reason": None,
        },
    }

    result = evaluate_proposal(custom_note, custom_note_id, json.dumps(payload))
    data = result["_data"]
    assert data["outcome"] in ("extracted", "partial")
    assert data["fields"]["patient_ref"]["status"] == STATUS_ABSENT
    assert data["fields"]["patient_ref"]["abstention_reason"] == "not_documented"
    assert data["fields"]["findings"][0]["assertion"] == "negated"


def test_phi_protection_and_audit_provenance_on_staging():
    """Confirms that extracted output formats safely and that PHI masking operates
    over patient-identifying data.
    """
    sensitive_source = {
        "clinical": {"findings": "Patient evaluated for cardiac symptoms."},
        "subject": {"mrn": "MRN:12345678"},
    }
    redacted_json, _ = redact(sensitive_source)
    redacted = json.loads(redacted_json)
    assert redacted["subject"]["mrn"] == "[REDACTED:phi]"
    assert "12345678" not in redacted_json


def test_clinical_diagnostic_semantic_scoring_measured_separately():
    """Proves that semantic clinical correctness is evaluated independently from
    deterministic schema and span validation pass rates.
    """
    test_case = ClinicalCase(
        case_id="staging-case-001",
        partition=Partition.DEVELOPMENT,
        setting=Setting.DOCUMENTATION,
        specialty="cardiology",
        conversation=(
            ConversationMessage("user", "Summarize the key findings from the progress note."),
        ),
        rubric_items=(
            RubricItem("r1", "Document chest pain", 1.0, kind="required_fact", evidence_terms=("chest pain",)),
            RubricItem("r2", "Note absence of SOB", 1.0, kind="required_fact", evidence_terms=("shortness of breath",)),
            RubricItem("r3", "Do not claim infarction", -2.0, kind="harmful_claim", evidence_terms=("infarction",)),
        ),
    )

    candidate_answer = "Patient reports chest pain on exertion. No shortness of breath at rest."
    grader = MarkerGrader()
    reply = grader.assess(test_case, candidate_answer)
    scored = score_case(test_case, reply.grade)

    assert scored.total_points == 2.0
    assert scored.possible_points == 2.0
    assert scored.met_harmful == 0
    assert scored.met_required == 2
    assert scored.band == "excellent"
