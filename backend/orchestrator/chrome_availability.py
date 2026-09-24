"""Resolves host-controlled capability flags (feature flags, dreaming pulse) into the
booleans AstralProjection's pure chrome-menu model consumes, so web/REST/WebSocket
delivery channels can't drift on available menu items.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Orchestrator.ChromeAvailability")


def projection_chrome_availability() -> dict[str, bool]:
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
        values["connections_enabled"] = True
    return values


def projection_native_chrome_availability(claims: dict) -> dict[str, bool]:
    values = projection_chrome_availability()
    capabilities = claims.get("_client_capabilities", []) if isinstance(claims, dict) else []
    values["work_enabled"] = bool(values.get("work_enabled", False) and isinstance(capabilities, list)
                                  and "work_read_v1" in capabilities)
    values["notes_enabled"] = bool(values.get("notes_enabled", False) and isinstance(capabilities, list)
                                   and "guidance_notes_v1" in capabilities)
    return values
