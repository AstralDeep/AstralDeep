"""Typed, redacted boundary around the signed LETS v1.0.10 public client."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx
from lets.client import (
    AuthenticationFailedError,
    LETSClient,
    LETSClientError,
    PermissionDeniedError,
    RemoteUnavailableError,
    RemoteValidationError,
    RequestConflictError,
    ResourceNotFoundError,
    RetryPolicy,
)
from lets.errors import PolicyError, ValidationError
from lets.integrations import (
    AstralDeepAuthorizer,
    AstralDeepProfile,
    ReplicaAuthorizer,
    ReplicaProfile,
)
from lets.models import BranchRevocation, LeaseGrant, LeaseSnapshot, Receipt

from orchestrator.lets_config import LetsHostConfig, SecretFileReference
from orchestrator.lets_scope_profile import (
    SCOPE_BINDINGS,
    ScopeBinding,
    binding_for_scope,
    validate_allocation,
)

MAX_SERVICE_TOKEN_BYTES = 16_384
MAX_LETS_RESPONSE_BYTES = 1_048_576

_WireModel = TypeVar("_WireModel", LeaseGrant, LeaseSnapshot, Receipt, BranchRevocation)


class LetsClientBoundaryError(RuntimeError):
    """Stable, value-free LETS boundary failure safe for logs and clients."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LetsClientIdentity:
    """Authenticated deployment identity used for response correlation."""

    tenant_id: str
    envelope_id: str
    warden_id: str
    policy_digest: str
    machine_digest: str
    config_epoch: int


