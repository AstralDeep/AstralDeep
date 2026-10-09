import logging
from datetime import datetime
from backend.models.emergency_stop import EmergencyStop
from backend.shared.exceptions import EmergencyStopActiveException

logger = logging.getLogger(__name__)

class WorkAdmission:
    def __init__(self, db_session):
        self.db = db_session

    async def check_emergency_stop(self, owner_id: str):
        """Verifica se existe um stop ativo para o proprietário."""
        stop = self.db.query(EmergencyStop).filter(EmergencyStop.id == owner_id).first()
        if stop and stop.is_active:
            logger.warning(f"Admission denied: Emergency stop active for owner {owner_id}")
            raise EmergencyStopActiveException(f"Emergency stop is active for owner {owner_id}")

    async def admit_work(self, work_request):
        """
        Admite um novo trabalho, verificando o gate de emergência.
        """
        await self.check_emergency_stop(work_request.owner_id)
        
        # Lógica de admissão existente...
        logger.info(f"Work admitted for owner {work_request.owner_id}")
        return True
