"""Production harness resolution accepts only code-owned qualified deployment facts and exact packaged bytes.
Work and ordinary dispatch share these checks; library conformance reports never grant rollout authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from llm_config import research_profile
from orchestrator.harness.deployment_allowlist import QUALIFIED_DEPLOYMENTS
from persistent_agents.models import AssignmentError
from orchestrator.harness.contract import HarnessIdentity
from orchestrator.harness.standard_llm_adapter import StandardLLMHarness


class HarnessSelectionError(AssignmentError):
    def __init__(self, code="harness_unavailable", status=503):
        self.status = status
        super().__init__(code, status)


@dataclass(frozen=True, slots=True)
class _PackagedFact:
    identity: HarnessProfileIdentity
    qualification_digest: str
    deployment_digest: str
    config_binding_key_id: str = field(repr=False)
    config_revision: str = field(repr=False)
    provider: str
    model: str
    implementation: tuple[tuple[str, str], ...]
    adapter_identity: HarnessIdentity

    @property
    def profile(self):
        if self.identity.profile != "standard_local":
            raise HarnessSelectionError("harness_profile_unsupported")
        return research_profile.LOCAL_PROFILE


_REQUIRED_IMPLEMENTATION = frozenset({
    "orchestrator/harness/contract.py", "orchestrator/harness/standard_llm_adapter.py",
    "orchestrator/harness/production_profiles.py", "orchestrator/harness/selection_service.py",
    "orchestrator/harness/host_admission.py", "orchestrator/harness/execution_binding.py",
    "orchestrator/harness/runtime.py",
    "orchestrator/harness/selection_api.py",
    "orchestrator/async_tasks.py", "orchestrator/subtasks.py",
    "orchestrator/goal_progress.py", "orchestrator/goal_project_context.py",
    "orchestrator/goal_verification.py", "orchestrator/conversation_publication.py",
    "orchestrator/history.py", "shared/feature_flags.py",
    "orchestrator/work_resume.py", "orchestrator/work_wake.py", "orchestrator/work_admission_api.py",
    "orchestrator/orchestrator.py", "orchestrator/work_admission.py", "orchestrator/work_submit.py",
    "llm_config/research_profile.py", "llm_config/user_store.py", "llm_config/invocation.py",
    "llm_config/audit_events.py", "llm_config/provider_egress.py", "llm_config/client_factory.py",
    "llm_config/evidence_transport.py", "shared/isolated_http.py", "shared/external_http.py",
    "shared/net_guard.py",
    "persistent_agents/research_input.py", "persistent_agents/execution.py",
    "persistent_agents/runner.py", "persistent_agents/chat_episode.py",
    "persistent_agents/service.py", "persistent_agents/dispatch_context.py",
    "orchestrator/work_result.py", "orchestrator/work_chat_result.py",
    "personalization/selected_guidance.py", "personalization/selected_guidance_boundary.py",
    "config/astral-composition.json", "backend/requirements.txt",
})
_DIGEST = re.compile(r"[a-f0-9]{64}")
_IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[2]


def _implementation_path(name):
    if name not in _REQUIRED_IMPLEMENTATION:
        raise ValueError
    backend = _IMPLEMENTATION_ROOT
    if name == "config/astral-composition.json":
        base, path = backend.parent, backend.parent / name
    elif name == "backend/requirements.txt":
        base, path = backend, backend / "requirements.txt"
    else:
        base, path = backend, backend / name
    if (path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path
            or not path.is_relative_to(base)):
        raise ValueError
    return path


def _verify_fact(fact):
    try:
        if (type(fact) is not _PackagedFact or type(fact.identity) is not HarnessProfileIdentity
                or fact.identity.profile != "standard_local"
                or type(fact.adapter_identity) is not HarnessIdentity
                or fact.adapter_identity != StandardLLMHarness().identity
                or type(fact.qualification_digest) is not str
                or _DIGEST.fullmatch(fact.qualification_digest) is None
                or type(fact.deployment_digest) is not str
                or _DIGEST.fullmatch(fact.deployment_digest) is None
                or type(fact.config_revision) is not str
                or _DIGEST.fullmatch(fact.config_revision) is None
                or type(fact.config_binding_key_id) is not str
                or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", fact.config_binding_key_id) is None
                or fact.provider not in research_profile.LOCAL_PROVIDERS
                or type(fact.model) is not str or not 1 <= len(fact.model) <= 256
                or fact.model != fact.model.strip() or any(ord(c) < 33 for c in fact.model)
                or type(fact.implementation) is not tuple
                or not 1 <= len(fact.implementation) <= 64):
            raise ValueError
        observed = []
        for entry in fact.implementation:
            if type(entry) is not tuple or len(entry) != 2:
                raise ValueError
            name, expected = entry
            if (type(name) is not str or name not in _REQUIRED_IMPLEMENTATION
                    or type(expected) is not str or _DIGEST.fullmatch(expected) is None):
                raise ValueError
            path = _implementation_path(name)
            with path.open("rb") as source:
                content = source.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise ValueError
            current = hashlib.sha256(content).hexdigest()
            if current != expected:
                raise ValueError
            observed.append((name, current))
        if len(set(name for name, _ in observed)) != len(observed):
            raise ValueError
        if set(name for name, _ in observed) != _REQUIRED_IMPLEMENTATION:
            raise ValueError
        digest = hashlib.sha256(json.dumps(sorted(observed), separators=(",", ":")).encode()).hexdigest()
        if digest != fact.identity.artifact_digest:
            raise ValueError
        return fact
    except (ValueError, TypeError, OSError, UnicodeError):
        raise HarnessSelectionError("harness_artifact_unavailable") from None


def _fact_from_record(record):
    try:
        if type(record) is not dict or set(record) != {
                "identity", "qualification_digest", "deployment_digest",
                "config_binding_key_id", "config_revision", "provider", "model", "implementation", "adapter_identity"}:
            raise ValueError
        if (type(record["adapter_identity"]) is not dict or set(record["adapter_identity"]) != {
                "harness_id", "profile", "adapter_version", "contract_version"}):
            raise ValueError
        return _PackagedFact(HarnessProfileIdentity(**record["identity"]),
            record["qualification_digest"], record["deployment_digest"],
            record["config_binding_key_id"], record["config_revision"],
            record["provider"], record["model"],
            tuple(tuple(entry) for entry in record["implementation"]),
            HarnessIdentity(**record["adapter_identity"]))
    except (TypeError, ValueError, KeyError):
        raise HarnessSelectionError("harness_artifact_unavailable") from None


def deployment_for(harness_id):
    if type(harness_id) is not str or harness_id != "standard_local":
        raise HarnessSelectionError("harness_profile_unknown", 422)
    for record in QUALIFIED_DEPLOYMENTS:
        fact = _fact_from_record(record)
        if fact.identity.profile == harness_id:
            return _verify_fact(fact)
    raise HarnessSelectionError("harness_qualification_missing", 409)


def deployment_for_capture(capture):
    try:
        from astralplane.repositories.harness_capture import HarnessCapture
    except ImportError:
        raise HarnessSelectionError("harness_component_unavailable") from None
    if type(capture) is not HarnessCapture:
        raise HarnessSelectionError("harness_capture_invalid")
    fact = deployment_for(capture.identity.profile)
    if (capture.identity != fact.identity or capture.qualification_digest != fact.qualification_digest
            or capture.research_profile != fact.profile.name):
        raise HarnessSelectionError("harness_capture_changed")
    return fact


def profile_for_record(record):
    capture = getattr(record, "harness_capture", None)
    return (research_profile.OPENAI_PROFILE if capture is None
            else deployment_for_capture(capture).profile)


def profile_for_capture_value(value):
    if value is None:
        return research_profile.OPENAI_PROFILE
    try:
        from astralplane.repositories.harness_capture import harness_capture_from_value
        captured = harness_capture_from_value(value)
    except Exception:
        raise HarnessSelectionError("harness_capture_invalid") from None
    return deployment_for_capture(captured).profile


def model_route(record=None):
    capture = getattr(record, "harness_capture", None)
    fact = None if capture is None else deployment_for_capture(capture)
    return {"kind": "model", "provider": "openai" if fact is None else fact.provider,
        "model": research_profile.MODEL if fact is None else fact.model,
        "max_output_tokens": research_profile.OUTPUT_TOKENS if fact is None else fact.profile.output_tokens,
        "response_format": {"type": "json_object"}}


def public_profiles():
    try:
        deployment_for("standard_local")
        available, reason = True, None
    except HarnessSelectionError as error:
        available, reason = False, error.code
    return [{"harness_id": "standard_local", "available": available, "reason": reason,
        "context_tokens": research_profile.LOCAL_CONTEXT_TOKENS,
        "output_tokens": research_profile.LOCAL_OUTPUT_TOKENS,
        "reserved_tokens": research_profile.LOCAL_RESERVED_TOKENS,
        "reserved_milliseconds": research_profile.LOCAL_RESERVED_MILLISECONDS}]
