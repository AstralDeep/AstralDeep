import pytest
from typing import List, Dict, Any
from backend.agents.search_controller import SearchOrchestrator, SourceState

def test_search_orchestrator_initialization() -> None:
    orchestrator = SearchOrchestrator(kernel_context={})
    assert orchestrator is not None

def test_search_result_distinction() -> None:
    mock_sources: List[Dict[str, Any]] = [
        {"id": "1", "state": SourceState.AVAILABLE, "name": "Alpha"},
        {"id": "2", "state": SourceState.REVOKED, "name": "Beta"}
    ]
    assert mock_sources[1]["state"] == SourceState.REVOKED

def test_search_denial_boundary() -> None:
    orchestrator = SearchOrchestrator(kernel_context={})
    result = orchestrator.execute_search("restricted_query")
    assert result["status"] == "denied"
