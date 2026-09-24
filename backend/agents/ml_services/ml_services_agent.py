#!/usr/bin/env python3
"""A2A agent consolidating CLASSify, Forecaster, and LLM-Factory behind one union tool
registry (mcp_server.py); composes its own Plane runtime via
orchestrator/plane_composition.py when run standalone.
"""
import asyncio
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.ml_services.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class MlServicesAgent(BaseA2AAgent):
    agent_id = "ml-services-1"
    service_name = "ML Services"
    description = (
        "Three external ML services you connect with your own keys: CLASSify "
        "trains and evaluates classifiers on tabular data, Forecaster fits and "
        "runs time-series forecasts, LLM-Factory routes chat, embeddings and "
        "transcription. Keys never leave your session in plaintext."
    )
    examples = [
        {"title": "Train a classifier",
         "prompt": "Submit the CSV I attached to CLASSify and walk me through "
                   "training a classifier on it"},
        {"title": "Forecast a series",
         "prompt": "Use Forecaster on the time series I attached and chart the next "
                   "12 periods"},
        {"title": "What can I run",
         "prompt": "List the models available through LLM-Factory"},
    ]
    skill_tags = ["machine-learning", "classification", "timeseries", "embeddings", "transcription"]

    # Old agent ids stay: decrypts credentials saved before consolidation
    predecessor_agent_ids = ("classify-1", "forecaster-1", "llm-factory-1")

    card_metadata = {
        "required_credentials": [
            {
                "key": "CLASSIFY_URL",
                "label": "CLASSify Service URL",
                "description": "Base URL of your CLASSify deployment (e.g. https://classify.ai.uky.edu/).",
                "required": False,
                "type": "api_key",
                "placeholder": "https://classify.ai.uky.edu/",
            },
            {
                "key": "CLASSIFY_API_KEY",
                "label": "CLASSify API Key",
                "description": "Personal API key issued by your CLASSify administrator. Used as a Bearer token.",
                "required": False,
                "type": "api_key",
            },
            {
                "key": "FORECASTER_URL",
                "label": "Forecaster Service URL",
                "description": "Base URL of your Timeseries Forecaster deployment (e.g. https://forecaster.ai.uky.edu/).",
                "required": False,
                "type": "api_key",
                "placeholder": "https://forecaster.ai.uky.edu/",
            },
            {
                "key": "FORECASTER_API_KEY",
                "label": "Forecaster API Key",
                "description": "Personal API key for the Forecaster service. Used as a Bearer token.",
                "required": False,
                "type": "api_key",
            },
            {
                "key": "LLM_FACTORY_URL",
                "label": "LLM-Factory Service URL",
                "description": "Base URL of your LLM-Factory Router deployment (e.g. https://llm-factory.ai.uky.edu/).",
                "required": False,
                "type": "api_key",
                "placeholder": "https://llm-factory.ai.uky.edu/",
            },
            {
                "key": "LLM_FACTORY_API_KEY",
                "label": "LLM-Factory API Key",
                "description": "Personal API key for the LLM-Factory Router. Used as a Bearer token on every request.",
                "required": False,
                "type": "api_key",
            },
        ],
        "long_running_tools": ["classify_start_training_job", "forecaster_start_training_job"],
    }

    def __init__(
        self,
        port: int = None,
        *,
        plane_runtime,
        plane_repositories=None,
        plane_blobs=None,
        attachment_materialization_service=None,
    ):
        repositories = plane_repositories or getattr(
            plane_runtime,
            "repositories",
            None,
        )
        if (
            plane_runtime is None
            or repositories is None
            or plane_blobs is None
            or attachment_materialization_service is None
        ):
            raise RuntimeError(
                "MlServicesAgent requires an initialized AstralPlane runtime "
                "catalog, blob store, and durable attachment publisher"
            )
        super().__init__(MCPServer(), port=port, port_env_var="ML_SERVICES_AGENT_PORT")

        from shared.attachment_materializer import (
            register_materialization_service as bind_materializer,
        )
        from shared.attachment_resolver import (
            register_plane_runtime as bind_resolver,
            unregister_plane_runtime as unbind_resolver,
        )

        resolver_binding_created = bind_resolver(
            plane_runtime,
            repositories,
            plane_blobs,
        )
        try:
            materializer_binding_created = bind_materializer(
                attachment_materialization_service
            )
        except BaseException:
            if resolver_binding_created:
                unbind_resolver(plane_runtime, repositories, plane_blobs)
            raise

        self._plane_runtime = plane_runtime
        self._plane_repositories = repositories
        self._plane_blobs = plane_blobs
        self._attachment_materialization_service = attachment_materialization_service
        self._resolver_binding_created = resolver_binding_created
        self._materializer_binding_created = materializer_binding_created

    def close_plane_bindings(self) -> None:
        from shared.attachment_materializer import unregister_materialization_service
        from shared.attachment_resolver import unregister_plane_runtime

        errors: list[BaseException] = []
        if self._materializer_binding_created:
            try:
                unregister_materialization_service(
                    self._attachment_materialization_service
                )
            except BaseException as exc:
                errors.append(exc)
            else:
                self._materializer_binding_created = False
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
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("MlServicesAgent Plane binding cleanup failed", errors)


def _compose_standalone_plane():
    from orchestrator.plane_composition import compose_plane_from_environment

    manifest = Path(__file__).resolve().parents[3] / "config" / "astral-composition.json"
    return compose_plane_from_environment(manifest)


async def _run_standalone(port: int | None) -> None:
    composition = _compose_standalone_plane()
    agent = None
    try:
        agent = MlServicesAgent(
            port=port,
            plane_runtime=composition.runtime,
            plane_repositories=composition.repositories,
            plane_blobs=composition.blobs,
            attachment_materialization_service=composition.attachment_materializer,
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
        raise BaseExceptionGroup("MlServicesAgent standalone Plane cleanup failed", errors)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ML Services Agent")
    parser.add_argument("--port", type=int, default=None, help="Port to run the agent on")
    args = parser.parse_args()

    asyncio.run(_run_standalone(args.port))
