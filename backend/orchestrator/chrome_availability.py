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
    connections = False
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
        connections = bool(flags.is_enabled("framework_credentials"))
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
    values = {
        "pulse_enabled": pulse,
        "byo_enabled": byo,
        "remote_enabled": remote,
        "computer_enabled": computer,
        "skills_enabled": skills,
        "notes_enabled": True,
        **workspace,
    }
    if connections:
        # KNOWN GAP: AstralProjection's ``webrender.chrome.render_topbar`` (a
        # single-purpose web-topbar renderer with its own explicit keyword
        # list, not a **kwargs passthrough) does not yet accept
        # ``connections_enabled`` even though ``build_menu_model``/
        # ``menu_model_dict`` — which every other delivery channel uses — has
        # carried it since 088 T048. Every current caller of THIS function
        # spreads its return dict into one of three sinks: the REST
        # ``GET /api/chrome/menu`` and the native ``chrome_menu`` WS push (both
        # ``menu_model_dict``, both fine) and the web shell's inline
        # ``render_topbar(...)`` call, which raises ``TypeError`` on any
        # unrecognized keyword (caught there and logged, degrading the WHOLE
        # topbar to a bare shell — not a crash, but visibly broken for every
        # signed-in web user). Add the key ONLY when Connections is actually
        # enabled, so the byte-identical-when-off contract holds today and the
        # narrower flag-on web-topbar regression is confined to an operator
        # who has deliberately turned on this still-integrating feature.
        # Remove this guard once ``render_topbar`` accepts (and forwards)
        # ``connections_enabled`` like it already does ``notes_enabled``.
        values["connections_enabled"] = True
    return values


def projection_native_chrome_availability(claims: dict) -> dict[str, bool]:
    """Resolve native Work support from the current registered capability hint.

    This affects presentation only. The Work host separately authenticates the
    original JWT and current caller and never treats capability as permission.
    """
    values = projection_chrome_availability()
    capabilities = claims.get("_client_capabilities", []) if isinstance(claims, dict) else []
    values["work_enabled"] = bool(values.get("work_enabled", False) and isinstance(capabilities, list)
                                  and "work_read_v1" in capabilities)
    values["notes_enabled"] = bool(values.get("notes_enabled", False) and isinstance(capabilities, list)
                                   and "guidance_notes_v1" in capabilities)
    return values
