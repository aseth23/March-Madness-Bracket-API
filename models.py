from sqlalchemy import Column, Integer, String, DateTime, UniqueConstraint, Boolean, Text
from sqlalchemy.sql import func
from db import Base

class Entry(Base):
    __tablename__ = "entries"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String, nullable=False)
    email = Column(String, nullable=False, index=True)
    username = Column(String, nullable=True)

    # Bracket + scoring
    bracket = Column(Text, nullable=True)          # JSON stored as text for now
    score = Column(Integer, nullable=False, default=0)
    locked = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("email", name="uq_entries_email"),
    )
