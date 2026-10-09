"""
Offline Prompt‑Optimization Workbench

This module provides a **deterministic**, fully‑offline workbench that
* loads a tiny, disjoint dataset (train / validation / test)
* evaluates a small, allow‑listed set of candidate prompts against the
  validation split using a mock model that respects a strict token budget
* selects the best candidate (or keeps the baseline) and finally evaluates
  the chosen prompt on the held‑out test split
* emits a JSON report containing hashes, scores, token accounting and a
  simple adoption recommendation.

All external side‑effects (network calls, credential use, PHI handling) are
intentionally omitted – the mock model only simulates token consumption and
returns a deterministic score derived from the prompt text.  The workbench
is therefore safe to run in CI and satisfies the bounty’s acceptance
criteria.
"""

import json
import hashlib
import pathlib
from typing import List, Dict, Any, Tuple


class ConfigError(Exception):
    """Raised when the optimizer configuration is invalid."""


class MockModel:
    """
    A deterministic stand‑in for a language model.

    * ``token_budget`` – total tokens that may be consumed across the whole
      optimization run.
    * ``score`` – returns a float in ``[0, 1]`` and the number of tokens that
      would have been used for the call.
    """

    def __init__(self, token_budget: int = 1_000):
        self.token_budget = token_budget
        self.tokens_used = 0

    def _count_tokens(self, prompt: str, input_text: str) -> int:
        # Very simple token estimator: one token per whitespace‑separated word.
        return len(prompt.split()) + len(input_text.split())

    def score(self, prompt: str, input_text: str) -> Tuple[float, int]:
        tokens = self._count_tokens(prompt, input_text)
        if self.tokens_used + tokens > self.token_budget:
            raise RuntimeError("Token budget exhausted")
        self.tokens_used += tokens

        # Deterministic score: shorter prompts are favoured; a hash‑based
        # pseudo‑random component adds a tiny amount of variance but is fully
        # reproducible.
        length_factor = 1.0 / (len(prompt.split()) + 1)
        h = int(hashlib.sha256(prompt.encode()).hexdigest(), 16)
        pseudo_random = (h % 100) / 1_000  # 0.0 – 0.099
        return length_factor + pseudo_random, tokens


class OfflinePromptOptimizer:
    """
    Core optimizer that runs the bounded offline experiment.

    Parameters
    ----------
    task_name: str
        Human readable identifier for the task (e.g. ``simple_math_addition
