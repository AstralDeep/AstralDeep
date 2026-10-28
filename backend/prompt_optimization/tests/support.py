"""Keeps synthetic qualification-run storage scoped to each workbench test.
Real PostgreSQL coverage separately verifies Plane's unique insert and transaction boundary.
"""

from prompt_optimization.holdout import PlaneHoldoutSeals
from prompt_optimization.tests.test_holdout_seals import _RunStorage

storage = _RunStorage()
holdout_seals = PlaneHoldoutSeals(storage)


def reset_holdouts():
    storage.runs.clear()
