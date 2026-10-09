import pytest
from unittest.mock import MagicMock
from backend.orchestrator.work_admission import WorkAdmission
from backend.shared.exceptions import EmergencyStopActiveException

@pytest.mark.asyncio
async def test_emergency_stop_prevents_admission():
    # Mock DB e Session
    mock_db = MagicMock()
    mock_stop = MagicMock()
    mock_stop.is_active = True
    
    # Configura o mock para retornar o stop ativo
    mock_db.query.return_value.filter.return_value.first.return_value = mock_stop
    
    admission = WorkAdmission(mock_db)
    
    with pytest.raises(EmergencyStopActiveException):
        await admission.check_emergency_stop("owner_123")

@pytest.mark.asyncio
async def test_resume_clears_stop():
    # Teste de lógica de persistência de resume...
    pass
