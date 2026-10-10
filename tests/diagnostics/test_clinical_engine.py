import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from backend.diagnostics.clinical.engine import ClinicalDiagnosticEngine
from backend.diagnostics.clinical.schema import ClinicalDiagnosticConfig, HealthBenchRubricItem, DiagnosticStatus

@pytest.mark.asyncio
async def test_diagnostic_success():
    # Setup Mocks
    mock_dispatcher = AsyncMock()
    mock_dispatcher.dispatch_with_audit.return_value = {
        "valid_json": True,
        "score": 0.85,
        "model_id": "gpt-4-clinical-judge"
    }
    mock_audit = AsyncMock()
    
    config = ClinicalDiagnosticConfig(max_concurrency=2)
    engine = ClinicalDiagnosticEngine(mock_dispatcher, mock_audit, config)
    
    cases = [{"id": "case_1", "context": "patient has fever", "requester_identity": "test_user"}]
    rubrics = [HealthBenchRubricItem(item_id="r1", category="consultation", criteria="accuracy", weight=1.0)]
    
    results = await engine.run_diagnostic(cases, rubrics)
    
    assert len(results) == 1
    assert results[0].score == 0.85
    assert results[0].status == DiagnosticStatus.SUCCESS
    assert results[0].grader_identity == "gpt-4-clinical-judge"

@pytest.mark.asyncio
async def test_diagnostic_infrastructure_failure_on_invalid_json():
    mock_dispatcher = AsyncMock()
    mock_dispatcher.dispatch_with_audit.return_value = {
        "valid_json": False,
        "model_id": "broken-grader"
    }
    mock_audit = AsyncMock()
    
    engine = ClinicalDiagnosticEngine(mock_dispatcher, mock_audit, ClinicalDiagnosticConfig())
    
    cases = [{"id": "case_fail", "context": "...", "requester_identity": "user"}]
    rubrics = []
    
    results = await engine.run_diagnostic(cases, rubrics)
    
    assert results[0].status == DiagnosticStatus.INVALID_GRADING
    assert results[0].score == 0.0

@pytest.mark.asyncio
async def test_budget_exhaustion():
    mock_dispatcher = AsyncMock()
    mock_audit = AsyncMock()
    
    # Config with 0 seconds budget to force exhaustion
    config = ClinicalDiagnosticConfig(elapsed_budget_seconds=0)
    engine = ClinicalDiagnosticEngine(mock_dispatcher, mock_audit, config)
    
    cases = [{"id": "case_timeout", "context": "...", "requester_identity": "user"}]
    results = await engine.run_diagnostic(cases, [])
    
    assert results[0].status == DiagnosticStatus.BUDGET_EXHAUSTED
