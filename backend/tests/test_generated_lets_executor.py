from __future__ import annotations

import hashlib
import json
import sys
import time
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from lets.canonical import b64url_encode, canonical_digest, canonical_json
from lets.crypto import Ed25519Signer
from lets.errors import AuthorityAnchorTransportError, StorageError
from lets.manifest import (
    ClusterManifest,
    ManifestPublicKey,
    ManifestSignature,
    WardenManifest,
)
from lets.models import Receipt
from lets.policy import MachineSpec, PolicySpec, ResourceDimension, TransitionSpec

from orchestrator import generated_lets_executor as executor
from orchestrator.agent_generator import (
    BYO_BUNDLE_FILENAMES,
    BYO_LEGACY_RUNTIME_DISPOSITIONS,
    BYO_RUNTIME_CONTRACT_VERSION,
    AgentCodeGenerator,
)
from shared.base_agent import BaseA2AAgent
from shared.protocol import MCP_PROTOCOL_VERSION, MCPRequest


AGENT_ID = "ua-generated-test"


def test_generator_emits_one_reviewed_public_v1011_adapter_for_v3() -> None:
    generator = AgentCodeGenerator(llm_client=object(), llm_model="unused")
    byo = generator.generate_byo_scaffold(
        agent_name="Demo", description="description", agent_id=AGENT_ID
    )
    backend = generator.generate_template_files(
        "Demo", "description", "demo", agent_id=AGENT_ID
    )

    assert BYO_RUNTIME_CONTRACT_VERSION == 3
    assert BYO_LEGACY_RUNTIME_DISPOSITIONS == {2: "dispatch_mediated_only"}
    assert BYO_BUNDLE_FILENAMES == (
        "agent_main.py",
        "astralprims_ui.py",
        "protected_executor.py",
        "mcp_tools.py",
    )
    assert executor.LETS_RELEASE == "v1.0.11"
    assert byo["protected_executor.py"] == backend["protected_executor.py"]
    assert "from lets.executor import" in byo["protected_executor.py"]
    assert "from lets.executor_authority import" in byo["protected_executor.py"]


def _signed_manifest():
    resources = tuple(
        ResourceDimension(name, "count")
        for name in ("read", "write", "search", "system", "files", "execute")
    )
    transitions = tuple(
        TransitionSpec(
            name=transition,
            source="ready",
            target="ready",
            cost=tuple(1 if index == dimension else 0 for index in range(6)),
            capability=capability,
        )
        for _scope, capability, transition, dimension in executor._SCOPE_BINDINGS
    )
    policy = PolicySpec(
        policy_id="astral-tools",
        policy_version="v1",
        dimensions=resources,
        machine=MachineSpec(
            machine_id="astral-tool-effects",
            initial_state="ready",
            transitions=transitions,
        ),
        max_lease_ttl_ns=60_000_000_000,
        receipt_ttl_ns=30_000_000_000,
        max_clock_uncertainty_ns=1_000_000_000,
        transfer_gap_window=64,
    )
    warden = Ed25519Signer.generate("warden-a")
    operator = Ed25519Signer.generate("operator-a")
    manifest = ClusterManifest(
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        config_epoch=1,
        created_at="2026-08-14T00:00:00Z",
        resources=resources,
        initial_budget=(100, 100, 100, 100, 100, 100),
        wardens=(
            WardenManifest(
                warden_id=warden.warden_id,
                peer_endpoint="https://warden-a.example",
                client_endpoint="https://warden-a.example",
                initial_share=(100, 100, 100, 100, 100, 100),
                keys=(
                    ManifestPublicKey(
                        warden.key_id,
                        warden.public_key_bytes,
                    ),
                ),
                extensions={},
            ),
        ),
        policies=(policy,),
        extensions={},
    )
    manifest = replace(
        manifest,
        signatures=(
            ManifestSignature(
                operator.key_id,
                operator.sign(canonical_json(manifest.unsigned_dict())),
            ),
        ),
    )
    return manifest, policy, warden, operator


