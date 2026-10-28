"""Seals final-holdout exposure as an atomic qualification run through AstralPlane.
Only run identities and digests are retained, and interrupted runs keep their exposure receipt.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.quality_audit import QualityTestRunRecord

from prompt_optimization.fixture import Fixture, render_case_block

OWNER_ID = "prompt-optimization-scripted"


class HoldoutReuseError(RuntimeError):
    pass


class PlaneHoldoutSeals:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.repository = runtime.repositories.quality_audit

    @staticmethod
    def input_digests(fixture: Fixture) -> tuple[str, ...]:
        return tuple(sorted(hashlib.sha256(render_case_block(case).encode("utf-8")).hexdigest()
                            for case in fixture.cases("holdout")))

    @classmethod
    def receipt_ids(cls, fixture: Fixture) -> tuple[str, ...]:
        return tuple(str(uuid.uuid5(uuid.NAMESPACE_URL,
                                   "astraldeep:prompt-optimization:final-input:" + digest))
                     for digest in cls.input_digests(fixture))

    def assert_available(self, fixture: Fixture) -> None:
        with self.runtime.transaction() as transaction:
            for run_id in self.receipt_ids(fixture):
                existing = self.repository.get_run(transaction, owner_id=OWNER_ID, run_id=run_id)
                if existing is not None:
                    raise HoldoutReuseError("final input already exposed; designate an untouched final hold-out")

    def seal(self, fixture: Fixture, optimization_run_id: str, *, context_digest: str) -> None:
        if type(context_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", context_digest) is None:
            raise ValueError("evaluation context digest must be SHA-256")
        started_at = datetime.now(timezone.utc)
        claim_id = str(uuid.uuid4())
        try:
            with self.runtime.transaction() as transaction:
                for run_id, input_digest in zip(self.receipt_ids(fixture), self.input_digests(fixture)):
                    record = QualityTestRunRecord(
                        owner_id=OWNER_ID, run_id=run_id, started_at=started_at, finished_at=None,
                        system_state={"optimization_run_id": optimization_run_id,
                                      "fixture_digest": fixture.digest,
                                      "holdout_digest": fixture.holdout_input_digest,
                                      "input_digest": input_digest, "claim_id": claim_id,
                                      "evaluation_context_digest": context_digest},
                        categories=("prompt-optimization-final-input",), status="running",
                    )
                    self.repository.create_run(transaction, record)
        except RepositoryConflictError:
            raise HoldoutReuseError("final input already exposed; designate an untouched final hold-out") from None

    def finish(self, fixture: Fixture, *, succeeded: bool) -> None:
        with self.runtime.transaction() as transaction:
            for run_id in self.receipt_ids(fixture):
                self.repository.finish_run(transaction, owner_id=OWNER_ID, run_id=run_id,
                                           status="completed" if succeeded else "failed",
                                           finished_at=datetime.now(timezone.utc))
