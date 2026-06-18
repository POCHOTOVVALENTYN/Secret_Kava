# app/database/models/tenant.py
from sqlalchemy import String, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class Tenant(Base):
    """Represents a separate Franchise psychology space (tenant) for SaaS white-label scaling."""
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Custom calendar or bot token mapping if white-labeled
    custom_bot_token: Mapped[str | None] = mapped_column(String(256), nullable=True)
    google_calendar_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Relationships
    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    psychologists: Mapped[list["Psychologist"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