class LetsWardenClient:
    """Astral-owned typed adapter over LETS public client contracts only."""

    def __init__(
        self,
        *,
        client: LETSClient,
        replica_authorizer: ReplicaAuthorizer,
        astral_authorizer: AstralDeepAuthorizer,
        identity: LetsClientIdentity,
        default_allocation: tuple[int, ...],
        default_ttl_ns: int,
    ) -> None:
        self._client = client
        self._replica = replica_authorizer
        self._astral = astral_authorizer
        self._identity = identity
        self._default_allocation = default_allocation
        self._default_ttl_ns = default_ttl_ns
        self._closed = False

    def __enter__(self) -> Self:
        if self._closed:
            raise LetsClientBoundaryError("client_closed")
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._client.close()
            except Exception:
                raise LetsClientBoundaryError("client_close_failed") from None

    def provision_agent(
        self,
        *,
        operation_id: str,
        agent_id: str,
        declared_scopes: Sequence[str],
    ) -> LeaseGrant:
        bindings = _scope_bindings(declared_scopes)
        raw = self._invoke(
            lambda: self._astral.provision_agent(
                operation_id=operation_id,
                agent_id=agent_id,
                declared_scopes=tuple(binding.scope for binding in bindings),
            )
        )
        grant = self._parse(LeaseGrant, raw)
        self._correlate_grant(
            grant,
            agent_id=agent_id,
            parent_lease_id=None,
            bindings=bindings,
            allocation=self._default_allocation,
        )
        return grant

    def replicate_agent(
        self,
        *,
        operation_id: str,
        parent_lease_id: str,
        agent_id: str,
        declared_scopes: Sequence[str],
        allocation: Sequence[int] | None = None,
        ttl_ns: int | None = None,
        expected_sequence: int | None = None,
    ) -> LeaseGrant:
        bindings = _scope_bindings(declared_scopes)
        try:
            selected_allocation = (
                self._default_allocation
                if allocation is None
                else validate_allocation(allocation)
            )
        except (TypeError, ValueError):
            raise LetsClientBoundaryError("invalid_request") from None
        selected_ttl = self._default_ttl_ns if ttl_ns is None else ttl_ns
        raw = self._invoke(
            lambda: self._astral.replicate_agent(
                operation_id=operation_id,
                parent_lease_id=parent_lease_id,
                agent_id=agent_id,
                declared_scopes=tuple(binding.scope for binding in bindings),
                allocation=selected_allocation,
                ttl_ns=selected_ttl,
                expected_sequence=expected_sequence,
            )
        )
        grant = self._parse(LeaseGrant, raw)
        self._correlate_grant(
            grant,
            agent_id=agent_id,
            parent_lease_id=parent_lease_id,
            bindings=bindings,
            allocation=selected_allocation,
        )
        return grant

    def authorize_tool(
        self,
        *,
        operation_id: str,
        lease_id: str,
        agent_id: str,
        declared_scope: str,
        executor_audience: str,
        nonce: str,
        evidence: Mapping[str, Any] | None = None,
        expected_state: str | None = None,
        expected_sequence: int | None = None,
    ) -> Receipt:
        try:
            binding = binding_for_scope(declared_scope)
        except (TypeError, ValueError):
            raise LetsClientBoundaryError("invalid_request") from None
        raw = self._invoke(
            lambda: self._astral.authorize_tool(
                operation_id=operation_id,
                lease_id=lease_id,
                declared_scope=declared_scope,
                executor_audience=executor_audience,
                nonce=nonce,
                evidence=evidence,
                expected_state=expected_state,
                expected_sequence=expected_sequence,
            )
        )
        receipt = self._parse(Receipt, raw)
        self._correlate_common(
            tenant_id=receipt.tenant_id,
            envelope_id=receipt.envelope_id,
            warden_id=receipt.warden_id,
            policy_digest=receipt.policy_digest,
            machine_digest=receipt.machine_digest,
            config_epoch=receipt.config_epoch,
        )
        _require_equal(receipt.request_id, operation_id)
        _require_equal(receipt.lease_id, lease_id)
        _require_equal(receipt.subject_id, agent_id)
        _require_equal(receipt.transition, binding.transition)
        _require_equal(receipt.executor_audience, executor_audience)
        _require_equal(receipt.nonce, nonce)
        _require_equal(tuple(receipt.cost), binding.unit_cost())
        return receipt

    def renew(
        self,
        *,
        operation_id: str,
        lease_id: str,
        agent_id: str,
        ttl_ns: int | None = None,
        expected_sequence: int | None = None,
        cascade: bool = False,
    ) -> LeaseSnapshot:
        raw = self._invoke(
            lambda: self._replica.renew(
                lease_id,
                request_id=operation_id,
                ttl_ns=self._default_ttl_ns if ttl_ns is None else ttl_ns,
                expected_sequence=expected_sequence,
                cascade=cascade,
            )
        )
        return self._snapshot(raw, lease_id=lease_id, agent_id=agent_id)

    def quiesce(
        self,
        *,
        operation_id: str,
        lease_id: str,
        agent_id: str,
    ) -> LeaseSnapshot:
        raw = self._invoke(
            lambda: self._replica.quiesce(lease_id, request_id=operation_id)
        )
        return self._snapshot(raw, lease_id=lease_id, agent_id=agent_id)

    def resume(
        self,
        *,
        operation_id: str,
        lease_id: str,
        agent_id: str,
    ) -> LeaseSnapshot:
        raw = self._invoke(
            lambda: self._replica.resume(lease_id, request_id=operation_id)
        )
        return self._snapshot(raw, lease_id=lease_id, agent_id=agent_id)

    def close_lease(
        self,
        *,
        operation_id: str,
        lease_id: str,
        agent_id: str,
    ) -> LeaseSnapshot:
        raw = self._invoke(
            lambda: self._replica.close(lease_id, request_id=operation_id)
        )
        return self._snapshot(raw, lease_id=lease_id, agent_id=agent_id)

    def reconcile(self, *, lease_id: str, agent_id: str) -> LeaseSnapshot:
        raw = self._invoke(lambda: self._client.lease(lease_id))
        return self._snapshot(raw, lease_id=lease_id, agent_id=agent_id)

    def revoke(
        self,
        *,
        operation_id: str,
        lease_id: str,
        reason: str,
    ) -> BranchRevocation:
        raw = self._invoke(
            lambda: self._replica.revoke(
                lease_id,
                request_id=operation_id,
                reason=reason,
            )
        )
        revocation = self._parse(BranchRevocation, raw)
        _require_equal(revocation.tenant_id, self._identity.tenant_id)
        _require_equal(revocation.envelope_id, self._identity.envelope_id)
        _require_equal(revocation.config_epoch, self._identity.config_epoch)
        _require_equal(revocation.issuer_warden, self._identity.warden_id)
        _require_equal(revocation.branch_lease_id, lease_id)
        _require_equal(revocation.reason, reason)
        return revocation

    def _snapshot(
        self,
        raw: Mapping[str, Any],
        *,
        lease_id: str,
        agent_id: str,
    ) -> LeaseSnapshot:
        snapshot = self._parse(LeaseSnapshot, raw)
        self._correlate_grant_identity(snapshot.grant, agent_id=agent_id)
        _require_equal(snapshot.grant.lease_id, lease_id)
        return snapshot

    def _correlate_grant(
        self,
        grant: LeaseGrant,
        *,
        agent_id: str,
        parent_lease_id: str | None,
        bindings: tuple[ScopeBinding, ...],
        allocation: tuple[int, ...],
    ) -> None:
        self._correlate_grant_identity(grant, agent_id=agent_id)
        _require_equal(grant.parent_id, parent_lease_id)
        _require_equal(
            grant.capabilities,
            frozenset(binding.capability for binding in bindings),
        )
        _require_equal(tuple(grant.allocation), allocation)

    def _correlate_grant_identity(self, grant: LeaseGrant, *, agent_id: str) -> None:
        self._correlate_common(
            tenant_id=grant.tenant_id,
            envelope_id=grant.envelope_id,
            warden_id=grant.warden_id,
            policy_digest=grant.policy_digest,
            machine_digest=grant.machine_digest,
            config_epoch=grant.config_epoch,
        )
        _require_equal(grant.subject_id, agent_id)

    def _correlate_common(
        self,
        *,
        tenant_id: str,
        envelope_id: str,
        warden_id: str,
        policy_digest: str,
        machine_digest: str,
        config_epoch: int,
    ) -> None:
        _require_equal(tenant_id, self._identity.tenant_id)
        _require_equal(envelope_id, self._identity.envelope_id)
        _require_equal(warden_id, self._identity.warden_id)
        _require_equal(policy_digest, self._identity.policy_digest)
        _require_equal(machine_digest, self._identity.machine_digest)
        _require_equal(config_epoch, self._identity.config_epoch)

    def _invoke(self, operation: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any]:
        if self._closed:
            raise LetsClientBoundaryError("client_closed")
        try:
            value = operation()
        except LetsClientBoundaryError:
            raise
        except AuthenticationFailedError as exc:
            raise _remote_error("authentication_failed", exc) from None
        except PermissionDeniedError as exc:
            raise _remote_error("permission_denied", exc) from None
        except ResourceNotFoundError as exc:
            raise _remote_error("not_found", exc) from None
        except RequestConflictError as exc:
            raise _remote_error("request_conflict", exc) from None
        except RemoteValidationError as exc:
            raise _remote_error("remote_validation_failed", exc) from None
        except RemoteUnavailableError as exc:
            raise _remote_error("remote_unavailable", exc, retryable=True) from None
        except httpx.TimeoutException:
            raise LetsClientBoundaryError("request_timeout", retryable=True) from None
        except httpx.TransportError:
            raise LetsClientBoundaryError(
                "transport_unavailable", retryable=True
            ) from None
        except (PolicyError, ValidationError, TypeError, ValueError):
            raise LetsClientBoundaryError("invalid_request") from None
        except LETSClientError as exc:
            raise _remote_error(
                "remote_failure",
                exc,
                retryable=exc.status_code >= 500,
            ) from None
        except Exception:
            raise LetsClientBoundaryError("client_failure") from None
        if not isinstance(value, Mapping):
            raise LetsClientBoundaryError("invalid_response")
        return value

    @staticmethod
    def _parse(model: type[_WireModel], value: Mapping[str, Any]) -> _WireModel:
        try:
            return model.from_dict(dict(value))
        except (KeyError, TypeError, ValueError, ValidationError):
            raise LetsClientBoundaryError("invalid_response") from None


