"""Feature 054 — the mandatory first-run LLM provider-setup gate.

Server-authoritative delivery + lifecycle of the "Set up your AI provider"
dialog (spec FR-013..FR-016):

* :func:`push_setup_dialog` — push the mandatory dialog to one socket
  (register-time, or re-gate after a clear). Web receives a
  ``chrome_render`` modal in the no-close mandatory variant; native SDUI
  clients (Windows/Android/iOS/macOS) receive the existing ``chrome_surface``
  frame with its reserved ``mode`` field set to ``"mandatory"`` — no new
  frame types, no protocol-manifest change. The watch is chrome-free by
  design and is never pushed (it gets spoken guidance at the chat
  pre-flight instead, FR-017).
* :func:`unlock_after_save` — after a successful probe-gated save, close
  the gate on ALL of the user's connected sockets and render each gated
  socket's welcome canvas, without re-login (FR-015).
* :func:`regate_after_clear` — after a clear, immediately push the
  mandatory dialog to all of the user's sockets (FR-009: there is no
  default to revert to).

The dialog is only the UX half; the authoritative half is the set of
server-side refusals (chat pre-flight, ``chrome_events`` gate,
``component_action``) that key off :meth:`Orchestrator.llm_configured_for`
regardless of what any client renders. Sign-out is the one guaranteed
escape: ``/auth/logout`` routes are never gated and the mandatory dialog
carries a sign-out affordance (FR-013).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from orchestrator.work_admission import OperationState, StaleExecutionFenceError

logger = logging.getLogger("Orchestrator.LLMGate")

SURFACE_KEY = "llm"

_WATCH = "watch"


def _device_type(orch, websocket) -> str:
    try:
        prof = orch.rote.get_profile(websocket)
        return getattr(prof.device_type, "value", str(prof.device_type))
    except Exception:
        return "browser"


def _gated_map(orch) -> dict:
    """Per-socket gate marker: id(websocket) → True while the mandatory
    dialog is (believed) showing on that socket."""
    m = getattr(orch, "_ws_llm_gated", None)
    if m is None:
        m = {}
        orch._ws_llm_gated = m
    return m


def _user_sockets(orch, user_id: str) -> list:
    """All live UI sockets registered to ``user_id``."""
    out = []
    for ws, claims in list((getattr(orch, "ui_sessions", None) or {}).items()):
        if ((claims or {}).get("sub") or "legacy") == user_id:
            out.append(ws)
    return out


def _roles_for(orch, websocket) -> list:
    claims = (getattr(orch, "ui_sessions", None) or {}).get(websocket) or {}
    roles = list((claims.get("realm_access") or {}).get("roles") or [])
    for client in (claims.get("resource_access") or {}).values():
        roles.extend((client or {}).get("roles") or [])
    return roles


async def push_setup_dialog(orch, websocket, user_id: str) -> None:
    """Push the mandatory provider-setup dialog to one socket.

    Device-aware: web → mandatory ``chrome_render`` modal (no ✕,
    ``data-mandatory``, sign-out link); native SDUI →
    ``chrome_surface {mode:"mandatory"}`` with the first-run composition.
    The watch is skipped (chrome-free by design).
    """
    from orchestrator.projection_surfaces import llm as llm_surface

    dtype = _device_type(orch, websocket)
    if dtype == _WATCH:
        return
    roles = _roles_for(orch, websocket)
    # WHICH account is this? The dialog is per-user and server-side, so a user
    # signed in on a second device under a DIFFERENT Keycloak account otherwise
    # sees a bare "set up your AI provider" and concludes their config failed to
    # sync. Name the principal. (Claims only — no logic change.)
    claims = (getattr(orch, "ui_sessions", None) or {}).get(websocket) or {}
    principal = (claims.get("preferred_username") or claims.get("email") or "")
    params = {"first_run": True, "principal": principal}
    if dtype in ("windows", "android", "ios", "macos"):
        from shared.protocol import ChromeSurface
        comps = list(await llm_surface.components(orch, user_id, roles, params) or [])
        try:
            from rote.adapter import ComponentAdapter
            comps = ComponentAdapter.adapt(comps, orch.rote.get_profile(websocket))
        except Exception:
            logger.debug("llm_gate: ROTE adapt failed; sending unadapted", exc_info=True)
        await orch._safe_send(websocket, ChromeSurface(
            region="modal",
            surface_key=SURFACE_KEY,
            title=llm_surface.FIRST_RUN_TITLE,
            admin_only=False,
            components=comps,
            mode="mandatory",
        ).to_json())
    else:
        from shared.protocol import ChromeRender
        from webrender.chrome import render_modal_shell
        body = await llm_surface.render(orch, user_id, roles, params)
        await orch._safe_send(websocket, ChromeRender(
            region="modal",
            html=render_modal_shell(
                llm_surface.FIRST_RUN_TITLE, body, SURFACE_KEY, mandatory=True),
        ).to_json())
    _gated_map(orch)[id(websocket)] = True


async def _push_gate_close(orch, websocket) -> None:
    """Close the mandatory dialog on one socket.

    Web: empty ``chrome_render`` clears the modal region. Natives: the
    documented blank-``surface_key`` empty-components close instruction
    (today's reducers only honor the BLANK form) — the 054 client edits
    additionally clear the mandatory pin on this frame."""
    if _device_type(orch, websocket) in ("windows", "android", "ios", "macos"):
        from shared.protocol import ChromeSurface
        await orch._safe_send(websocket, ChromeSurface(
            region="modal", surface_key="", title="", admin_only=False,
            components=[], mode="replace").to_json())
    else:
        from shared.protocol import ChromeRender
        await orch._safe_send(websocket, ChromeRender(region="modal", html="").to_json())


async def _send_welcome(orch, websocket, user_id: str) -> None:
    """Render the welcome canvas that the gate suppressed at register time."""
    try:
        if orch._ws_active_chat.get(id(websocket)):
            return
        from orchestrator.welcome import welcome_components
        try:
            tools_avail = await asyncio.to_thread(
                orch.compute_tools_available_for_user, user_id)
        except Exception:
            tools_avail = True
        await orch.send_ui_render(
            websocket, welcome_components(tools_available=tools_avail), speak=False)
        orch._ws_welcome[id(websocket)] = True
    except Exception:  # non-fatal — an empty canvas is fine
        logger.debug("llm_gate: welcome render failed (non-fatal)", exc_info=True)


async def unlock_after_save(
    orch,
    user_id: str,
    *,
    coordinator: Any | None = None,
    fence: Any | None = None,
    completed_owner: Any | None = None,
    completed_operation_id: Any | None = None,
    deadline_at_monotonic: float | None = None,
) -> bool:
    """Close the gate on ALL of the user's sockets after a successful save.

    Returns ``True`` iff at least one socket was gated (the caller uses
    this to skip its own modal re-render — the unlock already replaced it).

    Feature 060 credential operations pass their complete execution fence and
    attempt deadline.  Each logical gate transition is authorized immediately
    before it mutates the marker, so a cancelled/stale provider worker cannot
    unlock a user after the durable terminal state won the race.
    """
    completed_authority = (
        completed_owner is not None or completed_operation_id is not None
    )
    if (completed_owner is None) != (completed_operation_id is None):
        raise ValueError(
            "completed_owner and completed_operation_id are required together"
        )
    if fence is not None and completed_authority:
        raise ValueError("unlock authority must be live-fence or completed")
    if coordinator is None and (fence is not None or completed_authority):
        raise ValueError("coordinator is required for operation unlock")
    if coordinator is not None and fence is None and not completed_authority:
        raise ValueError("operation unlock authority is required")

    async def _assert_unlock_authority() -> None:
        if (
            deadline_at_monotonic is not None
            and time.monotonic() >= deadline_at_monotonic
        ):
            raise TimeoutError("credential save deadline elapsed before unlock")
        if coordinator is not None:
            if fence is not None:
                await asyncio.to_thread(
                    coordinator.assert_current_execution, fence
                )
            else:
                operation = await asyncio.to_thread(
                    coordinator.query_operation,
                    owner=completed_owner,
                    operation_id=completed_operation_id,
                )
                if operation.state is not OperationState.COMPLETED:
                    raise StaleExecutionFenceError(
                        "credential save did not complete"
                    )
        if (
            deadline_at_monotonic is not None
            and time.monotonic() >= deadline_at_monotonic
        ):
            raise TimeoutError("credential save deadline elapsed before unlock")

    await _assert_unlock_authority()
    gated = _gated_map(orch)
    any_unlocked = False
    for ws in _user_sockets(orch, user_id):
        await _assert_unlock_authority()
        was_gated = gated.pop(id(ws), False)
        if not was_gated:
            continue
        try:
            # Closing the race between the logical marker mutation and the
            # first client frame: deadline loss restores the marker, so no
            # post-deadline close/navigation can be projected.
            await _assert_unlock_authority()
        except Exception:
            gated[id(ws)] = True
            raise
        any_unlocked = True
        try:
            await _push_gate_close(orch, ws)
            await _assert_unlock_authority()
            await _send_welcome(orch, ws, user_id)
            await _assert_unlock_authority()
        except Exception:
            # Fence/deadline failures are operation-authority outcomes, not a
            # best-effort delivery miss. Propagate them so the outer Save
            # terminal cannot become a late success.
            if coordinator is not None:
                raise
            logger.debug(
                "llm_gate: unlock push failed for one socket",
                exc_info=True,
            )
    return any_unlocked


async def regate_after_clear(orch, user_id: str) -> int:
    """Push the mandatory dialog to all of the user's sockets after a clear.

    Returns the number of sockets gated. Watch sockets are skipped (they
    get the spoken guidance at their next AI use instead)."""
    count = 0
    if not getattr(orch, "_ff_llm_first_run", True):
        # Kill switch: no mandatory pushes. Server-side refusals still gate.
        return 0
    for ws in _user_sockets(orch, user_id):
        try:
            await push_setup_dialog(orch, ws, user_id)
            if _device_type(orch, ws) != _WATCH:
                count += 1
        except Exception:
            logger.debug("llm_gate: re-gate push failed for one socket",
                         exc_info=True)
    return count


def clear_socket(orch, websocket) -> None:
    """Disconnect cleanup — forget the gate marker for a closed socket."""
    m = getattr(orch, "_ws_llm_gated", None)
    if m is not None:
        m.pop(id(websocket), None)
