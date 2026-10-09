# Mesh Target Resolution and Presence Router
# Part of AstralDeep #281

from fastapi import APIRouter

from shared.mesh_targets_handlers import (
    router as mesh_targets_router,
)

# Mount mesh targets under /mesh
mesh_router = APIRouter(prefix="/mesh")
mesh_router.include_router(mesh_targets_router)

__all__ = ["mesh_router"]
