# AstralDeep Main Application
# Mesh Target Resolution and Presence Support
# Part of AstralDeep #281

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from shared.feature_flags import FeatureFlags
from shared.mesh_targets import MeshPresenceService

logger = logging.getLogger(__name__)

# Module-level service instance for injection
_mesh_service: MeshPresenceService | None = None


def get_mesh_service() -> MeshPresenceService:
    """Get the mesh presence service instance."""
    global _mesh_service
    if _mesh_service is None:
        _mesh_service = MeshPresenceService(enrollment_store={})
    return _mesh_service


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    logger.info("Starting AstralDeep application")
    yield
    logger.info("Shutting down AstralDeep application")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="AstralDeep",
        description="Astral system with mesh target resolution and presence",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Register mesh targets router
    from shared.mesh_targets_handlers import router as mesh_targets_router
    app.include_router(mesh_targets_router, prefix="/api/v1")

    # Feature flags
    @app.get("/api/v1/feature-flags")
    async def get_feature_flags() -> dict:
        return FeatureFlags.get_flags()

    return app


app = create_app()
