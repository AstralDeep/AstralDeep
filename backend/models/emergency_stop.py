from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey
from backend.shared.database import Base

class EmergencyStop(Base):
    __tablename__ = "emergency_stops"

    id = Column(String, primary_key=True)  # owner_id
    is_active = Column(Boolean, default=False)
    activated_at = Column(DateTime, default=datetime.utcnow)
    deactivated_at = Column(DateTime, nullable=True)
    reason = Column(String, nullable=True)

    def __repr__(self):
        return f"<EmergencyStop(owner={self.id}, active={self.is_active})>"
