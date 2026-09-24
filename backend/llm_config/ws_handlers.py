"""WebSocket handlers for saving/clearing a user's LLM configuration: re-probes the
exact submitted triple before persisting via user_store.py, audits, and acks. Returns
whether a row changed so the caller can re-gate the user's sockets.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Iterator, Optional

from audit.recorder import Recorder
from orchestrator.work_admission import OperationState, StaleExecutionFenceError

from .audit_events import record_llm_config_change
from .probe import probe_chat_completion
from .providers import get_preset, resolve_base_url
from .user_store import LLMConfigCommitDeadlineExceeded, UserLLMConfigStore

logger = logging.getLogger("LLMConfig.WSHandlers")

SafeSend = Callable[[Any, str], Awaitable[None]]
PhaseEmitter = Callable[[str, str, str], Awaitable[None]]
UnlockCallback = Callable[[], Awaitable[bool]]

LLM_CREDENTIAL_ATTEMPT_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class LLMConfigOperationContext:
    coordinator: Any
    fence: Any
    deadline_at_monotonic: float
    deadline_at_utc: datetime
    emit_phase: PhaseEmitter
    unlock_after_save: UnlockCallback
    failure: "LLMConfigOperationFailure | None" = field(
        default=None, init=False
    )
    completed_operation: Any | None = field(default=None, init=False)

    def remember_failure(
        self, failure: "LLMConfigOperationFailure"
    ) -> "LLMConfigOperationFailure":
        self.failure = failure
        return failure

    async def ensure_live(self) -> None:
        if time.monotonic() >= self.deadline_at_monotonic:
            raise self.remember_failure(LLMConfigOperationFailure.deadline())
        await asyncio.to_thread(
            self.coordinator.assert_current_execution, self.fence
        )

    async def phase(self, state: str, phase: str, label: str) -> None:
        await self.ensure_live()
        operation = await asyncio.to_thread(
            self.coordinator.update_phase, self.fence, phase
        )
        if time.monotonic() >= self.deadline_at_monotonic:
            raise self.remember_failure(LLMConfigOperationFailure.deadline())
        await self.emit_phase(state, phase, label)
        # Delivery-only: the durable revision above stays authoritative
        if operation.phase_code != phase:  # pragma: no cover
            raise StaleExecutionFenceError("operation phase update was not retained")


class LLMConfigOperationFailure(RuntimeError):
    def __init__(
        self,
        *,
        state: OperationState,
        code: str,
        safe_summary: str,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(safe_summary)
        self.state = state
        self.code = code
        self.safe_summary = safe_summary
        self.retry_after_ms = retry_after_ms

    @classmethod
    def deadline(cls) -> "LLMConfigOperationFailure":
        return cls(
            state=OperationState.RETRYABLE,
            code="deadline_exceeded",
            safe_summary="Credential save timed out",
        )


_ACTIVE_LLM_CONFIG_OPERATION: contextvars.ContextVar[
    LLMConfigOperationContext | None
] = contextvars.ContextVar("active_llm_config_operation", default=None)


@contextmanager
def active_llm_config_operation(
    operation: LLMConfigOperationContext,
) -> Iterator[None]:
    token = _ACTIVE_LLM_CONFIG_OPERATION.set(operation)
    try:
        yield
    finally:
        _ACTIVE_LLM_CONFIG_OPERATION.reset(token)


def _operation_failure(error_class: str | None) -> LLMConfigOperationFailure:
    if error_class in {"auth_failed", "model_not_found", "contract_violation"}:
        return LLMConfigOperationFailure(
            state=OperationState.FAILED,
            code="validation_failed",
            safe_summary="The provider credentials or model were rejected",
        )
    if error_class == "transport_error":
        return LLMConfigOperationFailure(
            state=OperationState.RETRYABLE,
            code="network_unavailable",
            safe_summary="The provider could not be reached",
        )
    return LLMConfigOperationFailure(
        state=OperationState.RETRYABLE,
        code="provider_unavailable",
        safe_summary="The provider is temporarily unavailable",
    )


def validate_config_submission(config: Dict[str, Any]) -> tuple:
    if not isinstance(config, dict):
        return {}, {"config": "malformed payload"}
    provider = (config.get("provider") or "custom").strip().lower()
    api_key = (config.get("api_key") or "").strip()
    model = (config.get("model") or "").strip()
    submitted_url = (config.get("base_url") or "").strip()

    errors: Dict[str, str] = {}
    preset = get_preset(provider)
    if preset is None:
        errors["provider"] = f"unknown provider {provider!r}"
        return {}, errors
    base_url = resolve_base_url(provider, submitted_url)
    if not base_url:
        errors["base_url"] = "endpoint address is required"
    elif not (base_url.startswith("http://") or base_url.startswith("https://")):
        errors["base_url"] = "endpoint address must start with http:// or https://"
    if not model:
        errors["model"] = "model is required"
    if preset.key_required and not api_key:
        errors["api_key"] = f"an API key is required for {preset.label}"

    fields = {
        "provider": provider,
        "base_url": base_url or "",
        "model": model,
        "api_key": api_key,
    }
    return fields, errors


async def _send_invalid(safe_send: SafeSend, websocket: Any,
                        message: str, *, fields: Optional[Dict[str, str]] = None,
                        error_class: Optional[str] = None) -> None:
    payload: Dict[str, Any] = {
        "type": "error",
        "code": "llm_config_invalid",
        "message": message,
    }
    if fields:
        payload["fields"] = fields
    if error_class:
        payload["error_class"] = error_class
    await safe_send(websocket, json.dumps(payload))


async def handle_llm_config_set(
    *,
    safe_send: SafeSend,
    websocket: Any,
    config: Dict[str, Any],
    actor_user_id: str,
    auth_principal: str,
    store: UserLLMConfigStore,
    recorder: Recorder,
) -> bool:
    operation = _ACTIVE_LLM_CONFIG_OPERATION.get()
    fields, errors = validate_config_submission(config)
    if errors:
        await _send_invalid(
            safe_send, websocket,
            "; ".join(f"{k}: {v}" for k, v in errors.items()),
            fields=errors,
        )
        if operation is not None:
            raise operation.remember_failure(LLMConfigOperationFailure(
                state=OperationState.FAILED,
                code="validation_failed",
                safe_summary="The provider settings are invalid",
            ))
        return False

    if operation is not None:
        await operation.phase(
            "validating",
            "validating_credentials",
            "Checking your provider credentials…",
        )

    ok, error_class, upstream = await probe_chat_completion(
        api_key=fields["api_key"], base_url=fields["base_url"], model=fields["model"])
    try:
        await record_llm_config_change(
            recorder,
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            action="tested",
            base_url=fields["base_url"],
            model=fields["model"],
            transport="ws",
            result="success" if ok else "failure",
            error_class=error_class if not ok else None,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(f"llm_config_change(tested) audit failed (non-fatal): {exc}")
    if not ok:
        failure = _operation_failure(error_class)
        await _send_invalid(
            safe_send, websocket,
            (
                failure.safe_summary
                if operation is not None
                else f"Connection test failed ({error_class}): {upstream or 'no details'}"
            ),
            error_class=error_class,
        )
        if operation is not None:
            raise operation.remember_failure(failure)
        return False

    if operation is not None:
        await operation.ensure_live()
    prior = await store.get(actor_user_id)
    if operation is not None:
        await operation.phase(
            "persisting",
            "saving_credentials",
            "Saving your provider settings…",
        )
    try:
        if operation is None:
            await store.set(
                actor_user_id,
                provider=fields["provider"],
                base_url=fields["base_url"],
                model=fields["model"],
                api_key=fields["api_key"],
            )
        else:
            commit = await store.set_fenced(
                actor_user_id,
                provider=fields["provider"],
                base_url=fields["base_url"],
                model=fields["model"],
                api_key=fields["api_key"],
                coordinator=operation.coordinator,
                fence=operation.fence,
                deadline_at_monotonic=operation.deadline_at_monotonic,
                deadline_at_utc=operation.deadline_at_utc,
            )
            operation.completed_operation = commit.operation
    except LLMConfigCommitDeadlineExceeded as exc:
        failure = LLMConfigOperationFailure.deadline()
        if operation is not None:
            failure = operation.remember_failure(failure)
        raise failure from exc
    except ValueError as exc:
        await _send_invalid(safe_send, websocket, str(exc))
        if operation is not None:
            raise operation.remember_failure(LLMConfigOperationFailure(
                state=OperationState.FAILED,
                code="validation_failed",
                safe_summary="The provider settings are invalid",
            )) from exc
        return False

    action = "updated" if prior is not None else "created"
    try:
        await record_llm_config_change(
            recorder,
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            action=action,
            base_url=fields["base_url"],
            model=fields["model"],
            transport="ws",
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(f"llm_config_change audit failed (non-fatal): {exc}")

    # Ack is the outer wrapper's job — avoids a terminal-state race
    if operation is None:
        await safe_send(
            websocket,
            json.dumps({"type": "llm_config_ack", "ok": True}),
        )
    return True


async def handle_llm_config_clear(
    *,
    safe_send: SafeSend,
    websocket: Any,
    actor_user_id: str,
    auth_principal: str,
    store: UserLLMConfigStore,
    recorder: Recorder,
) -> bool:
    removed = await store.clear(actor_user_id)
    if removed:
        try:
            await record_llm_config_change(
                recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                action="cleared",
                base_url=None,
                model=None,
                transport="ws",
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"llm_config_change audit failed (non-fatal): {exc}")
    await safe_send(websocket, json.dumps({"type": "llm_config_ack", "ok": True}))
    return removed


async def populate_from_register_ui(
    *,
    websocket: Any,
    llm_config: Optional[Dict[str, Any]],
    actor_user_id: str,
    auth_principal: str,
    recorder: Recorder,
    store: Optional[UserLLMConfigStore] = None,
) -> None:
    if llm_config:
        logger.debug(
            "register_ui.llm_config ignored (feature 054: server-persisted "
            "config is authoritative)")
    return None
