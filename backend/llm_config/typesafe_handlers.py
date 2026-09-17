"""Save and clear a user's TypeSafe key from the settings surface (feature 089, US1).

The shape of this module is set by one rule from the spec: **a rejected save
must never destroy a working stored key** (FR-003). So the order is fixed --
acknowledge, validate, probe, and only then persist. A key that fails its probe
never reaches the store, and the user's previous key is still there.

The probe is a ``models.list()`` call with a 5-second budget and no retries.
That is a deliberate contrast with the turn budget: a user who has just pressed
Save is waiting on purpose and would rather wait five seconds than be told
nothing, whereas a turn is waiting on the user's behalf and must not.

Everything else here is about not leaking the key. It is never logged, never
put in an audit payload, never echoed back to a surface, and never kept after
the handler returns. What the surface sees is a status and a fingerprint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Tuple

logger = logging.getLogger("LLMConfig.TypeSafeHandlers")

#: The probe's wall clock. Generous compared with the 1.5 s turn budget,
#: because the person is standing in front of it.
PROBE_TIMEOUT_SECONDS = 5.0

#: A save probe costs a request against the user's own quota, so the button is
#: rate limited per user: at most this many probes in the window.
PROBE_RATE_LIMIT = 5
PROBE_RATE_WINDOW_SECONDS = 60.0

MAX_KEY_CHARS = 512

#: In-process probe accounting: user id -> monotonic timestamps.
_probe_history: dict[str, list[float]] = {}


class TypeSafeSaveError(Exception):
    """A save could not proceed. The message is shown to the user verbatim."""


def _check_probe_rate(user_id: str, *, now: Optional[float] = None) -> None:
    """Raise when the user has probed too often. Keeps their quota theirs."""
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
    """Forget probe history. Used by tests and by a successful clear."""
    if user_id is None:
        _probe_history.clear()
    else:
        _probe_history.pop(user_id, None)


def validate_key(raw: Any) -> str:
    """Return a usable key or raise with the message the user should see."""
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
    """Verify the key against TypeSafe. Raise :class:`TypeSafeSaveError` if not.

    ``models.list()`` is the cheapest authenticated call the SDK offers, which
    is what makes it the right probe: it proves the key works without spending
    a System One request.
    """
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
    except Exception as error:  # noqa: BLE001 - classified below
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
            except Exception:  # pragma: no cover - shutdown is best-effort
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
    """Validate, probe and persist. Returns ``(status, fingerprint)``.

    Raises :class:`TypeSafeSaveError` with a user-facing message at every step
    that can fail. The store is only touched once the probe has succeeded.
    """
    from llm_config.typesafe_store import key_fingerprint

    key = validate_key(raw_key)
    _check_probe_rate(user_id)
    await (probe or probe_key)(key)

    status = await store.save(user_id, key)
    fingerprint = key_fingerprint(key)

    # A newly accepted key deserves a clean slate: whatever opened this user's
    # circuit was about the old one.
    try:
        from orchestrator.typesafe_routing.budget import circuit

        circuit().reset(user_id)
    except Exception:  # pragma: no cover - the circuit is best-effort
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


async def clear_key(
    store: Any,
    user_id: str,
    *,
    recorder: Any = None,
    actor_user_id: Optional[str] = None,
    auth_principal: Optional[str] = None,
) -> bool:
    """Remove the user's key. Idempotent: a second Remove is not an error."""
    removed = await store.clear(user_id)
    reset_probe_rate(user_id)
    try:
        from orchestrator.typesafe_routing.budget import circuit

        circuit().reset(user_id)
    except Exception:  # pragma: no cover - the circuit is best-effort
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
    """Record a ``typesafe_credential`` event. Never raises into a save path."""
    if recorder is None:
        return
    from datetime import UTC, datetime
    from uuid import uuid4

    from audit.schemas import AuditEventCreate

    from .audit_events import _assert_no_api_key

    inputs_meta: dict[str, Any] = {"action": action}
    if fingerprint is not None:
        # The fingerprint is not key material: it is a truncated digest whose
        # only use is telling one saved key from another.
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
    except Exception:  # pragma: no cover - auditing must not break a save
        logger.warning("TypeSafe credential audit event failed", exc_info=True)


async def record_discarded(
    recorder: Any, user_id: str, *, auth_principal: Optional[str] = None
) -> None:
    """Audit an undecryptable row the store discarded."""
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
