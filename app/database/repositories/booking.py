# app/database/repositories/booking.py
from datetime import datetime
from typing import Sequence
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.booking import ConsultationBooking, RoomBooking
from app.database.repositories.base import BaseRepository

class BookingRepository(BaseRepository[ConsultationBooking]):
    """Manages scheduling checks and calendars overlays to prevent client double-bookings."""

    def __init__(self, session: AsyncSession):
        super().__init__(ConsultationBooking, session)

    async def get_by_user(self, user_id: int) -> Sequence[ConsultationBooking]:
        """Retrieves historical and pending consultation bookings associated with a client user."""
        query = select(ConsultationBooking).where(ConsultationBooking.user_id == user_id)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def check_overlap(self, psychologist_id: int, start: datetime, end: datetime) -> bool:
        """Verifies if selected date range clashes with already confirmed therapist sessions."""
        query = (
            select(ConsultationBooking)
            .where(ConsultationBooking.psychologist_id == psychologist_id)
            .where(ConsultationBooking.status.in_(["paid", "confirmed"]))
            .where(
                or_(
                    and_(ConsultationBooking.start_time <= start, ConsultationBooking.end_time > start),
                    and_(ConsultationBooking.start_time < end, ConsultationBooking.end_time >= end),
                    and_(ConsultationBooking.start_time >= start, ConsultationBooking.end_time <= end)
                )
            )
        )
        result = await self.session.execute(query)
        overlapping_record = result.scalars().first()
        return overlapping_record is not None
