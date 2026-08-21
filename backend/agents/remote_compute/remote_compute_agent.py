#!/usr/bin/env python3
"""remote-compute-1 — the unified remote-compute agent (feature 063).

Reaches the user's own registered machines/clusters over SSH and exposes BOTH the
read-only verbs (queue, job status/history, host facts, directory/process listing,
reachability) and the mutating verbs (submit/cancel jobs, create/delete paths,
upload files, control services/packages, signal processes). Runs IN-PROCESS;
gated by FF_REMOTE_COMPUTE.

Safe-seeded so the read verbs work out of the box, but every DESTRUCTIVE mutating
verb is still gated per-verb by the durable confirmation mechanism
(``orchestrator/remote_confirmation.py``, keyed on this agent's id) — merging the
read + control agents did NOT merge their safety classes.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.remote_compute.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RemoteComputeAgent")


class RemoteComputeAgent(BaseA2AAgent):
    """Unified remote-compute agent: read + mutating verbs, one grantable agent."""

    agent_id = "remote-compute-1"
    service_name = "Remote Compute"
    description = ("Work with your registered clusters and machines over SSH — check "
                   "the queue, job status/history, host facts, files and processes, and "
                   "act: submit and cancel jobs, create and delete paths, upload files, "
                   "control services and packages, signal processes. Destructive "
                   "operations always ask you to confirm first.")
    skill_tags = ["remote", "cluster", "slurm", "hpc", "ssh", "control"]

    def __init__(
        self,
        port: int = None,
        *,
        plane_runtime,
        plane_repositories=None,
        plane_blobs=None,
    ):
        repositories = plane_repositories or getattr(
            plane_runtime, "repositories", None
        )
        if plane_runtime is None or repositories is None or plane_blobs is None:
            raise RuntimeError(
                "RemoteComputeAgent requires the initialized AstralPlane runtime, catalog, and blobs"
            )

        super().__init__(MCPServer(), port=port, port_env_var="REMOTE_COMPUTE_AGENT_PORT")
        # The verb libraries retain their small host-object API while every
        # durable call resolves a typed repository on the injected Plane runtime.
        # This binding owns no driver or pool.
        from orchestrator.credential_manager import CredentialManager
        from agents.remote_compute import mcp_tools

        binding = SimpleNamespace(
            plane_runtime=plane_runtime,
            plane_repositories=repositories,
        )
        credential_manager = CredentialManager(
            db=binding,
            plane_runtime=plane_runtime,
            plane_repositories=repositories,
        )
        mcp_tools.register_deps(binding, credential_manager, plane_blobs)


def _compose_standalone_plane():
    """Compose the one Plane runtime owned by a networked agent process."""

    from orchestrator.plane_composition import compose_plane_from_environment

    manifest = Path(__file__).resolve().parents[3] / "config" / "astral-composition.json"
    return compose_plane_from_environment(manifest)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Remote Compute Agent')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on')
    args = parser.parse_args()
    composition = _compose_standalone_plane()
    try:
        agent = RemoteComputeAgent(
            port=args.port,
            plane_runtime=composition.runtime,
            plane_repositories=composition.repositories,
            plane_blobs=composition.blobs,
        )
        asyncio.run(agent.run())
    finally:
        composition.close()