def _environment(tmp_path: Path):
    manifest, policy, warden, operator = _signed_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), sort_keys=True), encoding="utf-8"
    )
    operator_path = tmp_path / "operators.json"
    operator_path.write_text(
        json.dumps(
            {
                "api_version": executor.OPERATOR_TRUST_TYPE,
                "threshold": 1,
                "keys": [
                    {
                        "key_id": operator.key_id,
                        "algorithm": "Ed25519",
                        "public_key": b64url_encode(operator.public_key_bytes),
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    database = tmp_path / "replay"
    authority = tmp_path / "authority"
    database.mkdir()
    authority.mkdir()
    values = {
        "ASTRAL_ENV": "production",
        "FF_LETS_EXTERNAL_WARDEN": "1",
        "LETS_MODE": "enforce",
        "LETS_SIGNED_TRUST_MANIFEST": str(manifest_path),
        "LETS_MANIFEST_OPERATOR_KEYS_FILE": str(operator_path),
        "LETS_WARDEN_ID": warden.warden_id,
        "LETS_TENANT_ID": manifest.tenant_id,
        "LETS_ENVELOPE_ID": manifest.envelope_id,
        "LETS_POLICY_DIGEST": policy.digest,
        "LETS_MACHINE_DIGEST": policy.machine.digest,
        "LETS_EXECUTOR_INSTANCE_ID": "generated-executor-a",
        "LETS_EXECUTOR_DB_ROOT": str(database),
        "LETS_EXECUTOR_AUTHORITY_ROOT": str(authority),
        "ASTRAL_AUTHORITY_OWNER_ID": "owner-a",
        "ASTRAL_AUTHORITY_BINDING_ID": "binding-a",
        "ASTRAL_AUTHORITY_LEASE_ID": "lease-a",
        "ASTRAL_AUTHORITY_LINEAGE_ID": "lineage-a",
        "ASTRAL_RUNTIME_ID": "runtime-a",
        "ASTRAL_RUNTIME_GENERATION": "1",
    }
    return values, manifest, policy, warden


def _permit(manifest, policy, warden, *, arguments):
    nonce = "ab" * 16
    context = {
        "type": executor.EVIDENCE_TYPE,
        "operation_id": "operation-a",
        "agent_id": AGENT_ID,
        "runtime_id": "runtime-a",
        "tool_id": "write_value",
        "scope": "tools:write",
        "capability": "astral.tools.write",
        "transition": "tool_write",
        "resource_dimension": 1,
        "executor_audience": "generated-executor-a",
        "channel": "a2a",
        "audit_correlation_id": "audit-a",
        "scope_profile_sha256": executor.SCOPE_PROFILE_SHA256,
        "authorized_effect_sha256": "1" * 64,
        "effect_sha256": "",
    }
    context["effect_sha256"] = executor._recompute_effect_sha256(
        context, expected_sequence=0, nonce=nonce
    )
    now = time.time_ns()
    receipt = Receipt(
        tenant_id=manifest.tenant_id,
        envelope_id=manifest.envelope_id,
        config_epoch=manifest.config_epoch,
        receipt_id="receipt-a",
        request_id="operation-a",
        warden_id=warden.warden_id,
        key_id=warden.key_id,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_digest=policy.digest,
        machine_digest=policy.machine.digest,
        lease_id="lease-a",
        lineage_id="lineage-a",
        subject_id=AGENT_ID,
        executor_audience="generated-executor-a",
        transition="tool_write",
        source_state="ready",
        target_state="ready",
        cost=(0, 1, 0, 0, 0, 0),
        resulting_sequence=1,
        evidence_digest=canonical_digest(context),
        nonce=nonce,
        issued_at_ns=now - 1_000_000_000,
        expires_at_ns=now + 30_000_000_000,
    )
    receipt = replace(
        receipt,
        signature=b64url_encode(
            warden.sign(canonical_json(receipt.unsigned_payload()))
        ),
    )
    return {
        "type": executor.PERMIT_TYPE,
        "binding_id": "binding-a",
        "owner_id": "owner-a",
        "runtime_generation": 1,
        "context": context,
        "expected_sequence": 0,
        "nonce": nonce,
        "wire_arguments_sha256": hashlib.sha256(
            executor._stable_canonical_bytes(arguments)
        ).hexdigest(),
        "receipt": receipt.to_dict(),
    }


def test_public_verifier_persists_replay_state_with_external_anchor(tmp_path):
    values, manifest, policy, warden = _environment(tmp_path)
    arguments = {"value": "exact"}
    permit = _permit(manifest, policy, warden, arguments=arguments)
    runtime = executor.load_protected_executor(values, agent_id=AGENT_ID)

    runtime.verify_and_claim(
        metadata=permit,
        final_arguments=arguments,
        tool_id="write_value",
        tool_scope="tools:write",
    )
    runtime.close()

    reopened = executor.load_protected_executor(values, agent_id=AGENT_ID)
    with pytest.raises(executor.ProtectedExecutorError, match="^receipt_replayed$"):
        reopened.verify_and_claim(
            metadata=permit,
            final_arguments=arguments,
            tool_id="write_value",
            tool_scope="tools:write",
        )
    reopened.close()
    assert list((tmp_path / "replay").glob("*.sqlite3"))
    assert list((tmp_path / "authority").glob("*.anchor"))


def test_signed_manifest_tamper_fails_closed(tmp_path):
    values, _manifest, _policy, _warden = _environment(tmp_path)
    path = Path(values["LETS_SIGNED_TRUST_MANIFEST"])
    document = json.loads(path.read_text(encoding="utf-8"))
    document["tenant_id"] = "tenant-tampered"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        executor.ProtectedExecutorConfigurationError,
        match="^trust_manifest_authentication_failed$",
    ):
        executor.load_protected_executor(values, agent_id=AGENT_ID)


@pytest.mark.parametrize(
    ("mutation", "final_arguments", "code"),
    [
        (
            lambda permit: permit.update(owner_id="wrong-owner"),
            {"value": "exact"},
            "executor_host_binding_mismatch",
        ),
        (
            lambda permit: permit["receipt"].update(lease_id="wrong-lease"),
            {"value": "exact"},
            "executor_host_binding_mismatch",
        ),
        (
            lambda permit: permit["receipt"].update(lineage_id="wrong-lineage"),
            {"value": "exact"},
            "executor_host_binding_mismatch",
        ),
        (lambda _permit: None, {"value": "mutated"}, "executor_arguments_mutated"),
        (
            lambda permit: permit["context"].update(tool_id="wrong-tool"),
            {"value": "exact"},
            "executor_host_binding_mismatch",
        ),
        (
            lambda permit: permit["context"].update(authorized_effect_sha256="2" * 64),
            {"value": "exact"},
            "executor_evidence_mismatch",
        ),
    ],
)
def test_exact_host_effect_argument_and_context_checks_fail_closed(
    tmp_path, mutation, final_arguments, code
):
    values, manifest, policy, warden = _environment(tmp_path)
    permit = _permit(manifest, policy, warden, arguments={"value": "exact"})
    mutation(permit)
    runtime = executor.load_protected_executor(values, agent_id=AGENT_ID)
    with pytest.raises(executor.ProtectedExecutorError, match=f"^{code}$"):
        runtime.verify_and_claim(
            metadata=permit,
            final_arguments=final_arguments,
            tool_id="write_value",
            tool_scope="tools:write",
        )
    runtime.close()


@pytest.mark.parametrize(
    "name",
    ["ASTRAL_AUTHORITY_LEASE_ID", "ASTRAL_AUTHORITY_LINEAGE_ID"],
)
def test_enforce_requires_host_owned_lease_and_lineage(tmp_path, name):
    values, _manifest, _policy, _warden = _environment(tmp_path)
    values.pop(name)

    with pytest.raises(
        executor.ProtectedExecutorConfigurationError,
        match=f"^missing_{name.lower()}$",
    ):
        executor.load_protected_executor(values, agent_id=AGENT_ID)


def test_receipt_signature_tamper_fails_closed(tmp_path):
    values, manifest, policy, warden = _environment(tmp_path)
    permit = _permit(manifest, policy, warden, arguments={"value": "exact"})
    signature = permit["receipt"]["signature"]
    permit["receipt"]["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    runtime = executor.load_protected_executor(values, agent_id=AGENT_ID)
    with pytest.raises(
        executor.ProtectedExecutorError, match="^receipt_signature_invalid$"
    ):
        runtime.verify_and_claim(
            metadata=permit,
            final_arguments={"value": "exact"},
            tool_id="write_value",
            tool_scope="tools:write",
        )
    runtime.close()


@pytest.mark.parametrize(
    "exception_factory",
    [
        lambda: StorageError("store unavailable"),
        lambda: AuthorityAnchorTransportError(
            "anchor unavailable",
            reason="helper_pipe",
            operation="read",
            request_flushed=False,
            mutation_uncertain=False,
            helper_pid=None,
            helper_exit_code=None,
        ),
    ],
)
def test_store_and_anchor_claim_failures_are_retryable_denials(
    tmp_path, exception_factory
):
    values, manifest, policy, warden = _environment(tmp_path)
    arguments = {"value": "exact"}
    permit = _permit(manifest, policy, warden, arguments=arguments)
    runtime = executor.load_protected_executor(values, agent_id=AGENT_ID)

    class FailingVerifier:
        def verify_and_claim(self, _receipt):
            raise exception_factory()

    runtime.verifier = FailingVerifier()
    with pytest.raises(executor.ProtectedExecutorError) as raised:
        runtime.verify_and_claim(
            metadata=permit,
            final_arguments=arguments,
            tool_id="write_value",
            tool_scope="tools:write",
        )
    assert raised.value.code == "replay_store_unavailable"
    assert raised.value.retryable is True
    runtime.close()


@pytest.mark.parametrize(
    "target",
    ["SQLiteReceiptReplayStore", "ProcessFileExecutorAuthorityAnchor"],
)
def test_store_and_anchor_initialization_fail_closed(tmp_path, monkeypatch, target):
    values, _manifest, _policy, _warden = _environment(tmp_path)

    class Broken:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("sensitive infrastructure detail")

        @classmethod
        def initialize(cls, *_args, **_kwargs):
            raise RuntimeError("sensitive infrastructure detail")

    monkeypatch.setattr(executor, target, Broken)
    with pytest.raises(
        executor.ProtectedExecutorConfigurationError,
        match="^executor_initialization_failed$",
    ) as raised:
        executor.load_protected_executor(values, agent_id=AGENT_ID)
    assert "sensitive infrastructure detail" not in str(raised.value)


def test_off_mode_is_state_free_and_preserves_no_permit_parity(tmp_path):
    runtime = executor.load_protected_executor({"LETS_MODE": "off"}, agent_id=AGENT_ID)

    assert runtime.requires_permit is False
    assert runtime.host is None
    assert runtime.replay_store is None
    assert runtime.authority_anchor is None
    assert list(tmp_path.iterdir()) == []


def test_generated_runner_v3_authority_contract_and_legacy_refusal(monkeypatch):
    class ProtectedExecutorError(RuntimeError):
        pass

    class ProtectedExecutorConfigurationError(ProtectedExecutorError):
        pass

    tools_module = types.ModuleType("mcp_tools")
    tools_module.TOOL_REGISTRY = {}
    ui_module = types.ModuleType("astralprims_ui")
    ui_module.normalize_tool_result = lambda value: (value, [], None)
    protected_module = types.ModuleType("protected_executor")
    protected_module.ProtectedExecutorError = ProtectedExecutorError
    protected_module.ProtectedExecutorConfigurationError = (
        ProtectedExecutorConfigurationError
    )
    protected_module.extract_permit = lambda _caps: None
    protected_module.load_protected_executor = lambda *, agent_id: None
    monkeypatch.setitem(sys.modules, "mcp_tools", tools_module)
    monkeypatch.setitem(sys.modules, "astralprims_ui", ui_module)
    monkeypatch.setitem(sys.modules, "protected_executor", protected_module)
    source = AgentCodeGenerator(
        llm_client=object(), llm_model="unused"
    ).generate_byo_scaffold(
        agent_name="Demo", description="description", agent_id=AGENT_ID
    )["agent_main.py"]
    generated_module = types.ModuleType("generated_authority_contract")
    exec(
        compile(source, "generated_authority_contract.py", "exec"),
        generated_module.__dict__,
    )
    fence = {
        "agent_id": AGENT_ID,
        "host_id": "11111111-1111-4111-8111-111111111111",
        "host_session_id": "22222222-2222-4222-8222-222222222222",
        "delivery_id": "33333333-3333-4333-8333-333333333333",
        "revision_id": "44444444-4444-4444-8444-444444444444",
        "runtime_instance_id": "55555555-5555-4555-8555-555555555555",
        "process_id": "66666666-6666-4666-8666-666666666666",
        "lifecycle_generation": 7,
    }
    authority = {
        "owner_id": "owner-a",
        "binding_id": "binding-a",
        "lease_id": "lease-a",
        "lineage_id": "lineage-a",
        "population": "byo_user",
        "executor_audience": "astraldeep.byo-executor/v1:audience",
        "agent_id": AGENT_ID,
        "runtime_instance_id": fence["runtime_instance_id"],
        "lifecycle_generation": 7,
    }
    monkeypatch.setenv("ASTRAL_RUNTIME_FENCE_JSON", json.dumps(fence))
    monkeypatch.setenv("ASTRAL_RUNTIME_CONTRACT_VERSION", "3")
    monkeypatch.setenv("ASTRAL_RUNTIME_BUNDLE_SHA256", "a" * 64)
    monkeypatch.delenv("ASTRAL_RUNTIME_AUTHORITY_JSON", raising=False)

    monkeypatch.setenv("LETS_MODE", "off")
    assert generated_module._runtime_context()["authority"] is None

    monkeypatch.setenv("LETS_MODE", "shadow")
    assert generated_module._runtime_context()["authority"] is None

    monkeypatch.setenv("LETS_MODE", "enforce")
    with pytest.raises(ValueError, match="authority is missing"):
        generated_module._runtime_context()
    monkeypatch.setenv("ASTRAL_RUNTIME_AUTHORITY_JSON", json.dumps(authority))
    assert generated_module._runtime_context()["authority"] == authority
    for missing in ("lease_id", "lineage_id"):
        incomplete = dict(authority)
        incomplete.pop(missing)
        monkeypatch.setenv(
            "ASTRAL_RUNTIME_AUTHORITY_JSON",
            json.dumps(incomplete),
        )
        with pytest.raises(ValueError, match="runtime authority is invalid"):
            generated_module._runtime_context()
    monkeypatch.setenv("ASTRAL_RUNTIME_AUTHORITY_JSON", json.dumps(authority))

    monkeypatch.setenv("ASTRAL_RUNTIME_CONTRACT_VERSION", "2")
    with pytest.raises(ValueError, match="contract version is unsupported"):
        generated_module._runtime_context()


@pytest.mark.asyncio
async def test_generated_server_claims_exactly_once_at_physical_actuator(
    monkeypatch,
):
    events: list[object] = []
    observed_arguments: list[dict] = []

    def tool(*, value):
        events.append("actuator")
        observed_arguments.append({"value": value})
        return {"ok": True}

    class ProtectedExecutorError(RuntimeError):
        def __init__(self, code, *, retryable=False):
            self.code = code
            self.retryable = retryable
            super().__init__(code)

    class Runtime:
        requires_permit = True

        def verify_and_claim(self, **kwargs):
            assert kwargs["metadata"] == {"permit": "outside-arguments"}
            assert kwargs["final_arguments"] == {
                "value": "exact",
                "filtered_transport_metadata": "still receipt-bound",
            }
            events.append("claim")

    runtime = Runtime()
    tools_module = types.ModuleType("agents.demo.mcp_tools")
    tools_module.TOOL_REGISTRY = {
        "write_value": {
            "description": "write",
            "scope": "tools:write",
            "function": tool,
        }
    }
    protected_module = types.ModuleType("agents.demo.protected_executor")
    protected_module.ProtectedExecutorError = ProtectedExecutorError
    protected_module.extract_permit = lambda caps: (caps or {}).get(
        executor.LETS_CALLER_CAPABILITY
    )
    protected_module.load_protected_executor = lambda *, agent_id: runtime
    package = types.ModuleType("agents.demo")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "agents.demo", package)
    monkeypatch.setitem(sys.modules, "agents.demo.mcp_tools", tools_module)
    monkeypatch.setitem(
        sys.modules, "agents.demo.protected_executor", protected_module
    )
    source = AgentCodeGenerator(
        llm_client=object(), llm_model="unused"
    ).generate_template_files(
        "Demo", "description", "demo", agent_id=AGENT_ID
    )["mcp_server.py"]
    generated_module = types.ModuleType("generated_demo_mcp_server")
    generated_module.__file__ = "generated_demo_mcp_server.py"
    exec(compile(source, "generated_demo_mcp_server.py", "exec"), generated_module.__dict__)
    server = generated_module.MCPServer()

    agent = BaseA2AAgent.__new__(BaseA2AAgent)
    agent.agent_id = AGENT_ID
    agent.card = SimpleNamespace(version="1.0.0")
    agent.mcp_server = server
    agent._logger = MagicMock()
    agent._decrypt_credentials_if_needed = MagicMock()
    agent._stream_wrapper_tasks = set()
    agent._verify_and_claim_protected_request = MagicMock(
        side_effect=lambda *_args, **_kwargs: events.append("early_claim")
    )
    websocket = SimpleNamespace(send_text=AsyncMock())
    monkeypatch.setattr("shared.base_agent.flags.is_enabled", lambda _name: False)
    request = MCPRequest(
        request_id="request-1",
        method="tools/call",
        params={
            "name": "write_value",
            "arguments": {
                "value": "exact",
                "filtered_transport_metadata": "still receipt-bound",
            },
        },
        protocol_version=MCP_PROTOCOL_VERSION,
        caller_capabilities={
            executor.LETS_CALLER_CAPABILITY: {"permit": "outside-arguments"}
        },
        caller_info={"name": "test", "version": "1"},
    )

    await agent.handle_mcp_request(websocket, request)

    assert events == ["claim", "actuator"]
    assert observed_arguments == [{"value": "exact"}]
    agent._verify_and_claim_protected_request.assert_not_called()
    assert executor.LETS_CALLER_CAPABILITY not in request.params["arguments"]
    assert "owner_id" not in request.params["arguments"]
    assert "binding_id" not in request.params["arguments"]


def test_generated_byo_dispatch_checks_full_wire_arguments_before_actuation(
    monkeypatch,
):
    events: list[str] = []
    received: list[dict] = []

    def tool(*, value):
        events.append("actuator")
        received.append({"value": value})
        return {"ok": True}

    class ProtectedExecutorError(RuntimeError):
        def __init__(self, code, *, retryable=False):
            self.code = code
            self.retryable = retryable
            super().__init__(code)

    class ProtectedExecutorConfigurationError(ProtectedExecutorError):
        pass

    class Runtime:
        requires_permit = True

        def verify_and_claim(self, **kwargs):
            assert kwargs["final_arguments"] == {
                "value": "exact",
                "filtered_transport_metadata": "receipt-bound",
            }
            events.append("claim")

        def card_metadata(self):
            return {"mode": "enforce"}

    tools_module = types.ModuleType("mcp_tools")
    tools_module.TOOL_REGISTRY = {
        "write_value": {"scope": "tools:write", "function": tool}
    }
    ui_module = types.ModuleType("astralprims_ui")
    ui_module.normalize_tool_result = lambda value: (value, [], None)
    protected_module = types.ModuleType("protected_executor")
    protected_module.ProtectedExecutorError = ProtectedExecutorError
    protected_module.ProtectedExecutorConfigurationError = (
        ProtectedExecutorConfigurationError
    )
    protected_module.extract_permit = lambda caps: (caps or {}).get(
        executor.LETS_CALLER_CAPABILITY
    )
    protected_module.load_protected_executor = lambda *, agent_id: Runtime()
    monkeypatch.setitem(sys.modules, "mcp_tools", tools_module)
    monkeypatch.setitem(sys.modules, "astralprims_ui", ui_module)
    monkeypatch.setitem(sys.modules, "protected_executor", protected_module)
    source = AgentCodeGenerator(
        llm_client=object(), llm_model="unused"
    ).generate_byo_scaffold(
        agent_name="Demo", description="description", agent_id=AGENT_ID
    )["agent_main.py"]
    generated_module = types.ModuleType("generated_byo_agent")
    exec(compile(source, "generated_byo_agent.py", "exec"), generated_module.__dict__)

    response = generated_module.dispatch(
        {
            "type": "mcp_request",
            "request_id": "request-a",
            "method": "tools/call",
            "params": {
                "name": "write_value",
                "arguments": {
                    "value": "exact",
                    "filtered_transport_metadata": "receipt-bound",
                },
            },
            "caller_capabilities": {
                executor.LETS_CALLER_CAPABILITY: {"permit": "outside"}
            },
        },
        Runtime(),
    )

    assert "error" not in response
    assert events == ["claim", "actuator"]
    assert received == [{"value": "exact"}]

    class ShadowRuntime:
        requires_permit = False

        def verify_and_claim(self, **_kwargs):
            raise AssertionError("shadow must not claim or block")

    events.clear()
    received.clear()
    shadow_response = generated_module.dispatch(
        {
            "type": "mcp_request",
            "request_id": "request-shadow",
            "method": "tools/call",
            "params": {"name": "write_value", "arguments": {"value": "exact"}},
            "caller_capabilities": {
                executor.LETS_CALLER_CAPABILITY: {"would_deny": True}
            },
        },
        ShadowRuntime(),
    )
    assert "error" not in shadow_response
    assert events == ["actuator"]
