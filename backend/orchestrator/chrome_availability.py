"""Deep-owned host policy inputs for the pure Projection chrome model.

AstralProjection owns the menu vocabulary and rendering, but it must not import
Deep's feature-flag implementation.  Resolve the host-controlled capability
switches here and pass the resulting booleans into every Projection delivery
channel so web, REST, and WebSocket chrome cannot drift.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Orchestrator.ChromeAvailability")


def projection_chrome_availability() -> dict[str, bool]:
    """Return fail-closed host inputs for the Projection chrome model."""

    pulse = False
    byo = False
    remote = False
    try:
        from dreaming.pulse import pulse_enabled

        pulse = bool(pulse_enabled())
    except Exception:
        logger.warning("Unable to resolve Pulse chrome availability; hiding it", exc_info=True)
    try:
        from shared.feature_flags import flags

        byo = bool(flags.is_enabled("byo_agents"))
        remote = bool(flags.is_enabled("remote_compute"))
    except Exception:
        logger.warning("Unable to resolve agent chrome availability; hiding it", exc_info=True)
    return {
        "pulse_enabled": pulse,
        "byo_enabled": byo,
        "remote_enabled": remote,
    }
