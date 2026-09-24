"""In-process registry for the bundled first-party agents: discovers, instantiates, and
registers each one through the orchestrator's normal register_agent path so it
behaves like a networked agent. Used by orchestrator.py and start.py.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from typing import List, Optional

logger = logging.getLogger("LocalAgents")

BUILT_IN_AGENT_DIRS = (
    "connectors",
    "dice_roller",
    "general",
    "journal_review",
    "medical",
    "ml_services",
    "summarizer",
    "weather",
    "web_research",
)

_REMOTE_COMPUTE_AGENT_DIRS = (
    "remote_compute",
)

_COMPUTER_USE_AGENT_DIRS = (
    "computer_use",
)

FIRST_PARTY_PUBLIC_AGENT_IDS = (
    "connectors-1",
    "dice-roller-1",
    "general-1",
    "journal-review-1",
    "medical-1",
    "ml-services-1",
    "summarizer-1",
    "weather-1",
    "web-research-1",
    "remote-compute-1",
    "computer-use-1",
)


def _agents_root() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "agents")


def discover_built_in_agent_dirs(agents_root: Optional[str] = None) -> List[str]:
    root = agents_root or _agents_root()
    found = []
    for name in BUILT_IN_AGENT_DIRS:
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, f"{name}_agent.py")):
            found.append(name)
    return found


def _load_agent_class(dir_name: str):
    from shared.base_agent import BaseA2AAgent

    mod = importlib.import_module(f"agents.{dir_name}.{dir_name}_agent")
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, BaseA2AAgent) and obj is not BaseA2AAgent and obj.__module__ == mod.__name__:
            return obj
    return None


async def register_built_ins(orch) -> List[str]:
    from shared.protocol import RegisterAgent
    from shared import attachment_materializer, attachment_resolver

    runtime_composition = getattr(orch, "runtime_composition", None)
    plane = getattr(runtime_composition, "plane", None)
    plane_runtime = getattr(plane, "runtime", None)
    plane_repositories = getattr(plane, "repositories", None)
    plane_blobs = getattr(plane, "blobs", None)
    attachment_materializations = getattr(plane, "attachment_materializer", None)
    if (
        plane_runtime is None
        or plane_repositories is None
        or plane_blobs is None
        or attachment_materializations is None
    ):
        logger.error(
            "Feature 040: refusing in-process built-ins without the initialized "
            "application Plane runtime"
        )
        return []
    resolver_binding_created = False
    try:
        resolver_binding_created = attachment_resolver.register_plane_runtime(
            plane_runtime,
            plane_repositories,
            plane_blobs,
        )
        attachment_materializer.register_materialization_service(
            attachment_materializations,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Feature 040: refusing in-process built-ins because attachment "
            "persistence could not bind to Plane"
        )
        if resolver_binding_created:
            attachment_resolver.unregister_plane_runtime(
                plane_runtime,
                plane_repositories,
                plane_blobs,
            )
        return []

    registered: List[str] = []
    dirs = discover_built_in_agent_dirs()
    try:
        from shared.feature_flags import flags
        if flags.is_enabled("remote_compute"):
            root = _agents_root()
            for name in _REMOTE_COMPUTE_AGENT_DIRS:
                d = os.path.join(root, name)
                if (name not in dirs and os.path.isdir(d)
                        and os.path.exists(os.path.join(d, f"{name}_agent.py"))):
                    dirs.append(name)
    except Exception:  # noqa: BLE001
        logger.debug("Feature 063 flag check failed (non-fatal)", exc_info=True)
    try:
        from shared.feature_flags import flags
        if flags.is_enabled("computer_use"):
            root = _agents_root()
            for name in _COMPUTER_USE_AGENT_DIRS:
                d = os.path.join(root, name)
                if (name not in dirs and os.path.isdir(d)
                        and os.path.exists(os.path.join(d, f"{name}_agent.py"))):
                    dirs.append(name)
    except Exception:  # noqa: BLE001
        logger.debug("Feature 076 flag check failed (non-fatal)", exc_info=True)
    for dir_name in dirs:
        try:
            cls = _load_agent_class(dir_name)
            if cls is None:
                logger.warning("Feature 040: no BaseA2AAgent subclass found in '%s'", dir_name)
                continue
            parameters = inspect.signature(cls).parameters
            plane_kwargs = {}
            if "plane_runtime" in parameters:
                plane_kwargs["plane_runtime"] = plane_runtime
            if "plane_repositories" in parameters:
                plane_kwargs["plane_repositories"] = plane_repositories
            if "plane_blobs" in parameters:
                plane_kwargs["plane_blobs"] = plane_blobs
            if "attachment_materialization_service" in parameters:
                plane_kwargs["attachment_materialization_service"] = (
                    attachment_materializations
                )
            if "orchestrator" in parameters:
                plane_kwargs["orchestrator"] = orch
            agent = cls(
                **plane_kwargs
            )
            await orch.register_agent(
                None,
                RegisterAgent(agent_card=agent.card, api_key=os.getenv("AGENT_API_KEY") or None),
            )
            orch.local_agents[agent.card.agent_id] = agent
            registered.append(agent.card.agent_id)
        except Exception:  # noqa: BLE001
            logger.exception("Feature 040: failed to load built-in agent '%s' in-process", dir_name)
    if registered:
        logger.info("Feature 040: %d built-in agents registered in-process: %s",
                    len(registered), registered)
    return registered
