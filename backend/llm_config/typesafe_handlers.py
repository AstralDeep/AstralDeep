"""Validates, probes, and saves owner-scoped decisions providers from the settings surface.
Failed probes preserve the existing encrypted configuration in typesafe_store.py.
Audit events contain fingerprints rather than credentials.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Tuple

from orchestrator.typesafe_routing.custom import CustomDecisionClient, probe_custom

logger = logging.getLogger("LLMConfig.TypeSafeHandlers")

# Generous vs the 1.5s turn budget: someone is watching
PROBE_TIMEOUT_SECONDS = 5.0

PROBE_RATE_LIMIT = 5
PROBE_RATE_WINDOW_SECONDS = 60.0

MAX_KEY_CHARS = 512

_probe_history: dict[str, list[float]] = {}


class TypeSafeSaveError(Exception):
    pass


def _check_probe_rate(user_id: str, *, now: Optional[float] = None) -> None:
    moment = now if now is not None else time.monotonic()
    history = [t for t in _probe_history.get(user_id, ()) if moment - t < PROBE_RATE_WINDOW_SECONDS]
    if len(history) >= PROBE_RATE_LIMIT:
        wait = int(PROBE_RATE_WINDOW_SECONDS - (moment - history[0])) + 1
        _probe_history[user_id] = history
        raise TypeSafeSaveError(
            f"Too many attempts. Wait {wait} seconds and try again."
        )
    history.append(moment)
    _probe_history[user_id] = history


def reset_probe_rate(user_id: Optional[str] = None) -> None:
    if user_id is None:
        _probe_history.clear()
    else:
        _probe_history.pop(user_id, None)


def validate_key(raw: Any) -> str:
    if not isinstance(raw, str):
        raise TypeSafeSaveError("Enter your TypeSafe API key.")
    key = raw.strip()
    if not key:
        raise TypeSafeSaveError("Enter your TypeSafe API key.")
    if len(key) > MAX_KEY_CHARS:
        raise TypeSafeSaveError(
            f"That key is longer than {MAX_KEY_CHARS} characters — check for a "
            f"stray paste."
        )
    if any(character.isspace() for character in key):
        raise TypeSafeSaveError(
            "That key contains a space. Copy it again without surrounding text."
        )
    return key


async def probe_key(key: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> None:
    from orchestrator.typesafe_routing.budget import is_auth_failure, is_transient
    from orchestrator.typesafe_routing.client import (
        TYPESAFE_API_BASE,
        TYPESAFE_MODEL,
        TypeSafeUnavailable,
        load_sdk,
    )

    try:
        surface = load_sdk()
    except TypeSafeUnavailable:
        raise TypeSafeSaveError(
            "TypeSafe support is not installed on this server."
        ) from None

    client = surface.client_class(
        api_key=key,
        base_url=TYPESAFE_API_BASE,
        model=TYPESAFE_MODEL,
        retry=surface.retry_policy(max_retries=0),
        timeout=timeout,
    )
    try:
        await asyncio.wait_for(client.models.list(), timeout=timeout + 1.0)
    except (asyncio.TimeoutError, TimeoutError):
        raise TypeSafeSaveError(
            "TypeSafe didn't respond in time. Your key was not changed — try again."
        ) from None
    except Exception as error:  # noqa: BLE001
        if is_auth_failure(error):
            raise TypeSafeSaveError(
                "TypeSafe rejected that key. Check it and try again."
            ) from None
        if is_transient(error):
            raise TypeSafeSaveError(
                "TypeSafe is unreachable right now. Your key was not changed — "
                "try again shortly."
            ) from None
        raise TypeSafeSaveError(
            "TypeSafe refused that request. Check the key and try again."
        ) from None
    finally:
        closer = getattr(client, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # pragma: no cover
                logger.debug("closing the probe client failed", exc_info=True)


async def save_key(
    store: Any,
    user_id: str,
    raw_key: Any,
    *,
    recorder: Any = None,
    actor_user_id: Optional[str] = None,
    auth_principal: Optional[str] = None,
    probe: Any = None,
) -> Tuple[Any, str]:
    from llm_config.typesafe_store import key_fingerprint

    key = validate_key(raw_key)
    _check_probe_rate(user_id)
    await (probe or probe_key)(key)

    status = await store.save(user_id, key)
    fingerprint = key_fingerprint(key)

    try:
        from orchestrator.typesafe_routing.budget import circuit

        circuit().reset(user_id)
    except Exception:  # pragma: no cover
        logger.debug("circuit reset after TypeSafe save failed", exc_info=True)

    await _audit(
        recorder,
        actor_user_id=actor_user_id or user_id,
        auth_principal=auth_principal or user_id,
        action="saved",
        outcome="success",
        description="User saved a TypeSafe routing key",
        fingerprint=fingerprint,
    )
    return status, fingerprint


async def save_decisions(store: Any, user_id: str, fields: dict, **audit: Any) -> None:
    provider = fields.get("decisions_provider", "typesafe")
    if provider not in ("typesafe", "custom"):
        raise TypeSafeSaveError("Choose TypeSafe AI or Custom.")
    raw_key = fields.get("typesafe_api_key", "") if provider == "typesafe" else fields.get("decisions_api_key", "")
    key = validate_key(raw_key) if raw_key else ""
    saved = await store.get_key(user_id)
    if provider == "typesafe":
        if not key and saved is not None and saved.provider == "typesafe":
            key = saved.api_key
        await save_key(store, user_id, key, **audit)
        return
    base_url = fields.get("decisions_base_url", "").strip().rstrip("/")
    model = fields.get("decisions_model", "").strip()
    try:
        CustomDecisionClient(base_url, model)
    except ValueError:
        raise TypeSafeSaveError("Enter a valid base URL and model name for the decisions model.") from None
    if not key and saved is not None and saved.api_key:
        if saved.provider != "custom" or saved.base_url != base_url:
            raise TypeSafeSaveError(
                "The endpoint changed; enter the API key again. "
                "For a keyless endpoint, remove the saved decisions model first.")
        key = saved.api_key
    _check_probe_rate(user_id)
    try:
        await probe_custom(base_url=base_url, model=model, api_key=key)
    except (ValueError, TimeoutError):
        raise TypeSafeSaveError(
            "Couldn't connect to the decisions model. Check the base URL, model name, and API key. "
            "Your saved configuration was not changed.") from None
    _, fingerprint = await store.save_config(user_id, provider=provider, base_url=base_url, model=model, api_key=key)
    from orchestrator.typesafe_routing.budget import circuit

    circuit().reset(user_id)
    await _audit(audit.get("recorder"), actor_user_id=audit.get("actor_user_id") or user_id,
                 auth_principal=audit.get("auth_principal") or user_id,
                 action="saved", outcome="success", description="User saved a custom decisions model",
                 fingerprint=fingerprint)


async def clear_key(
    store: Any,
    user_id: str,
    *,
    recorder: Any = None,
    actor_user_id: Optional[str] = None,
    auth_principal: Optional[str] = None,
) -> bool:
    removed = await store.clear(user_id)
    reset_probe_rate(user_id)
    try:
        from orchestrator.typesafe_routing.budget import circuit

        circuit().reset(user_id)
    except Exception:  # pragma: no cover
        logger.debug("circuit reset after TypeSafe clear failed", exc_info=True)
    if removed:
        await _audit(
            recorder,
            actor_user_id=actor_user_id or user_id,
            auth_principal=auth_principal or user_id,
            action="cleared",
            outcome="success",
            description="User removed their TypeSafe routing key",
            fingerprint=None,
        )
    return removed


async def _audit(
    recorder: Any,
    *,
    actor_user_id: str,
    auth_principal: str,
    action: str,
    outcome: str,
    description: str,
    fingerprint: Optional[str],
) -> None:
    if recorder is None:
        return
    from datetime import UTC, datetime
    from uuid import uuid4

    from audit.schemas import AuditEventCreate

    from .audit_events import _assert_no_api_key

    inputs_meta: dict[str, Any] = {"action": action}
    if fingerprint is not None:
        inputs_meta["key_fingerprint"] = fingerprint
    _assert_no_api_key(inputs_meta)

    started = datetime.now(UTC)
    try:
        await recorder.record(
            AuditEventCreate(
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                event_class="typesafe_credential",
                action_type=f"typesafe_credential.{action}",
                description=description,
                correlation_id=str(uuid4()),
                outcome=outcome,
                inputs_meta=inputs_meta,
                outputs_meta={},
                started_at=started,
                completed_at=started,
            )
        )
    except Exception:  # pragma: no cover
        logger.warning("TypeSafe credential audit event failed", exc_info=True)


async def record_discarded(
    recorder: Any, user_id: str, *, auth_principal: Optional[str] = None
) -> None:
    await _audit(
        recorder,
        actor_user_id=user_id,
        auth_principal=auth_principal or user_id,
        action="discarded",
        outcome="failure",
        description=(
            "An unreadable TypeSafe credential row was discarded; the user now "
            "has no TypeSafe key"
        ),
        fingerprint=None,
    )


__all__ = (
    "MAX_KEY_CHARS",
    "PROBE_RATE_LIMIT",
    "PROBE_RATE_WINDOW_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "TypeSafeSaveError",
    "clear_key",
    "probe_key",
    "record_discarded",
    "reset_probe_rate",
    "save_key",
    "validate_key",
)
