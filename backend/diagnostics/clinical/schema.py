from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum

class DiagnosticStatus(Enum):
    SUCCESS = "success"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    INVALID_GRADING = "invalid_grading"
    BUDGET_EXHAUSTED = "budget_exhausted"

@dataclass(frozen=True)
class HealthBenchRubricItem:
    item_id: str
    category: str  # consultation, documentation, research, etc.
    criteria: str
    weight: float
    license_hash: str = "a5286ead1f03d178c7f7b134a5ac16944998428303012f0921814701e0f3995e"

@dataclass
class ClinicalDiagnosticResult:
    case_id: str
    score: float
    status: DiagnosticStatus
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_details: Optional[str] = None
    grader_identity: Optional[str] = None

@dataclass
class ClinicalDiagnosticConfig:
    max_concurrency: int = 5
    max_retries: int = 3
    elapsed_budget_seconds: int = 300
    cost_budget_usd: float = 10.0
    target_provider: str = "authorized_dispatcher"
