"""Typed, redacted LETS health and user-facing denial projection.

This module deliberately has no network or persistence behavior.  It combines
the already-validated host configuration posture with a small, host-owned
runtime observation and exposes only bounded status codes.  Raw LETS responses,
receipts, identities, exception text, and configuration values are never part
of the public model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orchestrator.lets_config import LetsConfigLoad, LetsMode

LetsRuntimeStatus = Literal[
    "healthy",
    "starting",
    "unavailable",
    "stale",
    "trust_failed",
    "replay_store_unavailable",
    "authority_anchor_unavailable",
]
LetsComponentStatus = Literal["disabled", "healthy", "degraded", "blocked"]

_RUNTIME_STATUSES = frozenset(
    {
        "healthy",
        "starting",
        "unavailable",
        "stale",
        "trust_failed",
        "replay_store_unavailable",
        "authority_anchor_unavailable",
    }
)
_OPERATOR_CODES = frozenset(
    {
        "lets_disabled",
        "lets_configuration_invalid",
        "lets_healthy",
        "lets_starting",
        "lets_unavailable",
        "lets_state_stale",
        "lets_trust_failed",
        "lets_replay_store_unavailable",
        "lets_authority_anchor_unavailable",
    }
)


@dataclass(frozen=True, slots=True)
class LetsRuntimeObservation:
    """A value-free observation supplied by the eventual runtime monitor."""

    status: LetsRuntimeStatus
    observed_at_ns: int
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.status not in _RUNTIME_STATUSES:
            raise ValueError("invalid_runtime_status")
        if type(self.observed_at_ns) is not int or self.observed_at_ns < 0:
            raise ValueError("invalid_observed_at")
        if type(self.retryable) is not bool:
            raise ValueError("invalid_retryable")
        if self.status in {"healthy", "trust_failed", "stale"} and self.retryable:
            raise ValueError("invalid_retryable_status")


@dataclass(frozen=True, slots=True)
class LetsDenyReason:
    """Stable public denial projection with no source error detail."""

    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        if type(self.retryable) is not bool or not _is_public_deny_reason(
            self.code,
            self.message,
            self.retryable,
        ):
            raise ValueError("invalid_public_deny_reason")

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class LetsHealthSnapshot:
    """Redacted application and governed-dispatch readiness."""

    mode: LetsMode
    component_status: LetsComponentStatus
    application_ready: bool
    existing_behavior_permitted: bool
    governed_dispatch_ready: bool
    shadow_probe_ready: bool
    diagnostic_only: bool
    operator_code: str
    user_reason: LetsDenyReason | None
    retryable: bool
    observed_at_ns: int | None

    def __post_init__(self) -> None:
        if self.mode not in {"off", "shadow", "enforce"}:
            raise ValueError("invalid_health_mode")
        if self.component_status not in {"disabled", "healthy", "degraded", "blocked"}:
            raise ValueError("invalid_component_status")
        if self.operator_code not in _OPERATOR_CODES:
            raise ValueError("invalid_operator_code")
        booleans = (
            self.application_ready,
            self.existing_behavior_permitted,
            self.governed_dispatch_ready,
            self.shadow_probe_ready,
            self.diagnostic_only,
            self.retryable,
        )
        if any(type(value) is not bool for value in booleans):
            raise ValueError("invalid_health_boolean")
        if self.user_reason is not None and not isinstance(
            self.user_reason,
            LetsDenyReason,
        ):
            raise ValueError("invalid_user_reason")
        if self.observed_at_ns is not None and (
            type(self.observed_at_ns) is not int or self.observed_at_ns < 0
        ):
            raise ValueError("invalid_health_observed_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "component_status": self.component_status,
            "application_ready": self.application_ready,
            "existing_behavior_permitted": self.existing_behavior_permitted,
            "governed_dispatch_ready": self.governed_dispatch_ready,
            "shadow_probe_ready": self.shadow_probe_ready,
            "diagnostic_only": self.diagnostic_only,
            "operator_code": self.operator_code,
            "user_reason": (
                None if self.user_reason is None else self.user_reason.to_dict()
            ),
            "retryable": self.retryable,
            "observed_at_ns": self.observed_at_ns,
        }


_DENIALS: dict[str, tuple[str, str, bool]] = {
    "authentication_failed": (
        "governance_trust_failed",
        "Governed execution could not verify its authority service.",
        False,
    ),
    "permission_denied": (
        "authority_denied",
        "The requested governed action was not authorized.",
        False,
    ),
    "not_found": (
        "authority_state_missing",
        "Required governed authority state is unavailable.",
        False,
    ),
    "request_conflict": (
        "governance_state_conflict",
        "Governed execution state changed; refresh before retrying.",
        False,
    ),
    "request_timeout": (
        "governance_unavailable",
        "Governed execution is temporarily unavailable.",
        True,
    ),
    "transport_unavailable": (
        "governance_unavailable",
        "Governed execution is temporarily unavailable.",
        True,
    ),
    "remote_unavailable": (
        "governance_unavailable",
        "Governed execution is temporarily unavailable.",
        True,
    ),
    "invalid_response": (
        "governance_invalid_response",
        "Governed execution returned an invalid response.",
        False,
    ),
    "remote_validation_failed": (
        "governance_invalid_response",
        "Governed execution returned an invalid response.",
        False,
    ),
    "stale_receipt": (
        "authority_expired",
        "The governed authorization is no longer current.",
        False,
    ),
    "receipt_expired": (
        "authority_expired",
        "The governed authorization is no longer current.",
        False,
    ),
    "replay_detected": (
        "authority_replay_rejected",
        "The governed authorization was already used.",
        False,
    ),
    "claimed_receipt": (
        "authority_replay_rejected",
        "The governed authorization was already used.",
        False,
    ),
    "client_not_configured": (
        "governance_not_ready",
        "Governed execution is not ready.",
        False,
    ),
    "client_closed": (
        "governance_not_ready",
        "Governed execution is not ready.",
        False,
    ),
}


def _is_public_deny_reason(code: object, message: object, retryable: object) -> bool:
    return any(
        (code, message, retryable) == declared for declared in _DENIALS.values()
    ) or (code, message, retryable) == (
        "governance_failed",
        "Governed execution could not complete safely.",
        retryable,
    )


def project_deny_reason(
    source_code: object,
    *,
    retryable: object = False,
) -> LetsDenyReason:
    """Map an internal failure to an allowlisted, value-free public reason."""

    if type(source_code) is str:
        declared = _DENIALS.get(source_code)
        if declared is not None:
            code, message, declared_retryable = declared
            return LetsDenyReason(code, message, declared_retryable)
    return LetsDenyReason(
        "governance_failed",
        "Governed execution could not complete safely.",
        retryable is True,
    )


def project_lets_health(
    loaded: LetsConfigLoad,
    observation: LetsRuntimeObservation | None = None,
) -> LetsHealthSnapshot:
    """Combine configuration and runtime posture without leaking source values."""

    if not isinstance(loaded, LetsConfigLoad):
        raise TypeError("invalid_config_load")
    readiness = loaded.readiness
    mode = readiness.mode
    if mode not in {"off", "shadow", "enforce"}:
        raise ValueError("invalid_readiness_mode")

    if mode == "off":
        return LetsHealthSnapshot(
            mode=mode,
            component_status="disabled",
            application_ready=True,
            existing_behavior_permitted=True,
            governed_dispatch_ready=False,
            shadow_probe_ready=False,
            diagnostic_only=False,
            operator_code="lets_disabled",
            user_reason=None,
            retryable=False,
            observed_at_ns=None,
        )

    if loaded.config is None:
        blocked = mode == "enforce"
        return LetsHealthSnapshot(
            mode=mode,
            component_status="blocked" if blocked else "degraded",
            application_ready=not blocked,
            existing_behavior_permitted=not blocked,
            governed_dispatch_ready=False,
            shadow_probe_ready=False,
            diagnostic_only=not blocked,
            operator_code="lets_configuration_invalid",
            user_reason=(
                project_deny_reason("client_not_configured") if blocked else None
            ),
            retryable=False,
            observed_at_ns=None,
        )

    if observation is None:
        observation = LetsRuntimeObservation(
            status="starting",
            observed_at_ns=0,
            retryable=True,
        )

    if observation.status == "healthy":
        return LetsHealthSnapshot(
            mode=mode,
            component_status="healthy",
            application_ready=True,
            existing_behavior_permitted=True,
            governed_dispatch_ready=mode == "enforce",
            shadow_probe_ready=mode == "shadow",
            diagnostic_only=mode == "shadow",
            operator_code="lets_healthy",
            user_reason=None,
            retryable=False,
            observed_at_ns=observation.observed_at_ns,
        )

    blocked = mode == "enforce"
    operator_code, public_source = _runtime_failure_codes(observation.status)
    return LetsHealthSnapshot(
        mode=mode,
        component_status="blocked" if blocked else "degraded",
        application_ready=not blocked,
        existing_behavior_permitted=not blocked,
        governed_dispatch_ready=False,
        shadow_probe_ready=False,
        diagnostic_only=not blocked,
        operator_code=operator_code,
        user_reason=(
            project_deny_reason(public_source, retryable=observation.retryable)
            if blocked
            else None
        ),
        retryable=observation.retryable,
        observed_at_ns=observation.observed_at_ns,
    )


def _runtime_failure_codes(status: LetsRuntimeStatus) -> tuple[str, str]:
    if status == "trust_failed":
        return "lets_trust_failed", "authentication_failed"
    if status == "stale":
        return "lets_state_stale", "stale_receipt"
    if status == "replay_store_unavailable":
        return "lets_replay_store_unavailable", "client_not_configured"
    if status == "authority_anchor_unavailable":
        return "lets_authority_anchor_unavailable", "client_not_configured"
    if status == "starting":
        return "lets_starting", "client_not_configured"
    return "lets_unavailable", "remote_unavailable"


def observe_lets_runtime(runtime: object | None) -> LetsRuntimeObservation | None:
    """Derive a value-free observation from the composition's cached state.

    This reads only; it never contacts the warden.  A fully wired graph
    carries a cached LIVE reachability observation (``runtime.reachability``,
    ``orchestrator.lets_probe``) whose ``observed_at_ns`` is the time of the
    last probe — ``None`` from the cache means the warden has never answered
    and projects as ``starting``.  An active mode that fell back to the
    explicit degraded graph (no client, so no probe) is ``unavailable`` as of
    composition.  Off mode and invalid configuration carry no runtime
    observation and are projected from the configuration posture alone.
    """

    if runtime is None:
        return None
    loaded = getattr(runtime, "loaded", None)
    if not isinstance(loaded, LetsConfigLoad):
        return None
    if loaded.readiness.mode == "off" or loaded.config is None:
        return None
    observed_at_ns = getattr(runtime, "composed_at_ns", 0)
    if type(observed_at_ns) is not int or observed_at_ns < 0:
        observed_at_ns = 0
    if getattr(runtime, "ready", False) is not True:
        return LetsRuntimeObservation("unavailable", observed_at_ns, retryable=True)
    cached = getattr(getattr(runtime, "reachability", None), "cached", None)
    if not callable(cached):
        # A wired graph with no probe seam cannot prove reachability: never
        # fabricate "healthy" from the mere existence of a client.
        return None
    observation = cached()
    if observation is None:
        return None
    if not isinstance(observation, LetsRuntimeObservation):
        return LetsRuntimeObservation("unavailable", observed_at_ns, retryable=True)
    return observation


def project_runtime_health(
    runtime: object | None,
    *,
    fallback: LetsConfigLoad | None = None,
) -> LetsHealthSnapshot:
    """Project the application-scoped composition (or its absence) to health.

    Without a composition there is no evidence that LETS was ever wired, so
    ``fallback`` (the configuration posture alone) is projected with no
    observation: an active mode reads as not yet observed, which blocks
    readiness in enforce and degrades it in shadow.  Off mode stays disabled.
    """

    loaded = getattr(runtime, "loaded", None)
    if isinstance(loaded, LetsConfigLoad):
        return project_lets_health(loaded, observe_lets_runtime(runtime))
    if not isinstance(fallback, LetsConfigLoad):
        raise TypeError("invalid_config_load")
    return project_lets_health(fallback)


def readiness_entry(snapshot: LetsHealthSnapshot) -> dict[str, object]:
    """The bounded ``lets`` object carried by ``/readyz``.

    Off mode is exactly ``{"mode": "off", "status": "disabled"}`` so a
    flag-off deployment exposes no LETS vocabulary beyond the fact that it is
    off.  Active modes add the operator code, whether existing (ungoverned)
    behavior may proceed, and the observation stamp: the time of the last
    live probe (``lets_probe``), or ``0`` when the warden never answered.
    """

    if not isinstance(snapshot, LetsHealthSnapshot):
        raise TypeError("invalid_health_snapshot")
    if snapshot.mode == "off":
        return {"mode": "off", "status": "disabled"}
    return {
        "mode": snapshot.mode,
        "status": snapshot.component_status,
        "reason": snapshot.operator_code,
        "governed_effects_permitted": snapshot.existing_behavior_permitted,
        "governed_dispatch_ready": snapshot.governed_dispatch_ready,
        "retryable": snapshot.retryable,
        "observed_at_ns": snapshot.observed_at_ns,
    }


def health_report(
    runtime: object | None,
    *,
    fallback: LetsConfigLoad | None = None,
) -> dict[str, object]:
    """Full redacted projection for the admin-only ``GET /lets/health``.

    Carries the snapshot, the configuration posture codes, and the redacted
    host configuration (``LetsHostConfig.redacted()``): never a file path,
    token, key, or raw LETS response.
    """

    snapshot = project_runtime_health(runtime, fallback=fallback)
    loaded = getattr(runtime, "loaded", None)
    if not isinstance(loaded, LetsConfigLoad):
        loaded = fallback
    readiness: dict[str, object] | None = None
    config: dict[str, object] | None = None
    if isinstance(loaded, LetsConfigLoad):
        posture = loaded.readiness
        readiness = {
            "mode": posture.mode,
            "status": posture.status,
            "reason": posture.reason,
            "application_ready": posture.application_ready,
            "lets_configured": posture.lets_configured,
            "governed_effects_permitted": posture.governed_effects_permitted,
            "diagnostic_only": posture.diagnostic_only,
        }
        if loaded.config is not None:
            config = dict(loaded.config.redacted())
    return {
        "health": snapshot.to_dict(),
        "readiness": readiness,
        "config": config,
        "composition_bound": isinstance(
            getattr(runtime, "loaded", None),
            LetsConfigLoad,
        ),
    }


__all__ = [
    "LetsDenyReason",
    "LetsHealthSnapshot",
    "LetsRuntimeObservation",
    "health_report",
    "observe_lets_runtime",
    "project_deny_reason",
    "project_lets_health",
    "project_runtime_health",
    "readiness_entry",
]
