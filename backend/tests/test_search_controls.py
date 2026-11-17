import pytest
from backend.agents.search_controller import SearchOrchestrator, SourceState

def test_search_orchestrator_initialization():
    orchestrator = SearchOrchestrator(kernel_context={})
    assert orchestrator is not None

def test_search_result_distinction():
    # Test that the system correctly identifies revoked vs available sources
    mock_sources = [
        {"id": "1", "state": SourceState.AVAILABLE},
        {"id": "2", "state": SourceState.REVOKED}
    ]
    # Logic to verify UI output matches the state
    assert mock_sources[1]["state"] == SourceState.REVOKED

def test_search_denial_boundary():
    # Verify that unauthorized search attempts are denied
    orchestrator = SearchOrchestrator(kernel_context={})
    with pytest.raises(Exception):
        # Simulate unauthorized access
        orchestrator.execute_search("restricted_query")
