from fastapi import APIRouter, Depends, HTTPException, status
from backend.shared.auth import get_current_owner
from backend.models.emergency_stop import EmergencyStop
from backend.shared.database import get_db
from datetime import datetime

router = APIRouter(prefix="/emergency-control", tags=["Emergency Control"])

@router.post("/stop")
async def trigger_emergency_stop(
    reason: str,
    owner=Depends(get_current_owner),
    db=Depends(get_db)
):
    """
    Ativa o stop de emergência para o proprietário atual.
    Persiste antes de retornar sucesso para garantir atomicidade.
    """
    stop = db.query(EmergencyStop).filter(EmergencyStop.id == owner.id).first()
    
    if not stop:
        stop = EmergencyStop(id=owner.id, is_active=True, reason=reason)
        db.add(stop)
    else:
        stop.is_active = True
        stop.reason = reason
        stop.activated_at = datetime.utcnow()
        stop.deactivated_at = None

    try:
        db.commit()
        # Aqui dispararíamos um sinal de interrupção para agentes ativos (via Redis/PubSub)
        # para interromper shells e execuções em curso imediatamente.
        return {"status": "emergency_stop_activated", "owner": owner.id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to persist emergency stop")

@router.post("/resume")
async def resume_operations(
    owner=Depends(get_current_owner),
    db=Depends(get_db)
):
    """
    Apenas o proprietário autenticado pode limpar o stop.
    """
    stop = db.query(EmergencyStop).filter(EmergencyStop.id == owner.id).first()
    
    if not stop or not stop.is_active:
        return {"status": "no_active_stop_found"}

    stop.is_active = False
    stop.deactivated_at = datetime.utcnow()
    
    try:
        db.commit()
        return {"status": "operations_resumed", "owner": owner.id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to resume operations")
