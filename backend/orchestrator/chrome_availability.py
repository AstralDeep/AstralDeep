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
    computer = False
    skills = False
    try:
        from dreaming.pulse import pulse_enabled

        pulse = bool(pulse_enabled())
    except Exception:
        logger.warning("Unable to resolve Pulse chrome availability; hiding it", exc_info=True)
    try:
        from shared.feature_flags import flags

        byo = bool(flags.is_enabled("byo_agents"))
        remote = bool(flags.is_enabled("remote_compute"))
        computer = bool(flags.is_enabled("computer_use"))
        skills = bool(flags.is_enabled("user_skills"))
    except Exception:
        logger.warning("Unable to resolve agent chrome availability; hiding it", exc_info=True)
    workspace = {}
    for output, feature in (("export_enabled", "artifact_export"),
                            ("share_enabled", "artifact_sharing"),
                            ("work_enabled", "persistent_agents")):
        try:
            from shared.feature_flags import flags

            workspace[output] = bool(flags.is_enabled(feature))
        except Exception:
            # An unavailable capability cannot hide an unrelated menu/control.
            workspace[output] = False
            logger.warning("Unable to resolve %s chrome availability; hiding it", feature, exc_info=True)
    return {
        "pulse_enabled": pulse,
        "byo_enabled": byo,
        "remote_enabled": remote,
        "computer_enabled": computer,
        "skills_enabled": skills,
        **workspace,
    }


def projection_native_chrome_availability(claims: dict) -> dict[str, bool]:
    """Resolve native Work support from the current registered capability hint.

    This affects presentation only. The Work host separately authenticates the
    original JWT and current caller and never treats capability as permission.
    """
    values = projection_chrome_availability()
    capabilities = claims.get("_client_capabilities", []) if isinstance(claims, dict) else []
    values["work_enabled"] = bool(values.get("work_enabled", False) and isinstance(capabilities, list)
                                  and "work_read_v1" in capabilities)
    return values
