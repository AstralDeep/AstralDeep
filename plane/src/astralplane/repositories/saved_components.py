"""Re-exports repositories/workspaces.py's CanvasRepository under the stable public name
SavedComponentRepository, avoiding a second implementation or SQL path for the same
table.
"""

from __future__ import annotations

from astralplane.repositories.workspaces import CanvasComponentRecord, CanvasRepository

SavedComponentRecord = CanvasComponentRecord


class SavedComponentRepository(CanvasRepository):
    pass


__all__ = ("SavedComponentRecord", "SavedComponentRepository")
