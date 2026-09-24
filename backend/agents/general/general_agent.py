"""General Agent: FastAPI A2A server exposing the file-reading and system tools over
/agent (WebSocket MCP) plus A2A discovery endpoints; composes its own Plane runtime
via orchestrator/plane_composition.py when run networked rather than in-process.
"""

import asyncio
import os
import sys
import logging
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.general.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

DEFAULT_PORT = 8003


class GeneralAgent(BaseA2AAgent):
    agent_id = "general-1"
    service_name = "General Agent"
    description = (
        "Reads this host's CPU, memory and disk, searches Wikipedia and arXiv, "
        "does arithmetic, restyles the workspace, and reads the files you "
        "attach — documents, spreadsheets, slides, images and DICOM."
    )
    examples = [
        {"title": "Machine health",
         "prompt": "Show current system status with CPU and memory metrics"},
        {"title": "Look something up",
         "prompt": "Search Wikipedia and arXiv for retrieval-augmented generation "
                   "and summarize what you find"},
        {"title": "Read what I attached",
         "prompt": "Read the spreadsheet I attached and chart the totals by month"},
    ]

    def __init__(
        self,
        port: int = DEFAULT_PORT,
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
                "GeneralAgent requires the initialized AstralPlane runtime, catalog, and blobs"
            )

        super().__init__(MCPServer(), port=port)
        from agents.general.file_tools import (
            register_plane_dependencies,
            unregister_plane_dependencies,
        )
        from shared.attachment_resolver import register_plane_runtime

        file_binding_created = register_plane_dependencies(
            plane_runtime,
            repositories,
            plane_blobs,
        )
        try:
            resolver_binding_created = register_plane_runtime(
                plane_runtime,
                repositories,
                plane_blobs,
            )
        except BaseException:
            if file_binding_created:
                unregister_plane_dependencies(
                    plane_runtime,
                    repositories,
                    plane_blobs,
                )
            raise

        self._plane_runtime = plane_runtime
        self._plane_repositories = repositories
        self._plane_blobs = plane_blobs
        self._file_binding_created = file_binding_created
        self._resolver_binding_created = resolver_binding_created
        try:
            logging.getLogger("GeneralAgent").info(
                "file tools wired to the application AstralPlane runtime"
            )
        except BaseException:
            self.close_plane_bindings()
            raise

    def close_plane_bindings(self) -> None:
        from agents.general.file_tools import unregister_plane_dependencies
        from shared.attachment_resolver import unregister_plane_runtime

        errors: list[BaseException] = []
        if self._resolver_binding_created:
            try:
                unregister_plane_runtime(
                    self._plane_runtime,
                    self._plane_repositories,
                    self._plane_blobs,
                )
            except BaseException as exc:
                errors.append(exc)
            else:
                self._resolver_binding_created = False
        if self._file_binding_created:
            try:
                unregister_plane_dependencies(
                    self._plane_runtime,
                    self._plane_repositories,
                    self._plane_blobs,
                )
            except BaseException as exc:
                errors.append(exc)
            else:
                self._file_binding_created = False
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("GeneralAgent Plane binding cleanup failed", errors)


def _compose_standalone_plane():
    from orchestrator.plane_composition import compose_plane_from_environment

    manifest = Path(__file__).resolve().parents[3] / "config" / "astral-composition.json"
    return compose_plane_from_environment(manifest)


async def _run_standalone(port: int) -> None:
    composition = _compose_standalone_plane()
    agent = None
    try:
        agent = GeneralAgent(
            port=port,
            plane_runtime=composition.runtime,
            plane_repositories=composition.repositories,
            plane_blobs=composition.blobs,
        )
        await agent.run()
    finally:
        await _close_standalone_plane(agent, composition)


async def _close_standalone_plane(agent, composition) -> None:
    from orchestrator.runtime_composition import close_blocking_component

    errors: list[BaseException] = []
    try:
        await composition.attachment_materializer.close()
    except BaseException as exc:
        errors.append(exc)
    try:
        await close_blocking_component(composition.attachment_materializations.close)
    except BaseException as exc:
        errors.append(exc)
    try:
        await composition.attachment_purges.close()
    except BaseException as exc:
        errors.append(exc)
    if agent is not None:
        try:
            agent.close_plane_bindings()
        except BaseException as exc:
            errors.append(exc)
    try:
        await close_blocking_component(composition.close)
    except BaseException as exc:
        errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("GeneralAgent standalone Plane cleanup failed", errors)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='General Agent')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    asyncio.run(_run_standalone(args.port))
