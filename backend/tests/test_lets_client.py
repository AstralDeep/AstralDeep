"""Pinned LETS v1.0.10 public-client boundary and redaction tests."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from lets.client import (
    AuthenticationFailedError,
    LETSClient,
    LETSClientError,
    PermissionDeniedError,
    ProblemDetails,
    RemoteUnavailableError,
    RemoteValidationError,
    RequestConflictError,
    ResourceNotFoundError,
)
from lets.errors import PolicyError, ValidationError
from lets.models import (
    BranchRevocation,
    LeaseGrant,
    LeaseSnapshot,
    LeaseStatus,
    Receipt,
)

from orchestrator.lets_client import (
    MAX_LETS_RESPONSE_BYTES,
    MAX_SERVICE_TOKEN_BYTES,
    LetsClientBoundaryError,
    create_lets_warden_client,
)
from orchestrator.lets_config import (
    AuthenticatedTrustManifest,
    LetsHostConfig,
    SecretFileReference,
)

POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64
DEFAULT_ALLOCATION = (2, 2, 2, 2, 2, 2)
SYNTHETIC_TOKEN = "synthetic-test-value"


def _reference(name: str, *, size: int = 1) -> SecretFileReference:
    return SecretFileReference(path=Path(f"C:/synthetic/{name}"), size_bytes=size)


def _config(**changes: object) -> LetsHostConfig:
    manifest = AuthenticatedTrustManifest(
        path=Path("C:/synthetic/trust-manifest.json"),
        sha256="3" * 64,
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        config_epoch=7,
        warden_id="warden-a",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        max_lease_ttl_ns=120_000_000_000,
    )
    values: dict[str, object] = {
        "master_enabled": True,
        "mode": "enforce",
        "environment": "production",
        "governed_cohorts": ("server_dynamic", "byo_user"),
        "governed_agent_allowlist": (),
        "warden_origin": "https://warden.example",
        "service_token_file": _reference("service-token"),
        "tenant_id": "tenant-a",
        "envelope_id": "envelope-a",
        "policy_digest": POLICY_DIGEST,
        "machine_digest": MACHINE_DIGEST,
        "default_allocation": DEFAULT_ALLOCATION,
        "default_ttl_seconds": 60,
        "request_timeout_seconds": 2.5,
        "request_attempts": 2,
        "trust_manifest": manifest,
    }
    values.update(changes)
    return LetsHostConfig(**values)  # type: ignore[arg-type]


def _grant(
    *,
    agent_id: str = "agent-a",
    lease_id: str = "lease-a",
    parent_id: str | None = None,
    capabilities: frozenset[str] = frozenset({"astral.tools.read"}),
    allocation: tuple[int, ...] = DEFAULT_ALLOCATION,
    tenant_id: str = "tenant-a",
) -> dict[str, Any]:
    return LeaseGrant(
        tenant_id=tenant_id,
        envelope_id="envelope-a",
        config_epoch=7,
        lease_id=lease_id,
        lineage_id="lineage-a",
        parent_id=parent_id,
        subject_id=agent_id,
        warden_id="warden-a",
        allocation=allocation,
        capabilities=capabilities,
        policy_id="astral-policy",
        policy_version="1",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        ancestor_path=() if parent_id is None else (parent_id,),
        branch_epoch=0,
        issued_at_ns=1,
        expires_at_ns=2,
        key_id="warden-a:key-1",
        signature="synthetic-signature",
    ).to_dict()


def _snapshot(
    *,
    agent_id: str = "agent-a",
    lease_id: str = "lease-a",
    status: LeaseStatus = LeaseStatus.ACTIVE,
) -> dict[str, Any]:
    return LeaseSnapshot(
        grant=LeaseGrant.from_dict(_grant(agent_id=agent_id, lease_id=lease_id)),
        residual=DEFAULT_ALLOCATION,
        current_state="ready",
        status=status,
        sequence=1,
        updated_at_ns=2,
    ).to_dict()


def _receipt(
    *,
    request_id: str = "operation-a",
    lease_id: str = "lease-a",
    agent_id: str = "agent-a",
    transition: str = "tool_read",
    audience: str = "gateway-a",
    nonce: str = "nonce-a",
    cost: tuple[int, ...] = (1, 0, 0, 0, 0, 0),
) -> dict[str, Any]:
    return Receipt(
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        config_epoch=7,
        receipt_id="receipt-a",
        request_id=request_id,
        warden_id="warden-a",
        key_id="warden-a:key-1",
        policy_id="astral-policy",
        policy_version="1",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        lease_id=lease_id,
        lineage_id="lineage-a",
        subject_id=agent_id,
        executor_audience=audience,
        transition=transition,
        source_state="ready",
        target_state="ready",
        cost=cost,
        resulting_sequence=1,
        evidence_digest=None,
        nonce=nonce,
        issued_at_ns=1,
        expires_at_ns=2,
        signature="synthetic-signature",
    ).to_dict()


def _revocation(*, reason: str = "compromised") -> dict[str, Any]:
    return BranchRevocation(
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        config_epoch=7,
        branch_lease_id="lease-a",
        lineage_id="lineage-a",
        epoch=1,
        issuer_warden="warden-a",
        issued_at_ns=1,
        reason=reason,
        key_id="warden-a:key-1",
        signature="synthetic-signature",
    ).to_dict()


class StubClient:
    def __init__(self, responses: Mapping[str, object] | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.close_calls = 0
        self.close_error: Exception | None = None

    def _call(self, name: str, *arguments: object) -> Mapping[str, Any]:
        self.calls.append((name, arguments))
        value = self.responses[name]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value(*arguments)
        return value  # type: ignore[return-value]

    def issue_root(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._call("issue_root", payload)

    def spawn(self, parent_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._call("spawn", parent_id, payload)

    def authorize(self, lease_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._call("authorize", lease_id, payload)

    def renew(self, lease_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._call("renew", lease_id, payload)

    def quiesce(self, lease_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._call("quiesce", lease_id, payload)

    def resume(self, lease_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._call("resume", lease_id, payload)

    def close_lease(
        self, lease_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._call("close_lease", lease_id, payload)

    def lease(self, lease_id: str) -> Mapping[str, Any]:
        return self._call("lease", lease_id)

    def revoke_branch(
        self, lease_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._call("revoke_branch", lease_id, payload)

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class CapturingFactory:
    def __init__(self, client: StubClient | Exception) -> None:
        self.client = client
        self.base_url: str | None = None
        self.keywords: dict[str, Any] = {}

    def __call__(self, base_url: str, **keywords: Any) -> StubClient:
        self.base_url = base_url
        self.keywords = keywords
        if isinstance(self.client, Exception):
            raise self.client
        return self.client


def _boundary(
    responses: Mapping[str, object],
    *,
    config: LetsHostConfig | None = None,
) -> tuple[Any, StubClient, CapturingFactory]:
    stub = StubClient(responses)
    factory = CapturingFactory(stub)
    boundary = create_lets_warden_client(
        _config() if config is None else config,
        client_factory=factory,  # type: ignore[arg-type]
        secret_reader=lambda _reference: SYNTHETIC_TOKEN,
    )
    return boundary, stub, factory


def _problem(status: int, detail: str = "synthetic-sensitive-marker") -> ProblemDetails:
    return ProblemDetails(
        type="urn:lets:problem:test",
        title="test",
        status=status,
        detail=detail,
        instance="/synthetic/private/path",
        code="remote-test",
        request_id="operation-a",
    )


def test_factory_settings_and_provision_contract_are_exact() -> None:
    ca_bundle = _reference("ca.pem")
    client_cert_file = _reference("client.pem")
    client_key_file = _reference("client.key")
    config = _config(
        ca_bundle=ca_bundle,
        client_cert_file=client_cert_file,
        client_key_file=client_key_file,
    )
    boundary, stub, factory = _boundary({"issue_root": _grant()}, config=config)

    assert factory.base_url == "https://warden.example"
    assert factory.keywords == {
        "token": SYNTHETIC_TOKEN,
        "verify": str(ca_bundle.path),
        "cert": (str(client_cert_file.path), str(client_key_file.path)),
        "timeout": 2.5,
        "total_timeout_s": 2.5,
        "max_response_bytes": MAX_LETS_RESPONSE_BYTES,
        "retry": factory.keywords["retry"],
    }
    assert factory.keywords["retry"].max_attempts == 2

    with boundary as entered:
        assert entered is boundary
        grant = boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
        )

    assert grant.lease_id == "lease-a"
    assert stub.calls == [
        (
            "issue_root",
            (
                {
                    "request_id": "operation-a",
                    "tenant_id": "tenant-a",
                    "envelope_id": "envelope-a",
                    "subject_id": "agent-a",
                    "allocation": list(DEFAULT_ALLOCATION),
                    "capabilities": ["astral.tools.read"],
                    "policy_digest": POLICY_DIGEST,
                    "ttl_ns": 60_000_000_000,
                },
            ),
        )
    ]
    assert stub.close_calls == 1
    boundary.close()
    assert stub.close_calls == 1
    with pytest.raises(LetsClientBoundaryError, match="^client_closed$"):
        boundary.__enter__()


def test_factory_uses_system_trust_without_mtls() -> None:
    boundary, _stub, factory = _boundary({"issue_root": _grant()})

    assert factory.keywords["verify"] is True
    assert factory.keywords["cert"] is None
    boundary.close()


def test_replicate_uses_explicit_and_default_limits() -> None:
    explicit = (3, 2, 1, 0, 0, 0)
    child = _grant(
        agent_id="agent-b",
        lease_id="lease-b",
        parent_id="lease-parent",
        capabilities=frozenset({"astral.tools.read", "astral.tools.write"}),
        allocation=explicit,
    )
    boundary, stub, _factory = _boundary({"spawn": child})

    grant = boundary.replicate_agent(
        operation_id="operation-b",
        parent_lease_id="lease-parent",
        agent_id="agent-b",
        declared_scopes=("tools:write", "tools:read"),
        allocation=explicit,
        ttl_ns=5_000_000_000,
        expected_sequence=4,
    )

    assert grant.lease_id == "lease-b"
    assert stub.calls[-1] == (
        "spawn",
        (
            "lease-parent",
            {
                "request_id": "operation-b",
                "subject_id": "agent-b",
                "allocation": list(explicit),
                "capabilities": ["astral.tools.read", "astral.tools.write"],
                "ttl_ns": 5_000_000_000,
                "policy_digest": POLICY_DIGEST,
                "expected_sequence": 4,
            },
        ),
    )

    default_child = _grant(
        agent_id="agent-b",
        lease_id="lease-c",
        parent_id="lease-parent",
    )
    stub.responses["spawn"] = default_child
    default_grant = boundary.replicate_agent(
        operation_id="operation-c",
        parent_lease_id="lease-parent",
        agent_id="agent-b",
        declared_scopes=("tools:read",),
    )
    assert default_grant.allocation == DEFAULT_ALLOCATION
    assert stub.calls[-1][1][1]["ttl_ns"] == 60_000_000_000  # type: ignore[index]
    boundary.close()


def test_authorize_tool_maps_scope_and_correlates_receipt() -> None:
    boundary, stub, _factory = _boundary({"authorize": _receipt()})

    receipt = boundary.authorize_tool(
        operation_id="operation-a",
        lease_id="lease-a",
        agent_id="agent-a",
        declared_scope="tools:read",
        executor_audience="gateway-a",
        nonce="nonce-a",
        evidence={"effect_digest": "sha256:" + "4" * 64},
        expected_state="ready",
        expected_sequence=0,
    )

    assert receipt.receipt_id == "receipt-a"
    assert stub.calls == [
        (
            "authorize",
            (
                "lease-a",
                {
                    "request_id": "operation-a",
                    "transition": "tool_read",
                    "executor_audience": "gateway-a",
                    "nonce": "nonce-a",
                    "evidence": {"effect_digest": "sha256:" + "4" * 64},
                    "expected_state": "ready",
                    "expected_sequence": 0,
                },
            ),
        )
    ]
    boundary.close()


@pytest.mark.parametrize(
    ("method_name", "client_method", "extra", "expected_arguments"),
    [
        (
            "renew",
            "renew",
            {"ttl_ns": 10, "expected_sequence": 3, "cascade": True},
            (
                "lease-a",
                {
                    "request_id": "operation-a",
                    "ttl_ns": 10,
                    "cascade": True,
                    "expected_sequence": 3,
                },
            ),
        ),
        (
            "renew",
            "renew",
            {},
            (
                "lease-a",
                {
                    "request_id": "operation-a",
                    "ttl_ns": 60_000_000_000,
                    "cascade": False,
                },
            ),
        ),
        (
            "quiesce",
            "quiesce",
            {},
            ("lease-a", {"request_id": "operation-a"}),
        ),
        (
            "resume",
            "resume",
            {},
            ("lease-a", {"request_id": "operation-a"}),
        ),
        (
            "close_lease",
            "close_lease",
            {},
            ("lease-a", {"request_id": "operation-a"}),
        ),
        ("reconcile", "lease", {}, ("lease-a",)),
    ],
)
def test_lifecycle_methods_use_public_contracts(
    method_name: str,
    client_method: str,
    extra: dict[str, object],
    expected_arguments: tuple[object, ...],
) -> None:
    boundary, stub, _factory = _boundary({client_method: _snapshot()})
    arguments: dict[str, object] = {"lease_id": "lease-a", "agent_id": "agent-a"}
    if method_name != "reconcile":
        arguments["operation_id"] = "operation-a"
    arguments.update(extra)

    snapshot = getattr(boundary, method_name)(**arguments)

    assert snapshot.grant.lease_id == "lease-a"
    assert stub.calls == [(client_method, expected_arguments)]
    boundary.close()


def test_revoke_uses_public_contract_and_correlates_reason() -> None:
    boundary, stub, _factory = _boundary({"revoke_branch": _revocation()})

    revocation = boundary.revoke(
        operation_id="operation-a",
        lease_id="lease-a",
        reason="compromised",
    )

    assert revocation.reason == "compromised"
    assert stub.calls == [
        (
            "revoke_branch",
            (
                "lease-a",
                {"request_id": "operation-a", "reason": "compromised"},
            ),
        )
    ]
    boundary.close()


@pytest.mark.parametrize(
    "response",
    [
        {**_receipt(), "unexpected": "field"},
        {**_receipt(), "type": "lets.receipt/v2"},
        {"type": "lets.receipt/v1"},
    ],
)
def test_success_responses_are_parsed_strictly(response: Mapping[str, Any]) -> None:
    boundary, _stub, _factory = _boundary({"authorize": response})

    with pytest.raises(LetsClientBoundaryError, match="^invalid_response$"):
        boundary.authorize_tool(
            operation_id="operation-a",
            lease_id="lease-a",
            agent_id="agent-a",
            declared_scope="tools:read",
            executor_audience="gateway-a",
            nonce="nonce-a",
        )
    boundary.close()


@pytest.mark.parametrize(
    "response",
    [
        _receipt(request_id="operation-other"),
        _receipt(lease_id="lease-other"),
        _receipt(agent_id="agent-other"),
        _receipt(transition="tool_write"),
        _receipt(audience="gateway-other"),
        _receipt(nonce="nonce-other"),
        _receipt(cost=(0, 1, 0, 0, 0, 0)),
        {**_receipt(), "tenant_id": "tenant-other"},
        {**_receipt(), "envelope_id": "envelope-other"},
        {**_receipt(), "warden_id": "warden-other"},
        {**_receipt(), "policy_digest": "sha256:" + "5" * 64},
        {**_receipt(), "machine_digest": "sha256:" + "6" * 64},
        {**_receipt(), "config_epoch": 8},
    ],
)
def test_receipt_host_binding_mismatch_is_denied(response: Mapping[str, Any]) -> None:
    boundary, _stub, _factory = _boundary({"authorize": response})

    with pytest.raises(LetsClientBoundaryError, match="^response_binding_mismatch$"):
        boundary.authorize_tool(
            operation_id="operation-a",
            lease_id="lease-a",
            agent_id="agent-a",
            declared_scope="tools:read",
            executor_audience="gateway-a",
            nonce="nonce-a",
        )
    boundary.close()


@pytest.mark.parametrize(
    "grant",
    [
        _grant(agent_id="agent-other"),
        _grant(capabilities=frozenset({"astral.tools.write"})),
        _grant(allocation=(1, 2, 2, 2, 2, 2)),
        {**_grant(), "parent_id": "lease-parent", "ancestor_path": ["lease-parent"]},
    ],
)
def test_grant_binding_mismatch_is_denied(grant: Mapping[str, Any]) -> None:
    boundary, _stub, _factory = _boundary({"issue_root": grant})

    with pytest.raises(LetsClientBoundaryError, match="^response_binding_mismatch$"):
        boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
        )
    boundary.close()


def test_lifecycle_and_revocation_binding_mismatch_are_denied() -> None:
    wrong_snapshot = _snapshot(agent_id="agent-other")
    boundary, _stub, _factory = _boundary({"lease": wrong_snapshot})
    with pytest.raises(LetsClientBoundaryError, match="^response_binding_mismatch$"):
        boundary.reconcile(lease_id="lease-a", agent_id="agent-a")
    boundary.close()

    boundary, _stub, _factory = _boundary(
        {"revoke_branch": _revocation(reason="different-reason")}
    )
    with pytest.raises(LetsClientBoundaryError, match="^response_binding_mismatch$"):
        boundary.revoke(
            operation_id="operation-a",
            lease_id="lease-a",
            reason="compromised",
        )
    boundary.close()


def test_invalid_local_requests_are_stably_redacted() -> None:
    boundary, _stub, _factory = _boundary(
        {
            "issue_root": _grant(),
            "spawn": _grant(parent_id="lease-parent"),
            "authorize": _receipt(),
            "revoke_branch": _revocation(),
        }
    )
    operations: tuple[Callable[[], object], ...] = (
        lambda: boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes="tools:read",
        ),
        lambda: boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=(),
        ),
        lambda: boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:unknown",),
        ),
        lambda: boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:read", "tools:read"),
        ),
        lambda: boundary.replicate_agent(
            operation_id="operation-a",
            parent_lease_id="lease-parent",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
            allocation=(1, 2),
        ),
        lambda: boundary.replicate_agent(
            operation_id="operation-a",
            parent_lease_id="lease-parent",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
            ttl_ns=0,
        ),
        lambda: boundary.authorize_tool(
            operation_id="operation-a",
            lease_id="lease-a",
            agent_id="agent-a",
            declared_scope="tools:unknown",
            executor_audience="gateway-a",
            nonce="nonce-a",
        ),
        lambda: boundary.revoke(
            operation_id="operation-a",
            lease_id="lease-a",
            reason="",
        ),
    )

    for operation in operations:
        with pytest.raises(
            LetsClientBoundaryError, match="^invalid_request$"
        ) as raised:
            operation()
        assert "unknown" not in repr(raised.value)
        assert "allocation" not in repr(raised.value)
    boundary.close()


@pytest.mark.parametrize(
    ("remote_error", "code", "status", "retryable"),
    [
        (AuthenticationFailedError(_problem(401)), "authentication_failed", 401, False),
        (PermissionDeniedError(_problem(403)), "permission_denied", 403, False),
        (ResourceNotFoundError(_problem(404)), "not_found", 404, False),
        (RequestConflictError(_problem(409)), "request_conflict", 409, False),
        (RemoteValidationError(_problem(422)), "remote_validation_failed", 422, False),
        (RemoteUnavailableError(_problem(503)), "remote_unavailable", 503, True),
        (LETSClientError(_problem(418)), "remote_failure", 418, False),
        (LETSClientError(_problem(500)), "remote_failure", 500, True),
    ],
)
def test_typed_remote_errors_are_mapped_without_problem_detail(
    remote_error: LETSClientError,
    code: str,
    status: int,
    retryable: bool,
) -> None:
    boundary, _stub, _factory = _boundary({"issue_root": remote_error})

    with pytest.raises(LetsClientBoundaryError, match=f"^{code}$") as raised:
        boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
        )
    assert raised.value.code == code
    assert raised.value.status_code == status
    assert raised.value.retryable is retryable
    assert "synthetic-sensitive-marker" not in repr(raised.value)
    assert "/synthetic/private/path" not in repr(raised.value)
    boundary.close()


@pytest.mark.parametrize(
    ("client_error", "code", "retryable"),
    [
        (httpx.ReadTimeout("synthetic-sensitive-marker"), "request_timeout", True),
        (
            httpx.ConnectError("synthetic-sensitive-marker"),
            "transport_unavailable",
            True,
        ),
        (PolicyError("synthetic-sensitive-marker"), "invalid_request", False),
        (ValidationError("synthetic-sensitive-marker"), "invalid_request", False),
        (TypeError("synthetic-sensitive-marker"), "invalid_request", False),
        (ValueError("synthetic-sensitive-marker"), "invalid_request", False),
        (RuntimeError("synthetic-sensitive-marker"), "client_failure", False),
    ],
)
def test_transport_local_and_unexpected_errors_are_redacted(
    client_error: Exception,
    code: str,
    retryable: bool,
) -> None:
    boundary, _stub, _factory = _boundary({"issue_root": client_error})

    with pytest.raises(LetsClientBoundaryError, match=f"^{code}$") as raised:
        boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
        )
    assert raised.value.retryable is retryable
    assert raised.value.status_code is None
    assert "synthetic-sensitive-marker" not in repr(raised.value)
    boundary.close()


def test_non_mapping_success_and_closed_client_are_denied() -> None:
    boundary, _stub, _factory = _boundary({"issue_root": ["not", "a", "mapping"]})
    with pytest.raises(LetsClientBoundaryError, match="^invalid_response$"):
        boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
        )
    boundary.close()
    with pytest.raises(LetsClientBoundaryError, match="^client_closed$"):
        boundary.provision_agent(
            operation_id="operation-b",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
        )


def test_close_failure_is_stably_redacted_and_not_retried() -> None:
    boundary, stub, _factory = _boundary({"issue_root": _grant()})
    stub.close_error = RuntimeError("synthetic-sensitive-marker")

    with pytest.raises(
        LetsClientBoundaryError, match="^client_close_failed$"
    ) as raised:
        boundary.close()
    assert "synthetic-sensitive-marker" not in repr(raised.value)
    boundary.close()
    assert stub.close_calls == 1


@pytest.mark.parametrize(
    "config",
    [
        _config(mode="off"),
        _config(master_enabled=False),
        _config(warden_origin=None),
    ],
)
def test_inactive_or_incomplete_config_is_rejected(config: LetsHostConfig) -> None:
    with pytest.raises(LetsClientBoundaryError, match="^client_not_configured$"):
        create_lets_warden_client(
            config,
            client_factory=CapturingFactory(StubClient()),  # type: ignore[arg-type]
            secret_reader=lambda _reference: SYNTHETIC_TOKEN,
        )

    with pytest.raises(LetsClientBoundaryError, match="^client_not_configured$"):
        create_lets_warden_client(  # type: ignore[arg-type]
            object(),
            client_factory=CapturingFactory(StubClient()),  # type: ignore[arg-type]
            secret_reader=lambda _reference: SYNTHETIC_TOKEN,
        )


@pytest.mark.parametrize("value", [None, "", "two words", "line\nbreak", 123])
def test_custom_secret_reader_must_return_one_opaque_token(value: object) -> None:
    with pytest.raises(LetsClientBoundaryError, match="^credential_invalid$"):
        create_lets_warden_client(
            _config(),
            client_factory=CapturingFactory(StubClient()),  # type: ignore[arg-type]
            secret_reader=lambda _reference: value,  # type: ignore[return-value]
        )


def test_secret_reader_failures_are_redacted() -> None:
    def boundary_failure(_reference: SecretFileReference) -> str:
        raise LetsClientBoundaryError("credential_invalid")

    def unexpected_failure(_reference: SecretFileReference) -> str:
        raise OSError("synthetic-sensitive-marker")

    with pytest.raises(LetsClientBoundaryError, match="^credential_invalid$"):
        create_lets_warden_client(
            _config(),
            client_factory=CapturingFactory(StubClient()),  # type: ignore[arg-type]
            secret_reader=boundary_failure,
        )
    with pytest.raises(
        LetsClientBoundaryError, match="^credential_unavailable$"
    ) as raised:
        create_lets_warden_client(
            _config(),
            client_factory=CapturingFactory(StubClient()),  # type: ignore[arg-type]
            secret_reader=unexpected_failure,
        )
    assert "synthetic-sensitive-marker" not in repr(raised.value)


def test_default_secret_reader_accepts_bounded_utf8_and_strips_file_newline(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "synthetic-token"
    token_path.write_bytes(b"synthetic-file-value\n")
    factory = CapturingFactory(StubClient())

    boundary = create_lets_warden_client(
        _config(
            service_token_file=SecretFileReference(
                path=token_path,
                size_bytes=token_path.stat().st_size,
            )
        ),
        client_factory=factory,  # type: ignore[arg-type]
    )

    assert factory.keywords["token"] == "synthetic-file-value"
    boundary.close()


@pytest.mark.parametrize("contents", [b"", b"contains\x00nul", b"\xff"])
def test_default_secret_reader_rejects_invalid_synthetic_files(
    tmp_path: Path,
    contents: bytes,
) -> None:
    token_path = tmp_path / "synthetic-token"
    token_path.write_bytes(contents)
    with pytest.raises(LetsClientBoundaryError, match="^credential_invalid$"):
        create_lets_warden_client(
            _config(
                service_token_file=SecretFileReference(
                    path=token_path,
                    size_bytes=max(len(contents), 1),
                )
            ),
            client_factory=CapturingFactory(StubClient()),  # type: ignore[arg-type]
        )


def test_default_secret_reader_rejects_missing_and_oversized_synthetic_files(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-token"
    with pytest.raises(LetsClientBoundaryError, match="^credential_unavailable$"):
        create_lets_warden_client(
            _config(service_token_file=SecretFileReference(path=missing, size_bytes=1)),
            client_factory=CapturingFactory(StubClient()),  # type: ignore[arg-type]
        )

    oversized = tmp_path / "oversized-token"
    oversized.write_bytes(b"x" * (MAX_SERVICE_TOKEN_BYTES + 1))
    with pytest.raises(LetsClientBoundaryError, match="^credential_invalid$"):
        create_lets_warden_client(
            _config(
                service_token_file=SecretFileReference(
                    path=oversized,
                    size_bytes=oversized.stat().st_size,
                )
            ),
            client_factory=CapturingFactory(StubClient()),  # type: ignore[arg-type]
        )


def test_factory_creation_failure_and_post_creation_cleanup_are_redacted() -> None:
    with pytest.raises(
        LetsClientBoundaryError, match="^client_configuration$"
    ) as raised:
        create_lets_warden_client(
            _config(),
            client_factory=CapturingFactory(RuntimeError("synthetic-sensitive-marker")),  # type: ignore[arg-type]
            secret_reader=lambda _reference: SYNTHETIC_TOKEN,
        )
    assert "synthetic-sensitive-marker" not in repr(raised.value)

    stub = StubClient()
    stub.close_error = RuntimeError("synthetic-close-marker")
    with pytest.raises(
        LetsClientBoundaryError, match="^client_configuration$"
    ) as raised:
        create_lets_warden_client(
            _config(default_ttl_seconds=-1),
            client_factory=CapturingFactory(stub),  # type: ignore[arg-type]
            secret_reader=lambda _reference: SYNTHETIC_TOKEN,
        )
    assert stub.close_calls == 1
    assert "synthetic-close-marker" not in repr(raised.value)


def _http_boundary(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: LetsHostConfig | None = None,
) -> tuple[Any, list[LETSClient]]:
    clients: list[LETSClient] = []

    def factory(base_url: str, **keywords: Any) -> LETSClient:
        retry = keywords.pop("retry")
        client = LETSClient(
            base_url,
            **keywords,
            retry=replace(retry, initial_backoff_s=0.0, maximum_backoff_s=0.0),
            transport=httpx.MockTransport(handler),
            sleep=lambda _seconds: None,
        )
        clients.append(client)
        return client

    boundary = create_lets_warden_client(
        _config() if config is None else config,
        client_factory=factory,
        secret_reader=lambda _reference: SYNTHETIC_TOKEN,
    )
    return boundary, clients


def _authorize(boundary: Any) -> Receipt:
    return boundary.authorize_tool(
        operation_id="operation-a",
        lease_id="lease-a",
        agent_id="agent-a",
        declared_scope="tools:read",
        executor_audience="gateway-a",
        nonce="nonce-a",
    )


def test_public_client_does_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307, headers={"location": "https://redirect.invalid/capture"}
        )

    boundary, _clients = _http_boundary(handler)

    with pytest.raises(LetsClientBoundaryError, match="^invalid_response$") as raised:
        boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
        )
    assert raised.value.status_code is None
    assert len(requests) == 1
    assert requests[0].url.host == "warden.example"
    boundary.close()


def test_public_client_enforces_response_size_bound() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_LETS_RESPONSE_BYTES + 1))

    boundary, _clients = _http_boundary(handler)

    with pytest.raises(
        LetsClientBoundaryError,
        match="^remote_validation_failed$",
    ) as raised:
        boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
        )
    assert raised.value.status_code == 502
    boundary.close()


def test_public_client_enforces_total_wall_clock_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        time.sleep(0.05)
        return httpx.Response(200, json=_grant())

    boundary, _clients = _http_boundary(
        handler,
        config=_config(request_timeout_seconds=0.01, request_attempts=1),
    )

    with pytest.raises(LetsClientBoundaryError, match="^request_timeout$") as raised:
        boundary.provision_agent(
            operation_id="operation-a",
            agent_id="agent-a",
            declared_scopes=("tools:read",),
        )
    assert raised.value.retryable is True
    boundary.close()


def test_retry_reuses_byte_identical_request_id_and_semantics() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if len(bodies) == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json=_receipt())

    boundary, _clients = _http_boundary(handler)

    receipt = _authorize(boundary)

    assert receipt.request_id == "operation-a"
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
    assert json.loads(bodies[0])["request_id"] == "operation-a"
    boundary.close()


def test_fingerprint_conflict_is_terminal_and_redacted() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            409,
            json={
                "type": "urn:lets:problem:fingerprint_conflict",
                "title": "conflict",
                "status": 409,
                "detail": "synthetic-sensitive-marker",
                "code": "fingerprint_conflict",
                "request_id": "operation-a",
            },
        )

    boundary, _clients = _http_boundary(handler)

    with pytest.raises(LetsClientBoundaryError, match="^request_conflict$") as raised:
        _authorize(boundary)
    assert raised.value.status_code == 409
    assert raised.value.retryable is False
    assert "synthetic-sensitive-marker" not in repr(raised.value)
    assert len(requests) == 1
    boundary.close()
