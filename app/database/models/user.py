# app/database/models/user.py
from sqlalchemy import String, Boolean, ForeignKey, BigInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class User(Base):
    """Stores information about Telegram bot clients, manager role allocations, and space association."""
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    
    # GDPR compliance column: AES encrypted string (optional integration)
    encrypted_phone: Mapped[str | None] = mapped_column(String(256), nullable=True)
    
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="client", nullable=False) # client, manager, admin, superadmin
    
    # Multi-Tenancy SaaS scope key
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    
    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="users")
    consultations: Mapped[list["ConsultationBooking"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    room_bookings: Mapped[list["RoomBooking"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    event_bookings: Mapped[list["EventBooking"]] = relationship(back_populates="user", cascade="all, delete-orphan")
