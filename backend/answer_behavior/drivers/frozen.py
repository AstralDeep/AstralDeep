"""Frozen driver replaying the corpus's per-actor stored transcripts, giving CI a
deterministic provider double with no network, credentials, or clock dependence.
"""

from __future__ import annotations

from answer_behavior.drivers.base import Driver
from answer_behavior.models import ACTIONS, AnswerTranscript, BehaviorCase


class FrozenDriver(Driver):
    mode = "frozen"

    def __init__(self, actor: str = "candidate") -> None:
        if actor not in ACTIONS:
            raise ValueError(f"unknown actor: {actor!r} (expected one of {ACTIONS})")
        self.actor = actor

    def run_case(self, case: BehaviorCase) -> AnswerTranscript:
        transcript = case.frozen_answers.get(self.actor)
        if transcript is None:
            raise ValueError(f"{case.case_id}: no frozen answer for actor {self.actor!r}")
        return transcript