def create_lets_warden_client(
    config: LetsHostConfig,
    *,
    client_factory: Callable[..., LETSClient] = LETSClient,
    secret_reader: Callable[[SecretFileReference], str] | None = None,
) -> LetsWardenClient:
    """Create one hardened client from an already authenticated active config."""

    values = _active_config(config)
    read_secret = _read_service_token if secret_reader is None else secret_reader
    try:
        token = read_secret(values.service_token_file)
    except LetsClientBoundaryError:
        raise
    except Exception:
        raise LetsClientBoundaryError("credential_unavailable") from None
    if (
        not isinstance(token, str)
        or not token
        or any(character.isspace() for character in token)
    ):
        raise LetsClientBoundaryError("credential_invalid")

    verify: bool | str = (
        True if values.ca_bundle is None else str(values.ca_bundle.path)
    )
    certificate: tuple[str, str] | None = None
    if values.client_cert_file is not None and values.client_key_file is not None:
        certificate = (
            str(values.client_cert_file.path),
            str(values.client_key_file.path),
        )
    try:
        client = client_factory(
            values.warden_origin,
            token=token,
            verify=verify,
            cert=certificate,
            timeout=values.request_timeout_seconds,
            total_timeout_s=values.request_timeout_seconds,
            max_response_bytes=MAX_LETS_RESPONSE_BYTES,
            retry=RetryPolicy(max_attempts=values.request_attempts),
        )
        replica = ReplicaAuthorizer(
            client,
            ReplicaProfile(
                tenant_id=values.tenant_id,
                envelope_id=values.envelope_id,
                policy_digest=values.policy_digest,
                default_allocation=values.default_allocation,
                default_capabilities=frozenset(
                    binding.capability for binding in SCOPE_BINDINGS
                ),
                default_ttl_ns=values.default_ttl_seconds * 1_000_000_000,
            ),
        )
        astral = AstralDeepAuthorizer(
            replica,
            AstralDeepProfile(
                scope_capabilities={
                    binding.scope: binding.capability for binding in SCOPE_BINDINGS
                },
                scope_transitions={
                    binding.scope: binding.transition for binding in SCOPE_BINDINGS
                },
            ),
        )
    except LetsClientBoundaryError:
        raise
    except Exception:
        if "client" in locals():
            try:
                client.close()
            except Exception:
                pass
        raise LetsClientBoundaryError("client_configuration") from None

    return LetsWardenClient(
        client=client,
        replica_authorizer=replica,
        astral_authorizer=astral,
        identity=LetsClientIdentity(
            tenant_id=values.tenant_id,
            envelope_id=values.envelope_id,
            warden_id=values.trust_manifest.warden_id,
            policy_digest=values.policy_digest,
            machine_digest=values.machine_digest,
            config_epoch=values.trust_manifest.config_epoch,
        ),
        default_allocation=values.default_allocation,
        default_ttl_ns=values.default_ttl_seconds * 1_000_000_000,
    )


