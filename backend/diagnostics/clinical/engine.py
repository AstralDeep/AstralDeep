import asyncio
import time
from typing import List, Dict, Any
from .schema import ClinicalDiagnosticResult, DiagnosticStatus, ClinicalDiagnosticConfig, HealthBenchRubricItem
from backend.security.dispatcher import AuthorizedDispatcher
from backend.security.audit import AuditLogger

class ClinicalDiagnosticEngine:
    def __init__(self, dispatcher: AuthorizedDispatcher, audit_logger: AuditLogger, config: ClinicalDiagnosticConfig):
        self.dispatcher = dispatcher
        self.audit_logger = audit_logger
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    async def run_diagnostic(self, cases: List[Dict[str, Any]], rubrics: List[HealthBenchRubricItem]) -> List[ClinicalDiagnosticResult]:
        start_time = time.time()
        results = []
        
        tasks = [self._process_case(case, rubrics, start_time) for case in cases]
        
        # Use gather with return_exceptions=True to ensure one failure doesn't kill the batch
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for res in batch_results:
            if isinstance(res, Exception):
                # This should be caught within _process_case, but as a safety net:
                results.append(ClinicalDiagnosticResult(
                    case_id="unknown",
                    score=0.0,
                    status=DiagnosticStatus.INFRASTRUCTURE_FAILURE,
                    error_details=str(res)
                ))
            else:
                results.append(res)
                
        return results

    async def _process_case(self, case: Dict[str, Any], rubrics: List[HealthBenchRubricItem], start_time: float) -> ClinicalDiagnosticResult:
        case_id = case.get("id", "unknown")
        
        async with self._semaphore:
            # 1. Budget Checks
            elapsed = time.time() - start_time
            if elapsed > self.config.elapsed_budget_seconds:
                return ClinicalDiagnosticResult(case_id, 0.0, DiagnosticStatus.BUDGET_EXHAUSTED, error_details="Time budget exceeded")

            try:
                # 2. Dispatch via Authorized Gate (not in-process driver)
                # This ensures IAM, Tool/PHI, and Egress gates are applied
                response = await self.dispatcher.dispatch_with_audit(
                    payload={
                        "task": "clinical_evaluation",
                        "context": case["context"],
                        "rubrics": [r.__dict__ for r in rubrics]
                    },
                    identity=case.get("requester_identity"),
                    audit_context={"diagnostic_run_id": "clinical_v1_batch"}
                )

                # 3. Parse Grader Output
                # We expect a structured JSON from the grader. 
                # If it's invalid, it's an INFRASTRUCTURE_FAILURE, not a 0 score.
                if not response.get("valid_json"):
                    return ClinicalDiagnosticResult(
                        case_id, 0.0, DiagnosticStatus.INVALID_GRADING, 
                        error_details="Grader returned malformed JSON",
                        grader_identity=response.get("model_id")
                    )

                score = float(response.get("score", 0.0))
                
                # 4. Audit Log
                await self.audit_logger.log_event(
                    event_type="CLINICAL_DIAGNOSTIC_COMPLETED",
                    details={"case_id": case_id, "score": score, "status": "success"}
                )

                return ClinicalDiagnosticResult(
                    case_id=case_id,
                    score=score,
                    status=DiagnosticStatus.SUCCESS,
                    grader_identity=response.get("model_id"),
                    metadata={"rubric_applied": len(rubrics)}
                )

            except Exception as e:
                # Distinguish between clinical failure and infra failure
                # In a real implementation, we would catch specific Dispatcher/RateLimit errors
                return ClinicalDiagnosticResult(
                    case_id, 0.0, DiagnosticStatus.INFRASTRUCTURE_FAILURE, 
                    error_details=str(e)
                )
