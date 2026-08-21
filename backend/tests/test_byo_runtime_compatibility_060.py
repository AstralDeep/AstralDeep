"""Neutral backend side of the feature-060 BYO runtime compatibility contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

import astralprojection
from astralplane import GENERATED_AGENT_BUNDLE_CONTRACT, FinalizedBundle
import pytest

from orchestrator.agent_generator import (
    BYO_LEGACY_RUNTIME_DISPOSITIONS,
    BYO_RUNTIME_CONTRACT_VERSION,
    BYO_RUNTIME_LOCK_ARTIFACT,
    BYO_RUNTIME_LOCK_SHA256,
    AgentCodeGenerator,
)
from orchestrator.user_agents import RuntimeCompatibilityPolicy


_PROJECTION_ROOT = Path(astralprojection.__file__).resolve().parents[2]
_PROJECTION_LOCK = _PROJECTION_ROOT / Path(BYO_RUNTIME_LOCK_ARTIFACT).relative_to(
    Path("components") / "AstralProjection"
)
_FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "runtime_reliability_060"
        / "runtime-lock-contract.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.skipif(
    not _PROJECTION_LOCK.is_file(),
    reason="active Projection runtime lock is not part of the product image",
)
def test_final_release_lock_is_the_only_backend_runtime_identity() -> None:
    assert _FIXTURE["runtime_contract_version"] == BYO_RUNTIME_CONTRACT_VERSION
    assert _FIXTURE["lock_artifact"] == BYO_RUNTIME_LOCK_ARTIFACT
    assert _FIXTURE["lock_digest"] == BYO_RUNTIME_LOCK_SHA256
    assert hashlib.sha256(_PROJECTION_LOCK.read_bytes()).hexdigest() == (
        BYO_RUNTIME_LOCK_SHA256
    )
    policy = RuntimeCompatibilityPolicy(
        runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
        runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
    )
    assert policy.runtime_contract_version == 3
    assert policy.runtime_lock_sha256 == BYO_RUNTIME_LOCK_SHA256
    assert BYO_LEGACY_RUNTIME_DISPOSITIONS == {2: "dispatch_mediated_only"}


def test_neutral_complete_bundle_digest_vector_matches_generator() -> None:
    vector = _FIXTURE["bundle_digest_vector"]
    files = vector["files"]
    assert _FIXTURE["bundle_digest_contract"] == "canonical-json-utf8-v1"
    canonical = json.dumps(
        files,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == vector["bundle_sha256"]
    assert AgentCodeGenerator._bundle_digest(files) == vector["bundle_sha256"]


def test_finalized_delivery_metadata_cannot_drift_from_selected_release_lock() -> None:
    generator = AgentCodeGenerator(llm_client=object(), llm_model="unused")
    vector = _FIXTURE["bundle_digest_vector"]
    finalized = generator.finalize_byo_bundle(
        files=vector["files"],
        agent_id="ua-runtime-compatibility",
        revision_id=str(uuid.uuid4()),
        agent_name="Compatibility Agent",
        description="exercises the selected packaged runtime lock",
        constitution_version="0.1.0",
        required_runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
    )
    assert isinstance(finalized, FinalizedBundle)
    assert finalized.contract is GENERATED_AGENT_BUNDLE_CONTRACT
    assert finalized.bundle_sha256 == vector["bundle_sha256"]
    assert finalized.manifest["runtime_contract_version"] == 3
    assert finalized.manifest["required_runtime_lock_sha256"] == (
        BYO_RUNTIME_LOCK_SHA256
    )
    with pytest.raises(ValueError, match="runtime lock"):
        generator.finalize_byo_bundle(
            files=vector["files"],
            agent_id="ua-runtime-compatibility",
            revision_id=str(uuid.uuid4()),
            agent_name="Compatibility Agent",
            description="rejects malformed runtime metadata",
            constitution_version="0.1.0",
            required_runtime_lock_sha256="not-a-digest",
        )
