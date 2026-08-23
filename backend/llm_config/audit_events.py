"""Audit-event emission helpers for feature 006-user-llm-config.

Three event classes are emitted via the existing feature-003
:class:`backend.audit.recorder.Recorder`:

* ``llm_config_change`` — user creates / updates / clears / tests their
  personal LLM configuration. Payload carries ``action``, ``base_url``,
  ``model``, and (for ``tested``) ``result`` / ``error_class``.
* ``llm_unconfigured`` — an LLM-dependent feature was invoked but
  neither the user's personal credentials nor the operator's ``.env``
  default were usable. Payload carries the call-site ``feature``.
* ``llm_call`` — every LLM-dependent call (success or failure, user or
  operator-default credentials). Payload carries ``feature``,
  ``credential_source``, ``base_url``, ``model``, ``total_tokens``
  (or ``None``), and ``upstream_error_class`` on failure.

Critical invariant: the user's API key MUST NEVER appear in any audit
payload, in any field, under any action. The :func:`_assert_no_api_key`
guard is invoked on every payload before recording; an internal
``ValueError`` is raised on detection (loud failure on the programmer
error of leaking a key, rather than silent recording).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from audit.recorder import Recorder
from audit.schemas import AuditEventCreate

from .types import CredentialSource, ResolvedConfig

logger = logging.getLogger("LLMConfig.AuditEvents")


# Forbidden substrings — common API-key prefixes. If any of these appears
# in any payload value the helper raises rather than recording. This is
# defence-in-depth on top of the application-layer rule "never put
# api_key in a payload field."
_KEY_PREFIX_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),  # OpenAI-style (also sk-ant-/sk-or-/sk-proj-)
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{20,}\b"),  # Groq-style
    re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}\b"),  # xAI-style
    re.compile(r"\bor-[A-Za-z0-9_\-]{20,}\b"),  # OpenRouter-style
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b"),  # Google API-key-style (Gemini)
)


def _assert_no_api_key(payload: Dict[str, Any]) -> None:
    """Defence-in-depth check: raise if any payload value contains an
    API-key-shaped substring or a literal ``api_key`` key.

    This is intentionally conservative — false positives here are
    preferable to a leaked key. Callers MUST pass already-redacted
    payloads.
    """
    if "api_key" in payload:
        raise ValueError(
            "Audit-event payload contains forbidden field 'api_key'. "
            "FR-002 / FR-006 forbid recording the user's API key under "
            "any circumstances."
        )
    for k, v in payload.items():
        if isinstance(v, str):
            for pat in _KEY_PREFIX_PATTERNS:
                if pat.search(v):
                    raise ValueError(
                        f"Audit-event payload field {k!r} appears to contain "
                        f"an API key. Strip it before recording."
                    )
        elif isinstance(v, dict):
            _assert_no_api_key(v)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def record_llm_config_change(
    recorder: Recorder,
    *,
    actor_user_id: str,
    auth_principal: str,
    action: str,
    base_url: Optional[str],
    model: Optional[str],
    transport: str,
    result: Optional[str] = None,
    error_class: Optional[str] = None,
    correlation_id: Optional[str] = None,
    scope: str = "user",
) -> None:
    """Emit an ``llm_config_change`` audit event.

    Args:
        action: One of ``"created"``, ``"updated"``, ``"cleared"``,
            ``"tested"``, or ``"discarded_undecryptable"`` (feature 054:
            an at-rest record could not be decrypted — key rotation or
            corruption — and was deleted, re-gating the user).
        base_url / model: Non-sensitive descriptors. ``None`` permitted
            for ``cleared`` (the caller may not have the prior values).
        transport: ``"ws"`` or ``"rest"`` — which channel the change came in on.
        result: ``"success"`` or ``"failure"`` — only for ``action="tested"``.
        error_class: Only set when ``result == "failure"``; one of the
            taxonomy values from contracts/rest-llm-test.md.
        scope: ``"user"`` (a user's own record) or ``"system"`` (the
            admin-managed deployment credential — feature 054).
    """
    if action not in ("created", "updated", "cleared", "tested",
                      "discarded_undecryptable"):
        raise ValueError(f"unknown action: {action!r}")
    if scope not in ("user", "system"):
        raise ValueError(f"unknown scope: {scope!r}")

    inputs_meta: Dict[str, Any] = {
        "action": action,
        "transport": transport,
        "scope": scope,
    }
    if base_url is not None:
        inputs_meta["base_url"] = base_url
    if model is not None:
        inputs_meta["model"] = model

    outputs_meta: Dict[str, Any] = {}
    if action == "tested":
        if result not in ("success", "failure"):
            raise ValueError("action='tested' requires result='success'|'failure'")
        outputs_meta["result"] = result
        if result == "failure" and error_class is not None:
            outputs_meta["error_class"] = error_class

    _assert_no_api_key(inputs_meta)
    _assert_no_api_key(outputs_meta)

    outcome = "success"
    if action == "tested" and result == "failure":
        outcome = "failure"

    started = _now()
    event = AuditEventCreate(
        actor_user_id=actor_user_id,
        auth_principal=auth_principal,
        event_class="llm_config_change",
        action_type=f"llm_config.{action}",
        description=_describe_config_change(action, model, result, scope),
        correlation_id=correlation_id or str(uuid4()),
        outcome=outcome,
        inputs_meta=inputs_meta,
        outputs_meta=outputs_meta,
        started_at=started,
        completed_at=started,
    )
    await recorder.record(event)


def _describe_config_change(
    action: str, model: Optional[str], result: Optional[str],
    scope: str = "user",
) -> str:
    # scope="system" describes the admin-managed deployment credential
    # (feature 054); scope="user" describes a user's personal record.
    who, what = (("Admin", "the system LLM credential") if scope == "system"
                 else ("User", "their personal LLM configuration"))
    if action == "cleared":
        return f"{who} cleared {what}"
    if action == "discarded_undecryptable":
        return (f"Stored {'system ' if scope == 'system' else ''}LLM "
                "configuration could not be decrypted and was discarded "
                "(treated as unconfigured)")
    model_str = f" ({model})" if model else ""
    if action == "tested":
        if result == "success":
            return f"{who} successfully tested {what}{model_str}"
        return f"{who}'s LLM configuration test failed{model_str}"
    if action == "created":
        return f"{who} saved {what}{model_str}"
    return f"{who} updated {what}{model_str}"


async def record_llm_unconfigured(
    recorder: Recorder,
    *,
    actor_user_id: str,
    auth_principal: str,
    feature: str,
    correlation_id: Optional[str] = None,
) -> None:
    """Emit an ``llm_unconfigured`` audit event when both credential
    sources are absent and an LLM-dependent feature could not proceed
    (FR-007).
    """
    inputs_meta = {"feature": feature, "reason": "no_user_config_no_env_default"}
    _assert_no_api_key(inputs_meta)
    started = _now()
    event = AuditEventCreate(
        actor_user_id=actor_user_id,
        auth_principal=auth_principal,
        event_class="llm_unconfigured",
        action_type="llm.unconfigured",
        description=f"LLM-dependent feature {feature!r} could not proceed: no credentials available",
        correlation_id=correlation_id or str(uuid4()),
        outcome="failure",
        inputs_meta=inputs_meta,
        outputs_meta={},
        started_at=started,
        completed_at=started,
    )
    await recorder.record(event)


async def record_llm_call(
    recorder: Recorder,
    *,
    actor_user_id: str,
    auth_principal: str,
    feature: str,
    credential_source: CredentialSource,
    resolved: ResolvedConfig,
    total_tokens: Optional[int],
    outcome: str,
    upstream_error_class: Optional[str] = None,
    correlation_id: Optional[str] = None,
    routed_model: Optional[str] = None,
) -> None:
    """Emit an ``llm_call`` audit event for every LLM-dependent invocation
    (FR-007a). The ``credential_source`` field is the cornerstone of
    SC-006 — operators use it to answer "for whom did the operator's
    account pay?".

    Args:
        credential_source: Which credential set served the call.
        resolved: The non-sensitive descriptors from the factory.
        total_tokens: From the upstream response's ``usage.total_tokens``,
            or ``None`` if the upstream omitted ``usage``.
        outcome: ``"success"`` or ``"failure"``.
        upstream_error_class: Only set on failure.
        routed_model: The model the request was ACTUALLY issued with when the
            model router (``FF_MODEL_ROUTER``) re-tiered a SYSTEM call. When
            given, ``model`` records it and ``configured_model`` keeps the
            record's default so the row names what was called, not merely
            what was configured.
    """
    if outcome not in ("success", "failure"):
        raise ValueError(f"unknown outcome: {outcome!r}")

    inputs_meta = {
        "feature": feature,
        "credential_source": credential_source.value,
        "base_url": resolved.base_url,
        "model": routed_model or resolved.model,
    }
    if routed_model and routed_model != resolved.model:
        inputs_meta["configured_model"] = resolved.model
    outputs_meta: Dict[str, Any] = {}
    if total_tokens is not None:
        outputs_meta["total_tokens"] = int(total_tokens)
    if outcome == "failure" and upstream_error_class is not None:
        outputs_meta["upstream_error_class"] = upstream_error_class

    _assert_no_api_key(inputs_meta)
    _assert_no_api_key(outputs_meta)

    started = _now()
    src_label = {
        CredentialSource.USER: "user credentials",
        CredentialSource.SYSTEM: "system credential",
    }.get(credential_source, "operator default")
    event = AuditEventCreate(
        actor_user_id=actor_user_id,
        auth_principal=auth_principal,
        event_class="llm_call",
        action_type=f"llm.call.{feature}",
        description=f"LLM call for {feature!r} via {src_label} ({outcome})",
        correlation_id=correlation_id or str(uuid4()),
        outcome=outcome,
        inputs_meta=inputs_meta,
        outputs_meta=outputs_meta,
        started_at=started,
        completed_at=started,
    )
    await recorder.record(event)
