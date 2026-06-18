# app/database/models/psychologist.py
from decimal import Decimal
from sqlalchemy import String, Numeric, ForeignKey, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class Psychologist(Base):
    """Stores professional information, pricing rates, and calendar sync configuration for psychologists."""
    __tablename__ = "psychologists"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    photo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bio: Mapped[str] = mapped_column(Text, nullable=False)
    specializations: Mapped[str] = mapped_column(Text, nullable=False) # Comma-separated specs
    experience_years: Mapped[int] = mapped_column(nullable=False)
    
    # Pricing configuration
    price_online: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    price_offline: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    google_calendar_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="psychologists")
