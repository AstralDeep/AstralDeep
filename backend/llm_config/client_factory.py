"""Pure factory that builds a per-call LLM client from a resolved
credential record (feature 054-byo-llm-setup; supersedes the feature-006
two-tier user→operator-default rule).

Decision rule (spec FR-019 / research.md R3):

* A **user-context** call (a live user WebSocket) resolves the caller's
  persisted :class:`~llm_config.user_store.PersistedLLMConfig`; absent ⇒
  :class:`LLMUnavailable` (the mandatory first-run gate).
* A **system-context** call (``websocket is None`` or a scheduled-turn
  ``VirtualWebSocket``) resolves the admin-managed system record; absent ⇒
  :class:`LLMUnavailable` (background features degrade honestly).

There is NO fallback in either direction — a user call never consumes the
system credential, a system call never consumes any user's credentials, and
no call may consume another user's record. The resolver
(``Orchestrator._resolve_llm_client_for``) picks the record + source; this
factory only materializes the client, so the no-fallback invariant is
structural rather than conditional.

The factory is pure and uncached, so a ``clear`` (which re-gates the user)
is observed on the very next call.
"""
from __future__ import annotations

from typing import Optional, Protocol, Tuple

from openai import OpenAI

from .types import CredentialSource, LLMUnavailable, ResolvedConfig


# Keep one provider attempt bounded even when the caller does not supply an
# override.  Retries are owned by ``Orchestrator._call_llm`` so the SDK must
# not multiply that budget underneath the orchestrator (the OpenAI SDK
# otherwise performs two additional attempts and uses a 600-second read
# timeout by default).
DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS = 60.0

# The in-repo sentinel for "no API key" — the OpenAI SDK refuses to build a
# client without SOME api_key value.
KEYLESS_API_KEY_SENTINEL = "not-needed"


def _strip_authorization(request) -> None:
    """Remove the SDK's synthesized bearer before the request goes out."""
    request.headers.pop("Authorization", None)


def _keyless_http_client():
    """A FRESH httpx client whose request hook strips the Authorization
    header.

    The OpenAI SDK insists on materializing ``Bearer <api_key>`` for every
    request, and neither an empty key (an illegal trailing-space header) nor
    a client-level ``Omit`` (the SDK only honors omission per-request) can
    suppress it — so the header is removed at the transport hook.

    A new client per call is deliberate: the SDK CLOSES an injected
    ``http_client`` when its short-lived ``OpenAI`` instance is finalized, so
    a module-level shared client gets closed by the first completed call and
    every later keyless call fails instantly with ``APIConnectionError``.
    Timeouts stay with the SDK's per-request settings.
    """
    import httpx

    return httpx.Client(event_hooks={"request": [_strip_authorization]})


def openai_auth_kwargs(api_key: str) -> dict:
    """Auth kwargs for an OpenAI-compatible client construction.

    A real key is passed through. An empty key (or the ``not-needed``
    sentinel) selects the keyless transport: the sentinel satisfies the
    SDK's constructor while the shared http client removes the
    ``Authorization`` header from the wire — keyless OpenAI-compatible
    servers (vLLM/sglang, local runtimes, the UK LLM factory) accept a
    missing bearer while rejecting an arbitrary wrong one with 401/403.
    """
    if api_key and api_key != KEYLESS_API_KEY_SENTINEL:
        return {"api_key": api_key}
    return {
        "api_key": KEYLESS_API_KEY_SENTINEL,
        "http_client": _keyless_http_client(),
    }


class LLMConfigLike(Protocol):
    """Duck-type of a resolved credential record: the decrypted
    ``PersistedLLMConfig`` (or any test double with the same fields)."""
    api_key: str
    base_url: str
    model: str


def build_llm_client(
    config: Optional[LLMConfigLike],
    source: CredentialSource,
    *,
    timeout: Optional[float] = None,
) -> Tuple[OpenAI, CredentialSource, ResolvedConfig]:
    """Build an :class:`OpenAI` client from the resolved credential record.

    Args:
        config: The decrypted record for this call's context — the caller's
            own persisted configuration (``source=USER``) or the deployment
            system record (``source=SYSTEM``). ``None`` means the context has
            no configuration.
        source: Which context the record belongs to; recorded as
            ``credential_source`` on the ``llm_call`` audit event.
        timeout: Optional per-request timeout in seconds. ``None`` selects the
            bounded 60-second product default.

    Returns:
        ``(client, source, resolved)`` — ``resolved`` carries the
        non-sensitive ``base_url`` / ``model`` for audit payloads.

    Raises:
        LLMUnavailable: When ``config`` is ``None`` — the documented
            fail-closed branch (first-run gate for users; honest skip for
            system work).
        ValueError: When ``source`` is the retired ``OPERATOR_DEFAULT``
            (no new call may carry it).
    """
    if source == CredentialSource.OPERATOR_DEFAULT:
        raise ValueError(
            "CredentialSource.OPERATOR_DEFAULT is retired (feature 054): "
            "the operator-default credential path no longer exists."
        )
    if config is None:
        raise LLMUnavailable(
            "No LLM configuration for this context: "
            + (
                "the user has not completed provider setup."
                if source == CredentialSource.USER
                else "no system credential has been configured by an admin."
            )
        )
    kwargs = {
        "base_url": config.base_url,
        "max_retries": 0,
        "timeout": (
            DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS
            if timeout is None
            else timeout
        ),
    }
    kwargs.update(openai_auth_kwargs(config.api_key))
    client = OpenAI(**kwargs)
    return (
        client,
        source,
        ResolvedConfig(base_url=config.base_url, model=config.model),
    )
