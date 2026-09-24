#!/usr/bin/env python3
"""The single grantable remote-compute agent, gated by FF_REMOTE_COMPUTE: unions
remote_observe's read verbs with remote_control's mutating verbs, the latter gated
per-verb by orchestrator/remote_confirmation.py.
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
    agent_id = "remote-compute-1"
    service_name = "Remote Compute"
    description = (
        "Works with the clusters and machines you have registered, over SSH: "
        "read the queue, job history, host facts, files and processes, then "
        "act — submit or cancel jobs, move files, control services. Anything "
        "destructive asks you first."
    )
    examples = [
        {"title": "What is running",
         "prompt": "Show the current job queue on my cluster with state and elapsed time"},
        {"title": "How busy is it",
         "prompt": "Show GPU and memory load per node on my cluster"},
        {"title": "Submit a job",
         "prompt": "Submit the batch script at ~/jobs/train.sbatch and tell me its job id"},
    ]
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