def _active_config(config: LetsHostConfig) -> LetsHostConfig:
    if not isinstance(config, LetsHostConfig) or config.mode == "off":
        raise LetsClientBoundaryError("client_not_configured")
    required = (
        config.master_enabled,
        config.warden_origin,
        config.service_token_file,
        config.tenant_id,
        config.envelope_id,
        config.policy_digest,
        config.machine_digest,
        config.default_allocation,
        config.default_ttl_seconds,
        config.request_timeout_seconds,
        config.request_attempts,
        config.trust_manifest,
    )
    if any(value is None or value is False for value in required):
        raise LetsClientBoundaryError("client_not_configured")
    return config


def _read_service_token(reference: SecretFileReference) -> str:
    try:
        with Path(reference.path).open("rb") as stream:
            raw = stream.read(MAX_SERVICE_TOKEN_BYTES + 1)
    except OSError:
        raise LetsClientBoundaryError("credential_unavailable") from None
    if not raw or len(raw) > MAX_SERVICE_TOKEN_BYTES or b"\x00" in raw:
        raise LetsClientBoundaryError("credential_invalid")
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise LetsClientBoundaryError("credential_invalid") from None


def _scope_bindings(scopes: Sequence[str]) -> tuple[ScopeBinding, ...]:
    if (
        isinstance(scopes, (str, bytes))
        or not isinstance(scopes, Sequence)
        or not scopes
    ):
        raise LetsClientBoundaryError("invalid_request")
    try:
        bindings = tuple(binding_for_scope(scope) for scope in scopes)
    except (TypeError, ValueError):
        raise LetsClientBoundaryError("invalid_request") from None
    if len({binding.scope for binding in bindings}) != len(bindings):
        raise LetsClientBoundaryError("invalid_request")
    return bindings


def _require_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise LetsClientBoundaryError("response_binding_mismatch")


def _remote_error(
    code: str,
    error: LETSClientError,
    *,
    retryable: bool = False,
) -> LetsClientBoundaryError:
    return LetsClientBoundaryError(
        code,
        retryable=retryable,
        status_code=error.status_code,
    )


__all__ = (
    "MAX_LETS_RESPONSE_BYTES",
    "MAX_SERVICE_TOKEN_BYTES",
    "LetsClientBoundaryError",
    "LetsClientIdentity",
    "LetsWardenClient",
    "create_lets_warden_client",
)
