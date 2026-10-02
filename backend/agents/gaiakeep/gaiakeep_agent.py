"""Registers GaiaKeep through the standard A2A runtime with Plane-backed machines and encrypted user credentials."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.credential_manager import CredentialManager
from shared.base_agent import BaseA2AAgent

from agents.gaiakeep.mcp_server import MCPServer


class GaiakeepAgent(BaseA2AAgent):
    agent_id = 'gaiakeep-1'
    service_name = 'GaiaKeep'
    description = ('Manage versioned GaiaKeep storage, provenance, jobs and site operations through '
                   'your registered SSH machine. Changes require human approval and Gaia roles.')
    skill_tags: ClassVar = ['storage', 'gaiakeep', 'remote', 'provenance', 'hpc']
    examples: ClassVar = [{'title': 'Storage state', 'prompt': 'Show GaiaKeep core status on my registered login node.'},
                {'title': 'Versioned files', 'prompt': 'List the files in my GaiaKeep version.'},
                {'title': 'Repair status', 'prompt': 'Show the status of my GaiaKeep repair job.'}]
    card_metadata: ClassVar = {'required_credentials': [
        {'key': 'GAIAKEEP_PRINCIPAL', 'label': 'GaiaKeep principal', 'type': 'api_key', 'required': True,
         'description': 'Your existing GaiaKeep principal identifier.'},
        {'key': 'GAIAKEEP_PRIVATE_KEY', 'label': 'GaiaKeep P-384 key (base64 PEM)', 'type': 'api_key', 'required': True,
         'description': 'Single-line base64 of your unencrypted P-384 PEM key; encrypted by the agent credential flow.'},
        {'key': 'GAIAKEEP_RECONSTRUCTION_TOKEN', 'label': 'Legacy reconstruction token', 'type': 'api_key',
         'required': False, 'description': 'An existing token for an operator-enabled legacy restore.'},
    ]}

    def __init__(self, port=None, *, plane_runtime, plane_repositories=None, plane_blobs=None):
        repositories = plane_repositories or getattr(plane_runtime, 'repositories', None)
        if plane_runtime is None or repositories is None or plane_blobs is None:
            raise RuntimeError('GaiaKeep requires the initialized application Plane runtime, repositories and blobs.')
        binding = SimpleNamespace(plane_runtime=plane_runtime, plane_repositories=repositories)
        credentials = CredentialManager(db=binding, plane_runtime=plane_runtime, plane_repositories=repositories)
        super().__init__(MCPServer(binding, credentials), port=port, port_env_var='GAIAKEEP_AGENT_PORT')
        self.host = '127.0.0.1'


def main():
    import argparse

    from orchestrator.plane_composition import compose_plane_from_environment
    from shared.feature_flags import flags

    if not flags.is_enabled('gaiakeep') or not flags.is_enabled('cresco'):
        raise SystemExit(os.EX_CONFIG if hasattr(os, 'EX_CONFIG') else 78)
    parser = argparse.ArgumentParser(description='GaiaKeep agent')
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args()
    composition = compose_plane_from_environment(Path(__file__).resolve().parents[3] / 'config' / 'astral-composition.json')
    try:
        agent = GaiakeepAgent(port=args.port, plane_runtime=composition.runtime,
                              plane_repositories=composition.repositories, plane_blobs=composition.blobs)
        asyncio.run(agent.run())
    finally:
        composition.close()


if __name__ == '__main__':
    main()
