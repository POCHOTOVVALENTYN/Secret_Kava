# app/database/models/booking.py
from datetime import datetime
from decimal import Decimal
from sqlalchemy import String, Numeric, ForeignKey, DateTime, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class Room(Base):
    """Represents a rentable physical therapeutic office space room."""
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    photo_urls: Mapped[str | None] = mapped_column(Text, nullable=True) # Comma-separated or JSON list
    equipment: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    hourly_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ConsultationBooking(Base):
    """Tracks appointments booked between a user client and a specific psychologist."""
    __tablename__ = "consultation_bookings"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    psychologist_id: Mapped[int] = mapped_column(ForeignKey("psychologists.id", ondelete="RESTRICT"), index=True, nullable=False)
    
    format: Mapped[str] = mapped_column(String(16), nullable=False)  # "online" or "offline"
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending, paid, confirmed, cancelled
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    
    google_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    
    # Relationships
    user: Mapped["User"] = relationship(back_populates="consultations")
    psychologist: Mapped["Psychologist"] = relationship()
    payment: Mapped["Payment"] = relationship(back_populates="consultation", uselist=False, cascade="all, delete-orphan")


class RoomBooking(Base):
    """Tracks room rental reservations made by clients or therapists."""
    __tablename__ = "room_bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="RESTRICT"), index=True, nullable=False)

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False) # pending, paid, cancelled
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    google_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="room_bookings")
    room: Mapped["Room"] = relationship()
    payment: Mapped["Payment"] = relationship(back_populates="room_booking", uselist=False, cascade="all, delete-orphan")


class EventBooking(Base):
    """Tracks registrations to studio events made by clients."""
    __tablename__ = "event_bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    
    event_id: Mapped[int] = mapped_column(nullable=False)
    event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    
    client_name: Mapped[str] = mapped_column(String(128), nullable=False)
    client_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False) # pending, paid, cancelled
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    google_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="event_bookings")
    payment: Mapped["Payment"] = relationship(back_populates="event_booking", uselist=False, cascade="all, delete-orphan")


class SpecialistSlot(Base):
    """Stores available slots configured by/for specialists."""
    __tablename__ = "specialist_slots"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    psychologist_id: Mapped[int] = mapped_column(ForeignKey("psychologists.id", ondelete="CASCADE"), index=True, nullable=False)
    date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)  # "YYYY-MM-DD"
    time: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"
    is_booked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    psychologist: Mapped["Psychologist"] = relationship()


class RoomRentalSlot(Base):
    """Stores available slots configured for room rentals (e.g. Головний кабінет)."""
    __tablename__ = "room_rental_slots"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True, nullable=False)
    date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)  # "YYYY-MM-DD"
    time: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"
    is_booked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    room: Mapped["Room"] = relationship()
