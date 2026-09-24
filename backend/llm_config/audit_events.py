"""Audit-event emission for llm_config: llm_config_change, llm_unconfigured, and
llm_call, each built so the user's API key can never enter a payload since
_assert_no_api_key runs on every emit. Wraps audit/recorder.py.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from audit.recorder import Recorder
from audit.schemas import AuditEventCreate

from .log_scrub import TYPESAFE_KEY_PATTERN, _is_api_key_field
from .types import CredentialSource, ResolvedConfig

logger = logging.getLogger("LLMConfig.AuditEvents")


_KEY_PREFIX_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bor-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b"),
    TYPESAFE_KEY_PATTERN,
)


def _assert_no_api_key(payload: Dict[str, Any]) -> None:
    offending = [key for key in payload if _is_api_key_field(key)]
    if offending:
        raise ValueError(
            f"Audit-event payload contains forbidden credential field(s) "
            f"{sorted(offending)!r}. FR-002 / FR-006 forbid recording the "
            f"user's API key under any circumstances, and feature 089 extends "
            f"that to the TypeSafe key: any field whose name ends in "
            f"'api_key' is refused."
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
