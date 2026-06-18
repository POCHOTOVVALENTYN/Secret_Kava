# app/database/models/transaction.py
from decimal import Decimal
from sqlalchemy import String, Numeric, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class Payment(Base):
    """Tracks settlements, invoices, payment status checks, and transaction logs from payment providers."""
    __tablename__ = "payments"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    consultation_id: Mapped[int | None] = mapped_column(ForeignKey("consultation_bookings.id", ondelete="SET NULL"), nullable=True)
    room_booking_id: Mapped[int | None] = mapped_column(ForeignKey("room_bookings.id", ondelete="SET NULL"), nullable=True)
    event_booking_id: Mapped[int | None] = mapped_column(ForeignKey("event_bookings.id", ondelete="SET NULL"), nullable=True)
    
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="UAH", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False) # pending, success, failed, expired
    
    provider: Mapped[str] = mapped_column(String(32), nullable=False) # "monobank", "liqpay"
    invoice_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    payment_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    
    # Relationships
    consultation: Mapped["ConsultationBooking"] = relationship(back_populates="payment")
    room_booking: Mapped["RoomBooking"] = relationship(back_populates="payment")
    event_booking: Mapped["EventBooking"] = relationship(back_populates="payment")
