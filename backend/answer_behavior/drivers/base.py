"""Driver contract for the answer-behavior suite: a driver produces one observed
AnswerTranscript per case, either by replaying frozen corpus answers or by driving a
live model through the bounded extraction path.
"""

from __future__ import annotations

import abc

from answer_behavior.models import AnswerTranscript, BehaviorCase


class Driver(abc.ABC):
    mode: str = ""

    @abc.abstractmethod
    def run_case(self, case: BehaviorCase) -> AnswerTranscript:
        raise NotImplementedError
