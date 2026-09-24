"""Pydantic models for the qualification audit trail: test runs, case results, evidence,
audit-chain entries, and LaTeX artifacts, shared by cli.py, database.py, evidence.py,
latex_export.py, and runner.py.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Outcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class VerificationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    NEEDS_RERUN = "needs_rerun"


class AuditAction(str, Enum):
    VERIFIED = "verified"
    DISPUTED = "disputed"
    NEEDS_RERUN = "needs_rerun"


class TestRun(BaseModel):
    id: str = Field(default_factory=_uuid)
    started_at: datetime = Field(default_factory=_now)
    finished_at: Optional[datetime] = None
    system_state: Dict[str, Any] = Field(default_factory=dict)
    categories: List[str] = Field(default_factory=list)
    status: RunStatus = RunStatus.RUNNING


class TestCaseResult(BaseModel):
    id: str = Field(default_factory=_uuid)
    run_id: str
    suite: str
    test_name: str
    outcome: Outcome
    duration_ms: float = 0.0
    metrics: Dict[str, Any] = Field(default_factory=dict)
    qualitative: str = ""
    evidence_hash: str = ""
    verification_status: VerificationStatus = VerificationStatus.PENDING


class TestEvidence(BaseModel):
    id: str = Field(default_factory=_uuid)
    case_id: str
    evidence_type: str
    data: Dict[str, Any] = Field(default_factory=dict)
    sha256: str = ""
    captured_at: datetime = Field(default_factory=_now)


class AuditEntry(BaseModel):
    id: str = Field(default_factory=_uuid)
    case_id: str
    action: AuditAction
    reviewer: str
    rationale: str = ""
    timestamp: datetime = Field(default_factory=_now)
    previous_hash: str = ""


class LatexArtifact(BaseModel):
    id: str = Field(default_factory=_uuid)
    run_id: str
    filename: str
    generated_from: List[str] = Field(default_factory=list)
    verification_complete: bool = False
    generated_at: datetime = Field(default_factory=_now)
