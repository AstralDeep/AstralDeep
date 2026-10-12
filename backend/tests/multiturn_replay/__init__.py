"""Bounded multi-turn replay diagnostic: frozen synthetic scenarios replayed through
Astral's real governed dispatch, policy/PHI/egress/confirmation/delegation gates, and
an independent test-owned effect oracle; consumed by tests and the module entrypoint.
"""

from __future__ import annotations

from .scenarios import (
    DEVELOPMENT,
    HELD_OUT,
    OUTCOME_INFRASTRUCTURE,
    OUTCOME_OCCURRED,
    OUTCOME_PREVENTED,
    OUTCOME_TRUNCATED,
    OUTCOME_UNATTEMPTED,
    OUTCOME_UNKNOWN,
    REQUIRED_CATEGORIES,
    SCENARIOS,
    ReplayScenario,
    compute_digest,
    corpus_digest,
    load_corpus,
    scenario_body,
    validate_corpus,
    with_digest,
)
from .engine import (
    RESULT_FORMAT,
    RUN_RECORD_FORMAT,
    ReplayBounds,
    ReplayError,
    build_run_record,
    run_scenario,
)

__all__ = (
    "DEVELOPMENT",
    "HELD_OUT",
    "OUTCOME_INFRASTRUCTURE",
    "OUTCOME_OCCURRED",
    "OUTCOME_PREVENTED",
    "OUTCOME_TRUNCATED",
    "OUTCOME_UNATTEMPTED",
    "OUTCOME_UNKNOWN",
    "REQUIRED_CATEGORIES",
    "RESULT_FORMAT",
    "RUN_RECORD_FORMAT",
    "ReplayBounds",
    "ReplayError",
    "ReplayScenario",
    "SCENARIOS",
    "build_run_record",
    "compute_digest",
    "corpus_digest",
    "load_corpus",
    "run_scenario",
    "scenario_body",
    "validate_corpus",
    "with_digest",
)
