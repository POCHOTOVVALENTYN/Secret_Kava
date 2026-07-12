# app/services/booking.py
from datetime import datetime, timedelta
import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from app.database.models.booking import ConsultationBooking, RoomBooking, EventBooking, SpecialistSlot, RoomRentalSlot
from app.database.models.psychologist import Psychologist
from app.database.models.user import User
from app.database.models.transaction import Payment
from app.integrations.google_cal import GoogleCalendarClient
from app.integrations.payments import WayForPayPaymentClient
from app.database.repositories.booking import BookingRepository
from app.integrations.google_sheets import GoogleSheetsClient
from app.scheduler.jobs import SchedulerService

logger = get_logger()

class BookingService:
    """Orchestrates database operations, Google Calendar checks, WayForPay invoices, and Redis locks."""

    def __init__(
        self,
        redis_client: redis.Redis,
        db_session: AsyncSession,
        gcal_client: GoogleCalendarClient | None = None,
        payment_client: WayForPayPaymentClient | None = None,
        sheets_client: GoogleSheetsClient | None = None,
        scheduler: SchedulerService | None = None
    ):
        self.redis = redis_client
        self.db = db_session
        self.gcal = gcal_client
        self.payment = payment_client
        self.sheets = sheets_client
        self.scheduler = scheduler
        self.booking_repo = BookingRepository(self.db)

    async def get_available_slots(self, psychologist_id: int, date_str: str) -> list[str]:
        """Calculates free time slots configured in the database, checking locks."""
        try:
            # Query slots for this psychologist and date from PostgreSQL
            query = select(SpecialistSlot).where(
                SpecialistSlot.psychologist_id == psychologist_id,
                SpecialistSlot.date == date_str,
                SpecialistSlot.is_booked == False
            ).order_by(SpecialistSlot.time.asc())
            
            result = await self.db.execute(query)
            slots = result.scalars().all()
            
            from zoneinfo import ZoneInfo
            kyiv_tz = ZoneInfo("Europe/Kyiv")
            now_kyiv = datetime.now(kyiv_tz)
            today_str = now_kyiv.strftime("%Y-%m-%d")
            
            available = []
            for s in slots:
                if date_str == today_str:
                    try:
                        slot_dt = datetime.strptime(f"{date_str} {s.time}", "%Y-%m-%d %H:%M").replace(tzinfo=kyiv_tz)
                        if slot_dt <= now_kyiv:
                            continue
                    except ValueError:
                        pass

                # Also check Redis lock status to prevent race conditions during checkout
                lock_key = f"lock:slot:{psychologist_id}:{date_str}:{s.time}"
                has_redis_lock = await self.redis.get(lock_key)
                
                if not has_redis_lock:
                    available.append(s.time)
                    
            return available
        except Exception as e:
            logger.error("failed_to_fetch_db_slots_falling_back_to_empty", error=str(e))
            return []

    async def get_available_room_dates(self, room_id: int, year: int, month: int) -> set[str]:
        """Returns set of date strings in YYYY-MM-DD format for a given room and month."""
        prefix = f"{year}-{month:02d}-"
        query = select(RoomRentalSlot.date).where(
            RoomRentalSlot.room_id == room_id,
            RoomRentalSlot.is_booked == False,
            RoomRentalSlot.date.like(f"{prefix}%")
        ).distinct()
        res = await self.db.execute(query)
        return set(res.scalars().all())

    async def get_available_room_slots(self, room_id: int, date_str: str) -> list[str]:
        """Returns list of free 'HH:MM' slot times for room_id on date_str, accounting for room/consultation overlaps."""
        query = select(RoomRentalSlot.time).where(
            RoomRentalSlot.room_id == room_id,
            RoomRentalSlot.date == date_str,
            RoomRentalSlot.is_booked == False
        ).order_by(RoomRentalSlot.time.asc())
        res = await self.db.execute(query)
        available_times = res.scalars().all()
        
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        kyiv_tz = ZoneInfo("Europe/Kyiv")
        now_kyiv = datetime.now(kyiv_tz)
        today_str = now_kyiv.strftime("%Y-%m-%d")
        
        free_times = []
        for t in available_times:
            if date_str == today_str:
                try:
                    slot_dt = datetime.strptime(f"{date_str} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=kyiv_tz)
                    if slot_dt <= now_kyiv:
                        continue
                except ValueError:
                    pass
            lock_key = f"lock:room_slot:{room_id}:{date_str}:{t}"
            if not await self.redis.get(lock_key):
                free_times.append(t)
                
        # Filter by overlapping confirmed RoomBookings and offline ConsultationBookings
        try:
            parsed_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return free_times
            
        from app.database.models.booking import RoomBooking, ConsultationBooking
        
        rb_query = select(RoomBooking).where(
            RoomBooking.status.in_(["confirmed", "paid"])
        )
        rb_res = await self.db.execute(rb_query)
        room_bookings = rb_res.scalars().all()
        
        cb_query = select(ConsultationBooking).where(
            ConsultationBooking.status.in_(["confirmed", "paid"]),
            ConsultationBooking.format == "offline"
        )
        cb_res = await self.db.execute(cb_query)
        consultations = cb_res.scalars().all()
        
        final_free_times = []
        for t_str in free_times:
            try:
                slot_time = datetime.strptime(t_str, "%H:%M").time()
            except ValueError:
                continue
            slot_dt_local = datetime.combine(parsed_date, slot_time, tzinfo=kyiv_tz)
            
            is_blocked = False
            
            # Check overlap with room bookings
            for rb in room_bookings:
                rb_start_local = rb.start_time.astimezone(kyiv_tz)
                rb_end_local = rb.end_time.astimezone(kyiv_tz)
                
                # Room 2 is blocked by any room booking. Room 1 is blocked by Room 1 or Room 2 booking.
                room_conflict = False
                if room_id == 2:
                    room_conflict = True
                elif room_id == 1:
                    if rb.room_id in (1, 2):
                        room_conflict = True
                        
                if room_conflict:
                    if slot_dt_local < rb_end_local and slot_dt_local + timedelta(hours=1) > rb_start_local:
                        is_blocked = True
                        break
                        
            if is_blocked:
                continue
                
            # Check overlap with offline consultations
            for cb in consultations:
                cb_start_local = cb.start_time.astimezone(kyiv_tz)
                cb_end_local = cb_start_local + timedelta(hours=1)
                
                if slot_dt_local < cb_end_local and slot_dt_local + timedelta(hours=1) > cb_start_local:
                    is_blocked = True
                    break
                    
            if not is_blocked:
                final_free_times.append(t_str)
                
        return final_free_times

    async def get_upcoming_fridays(self) -> list[dict]:
        """Returns next 4 Fridays with date strings and availability (based on 17 seats capacity)."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        kyiv_tz = ZoneInfo("Europe/Kyiv")
        now_local = datetime.now(kyiv_tz)
        
        fridays = []
        days_until_friday = (4 - now_local.weekday()) % 7
        
        if days_until_friday == 0 and now_local.hour >= 17:
            days_until_friday = 7
            
        start_friday = now_local.date() + timedelta(days=days_until_friday)
        
        from app.database.models.booking import EventBooking
        
        for i in range(4):
            friday_date = start_friday + timedelta(weeks=i)
            date_str = friday_date.strftime("%Y-%m-%d")
            event_start = datetime.combine(friday_date, datetime.strptime("17:00", "%H:%M").time())
            
            query = select(EventBooking).where(
                EventBooking.event_id == 99,
                EventBooking.start_time == event_start,
                EventBooking.status.in_(["paid", "confirmed"])
            )
            res = await self.db.execute(query)
            bookings_count = len(res.scalars().all())
            
            fridays.append({
                "date_str": date_str,
                "label": friday_date.strftime("%d.%m"),
                "booked_count": bookings_count,
                "available": bookings_count < 17
            })
            
        return fridays

    async def sync_specialist_slots_from_sheets(self) -> None:
        """Synchronizes slots from Google Sheets tab 'Вільні слоти консультації' into PostgreSQL database."""
        if not self.sheets:
            logger.warning("sheets_client_not_configured_skipping_slot_sync")
            return
            
        try:
            # 1. Read sheet rows
            rows = await self.sheets.read_sheet("Вільні слоти консультації")
            if not rows:
                logger.info("no_slots_found_in_sheet_or_empty")
                return
                
            # 2. Get all psychologists for name lookup
            psych_res = await self.db.execute(select(Psychologist))
            psychologists = psych_res.scalars().all()
            name_to_id = {p.name.strip().lower(): p.id for p in psychologists}
            
            # Map of existing slots from sheets to check deletions
            sheet_slot_keys = set()
            
            # Start at index 1 to skip header if it exists (e.g. check if first row is header)
            start_row = 0
            if len(rows) > 0 and ("дата" in str(rows[0][0]).lower() or "спеціаліст" in str(rows[0][1]).lower() if len(rows[0]) > 1 else False):
                start_row = 1
                
            for idx, row in enumerate(rows[start_row:], start_row + 1):
                if len(row) < 3:
                    continue
                    
                raw_date = str(row[0]).strip()
                raw_spec = str(row[1]).strip()
                raw_time = str(row[2]).strip()
                raw_status = str(row[3]).strip() if len(row) > 3 else "Вільний"
                
                # Normalize date
                parsed_date = None
                for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                    try:
                        parsed_date = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        pass
                if not parsed_date:
                    logger.warning("invalid_date_format_in_sheets_row", row=idx, date=raw_date)
                    continue
                    
                # Normalize time
                try:
                    parsed_time = datetime.strptime(raw_time, "%H:%M").strftime("%H:%M")
                except ValueError:
                    try:
                        parsed_time = datetime.strptime(raw_time, "%H.%M").strftime("%H:%M")
                    except ValueError:
                        logger.warning("invalid_time_format_in_sheets_row", row=idx, time=raw_time)
                        continue
                        
                spec_id = name_to_id.get(raw_spec.lower())
                if not spec_id:
                    logger.warning("psychologist_not_found_for_sheet_row", row=idx, name=raw_spec)
                    continue
                    
                is_booked = (raw_status.lower() in ("зайнятий", "заброньовано", "booked", "yes", "true", "1"))
                sheet_slot_keys.add((spec_id, parsed_date, parsed_time))
                
                # Check if slot already exists in DB
                slot_query = select(SpecialistSlot).where(
                    SpecialistSlot.psychologist_id == spec_id,
                    SpecialistSlot.date == parsed_date,
                    SpecialistSlot.time == parsed_time
                )
                slot_res = await self.db.execute(slot_query)
                slot = slot_res.scalars().first()
                
                # Check for active consultation bookings on this slot to prevent overbooking
                slot_start_time = datetime.strptime(f"{parsed_date} {parsed_time}", "%Y-%m-%d %H:%M")
                from app.database.models.booking import ConsultationBooking
                conflict_q = select(ConsultationBooking).where(
                    ConsultationBooking.psychologist_id == spec_id,
                    ConsultationBooking.start_time == slot_start_time,
                    ConsultationBooking.status.in_(["paid", "confirmed"])
                )
                conflict_res = await self.db.execute(conflict_q)
                if conflict_res.scalars().first() is not None:
                    is_booked = True
                
                if slot:
                    if slot.is_booked != is_booked:
                        slot.is_booked = is_booked
                        logger.info("updated_slot_status_from_sheets", psychologist_id=spec_id, date=parsed_date, time=parsed_time, is_booked=is_booked)
                else:
                    new_slot = SpecialistSlot(
                        psychologist_id=spec_id,
                        date=parsed_date,
                        time=parsed_time,
                        is_booked=is_booked
                    )
                    self.db.add(new_slot)
                    logger.info("inserted_slot_from_sheets", psychologist_id=spec_id, date=parsed_date, time=parsed_time, is_booked=is_booked)
                    
            await self.db.commit()
            
            # Delete any slots in PostgreSQL database that are NOT in Google Sheets (unless they are booked)
            all_slots_query = select(SpecialistSlot)
            all_slots_res = await self.db.execute(all_slots_query)
            all_slots = all_slots_res.scalars().all()
            
            deleted_count = 0
            for slot in all_slots:
                key = (slot.psychologist_id, slot.date, slot.time)
                if key not in sheet_slot_keys and not slot.is_booked:
                    await self.db.delete(slot)
                    deleted_count += 1
                    
            if deleted_count > 0:
                await self.db.commit()
                logger.info("deleted_removed_unbooked_slots_from_db", count=deleted_count)
                
        except Exception as e:
            logger.error("failed_to_sync_specialist_slots_from_sheets", error=str(e))
            await self.db.rollback()

    async def sync_room_rental_slots_from_sheets(self, room_id: int = 1) -> None:
        """Synchronizes room rental slots from Google Sheets tab into PostgreSQL."""
        if not self.sheets:
            logger.warning("sheets_client_not_configured_skipping_room_slot_sync")
            return
            
        sheet_name = "ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ" if room_id == 1 else "ВІЛЬНІ СЛОТИ ОРЕНДА НА ЗАХОДИ (АФІШИ)"
        try:
            # 1. Read sheet rows
            rows = await self.sheets.read_sheet(sheet_name)
            if not rows:
                logger.info("no_room_slots_found_in_sheet_or_empty", room_id=room_id)
                return
                
            sheet_slot_keys = set()
            
            # Start at index 1 to skip header if it exists
            start_row = 0
            if len(rows) > 0 and ("дата" in str(rows[0][0]).lower() or "час" in str(rows[0][1]).lower() if len(rows[0]) > 1 else False):
                start_row = 1
                
            for idx, row in enumerate(rows[start_row:], start_row + 1):
                if len(row) < 2:
                    continue
                    
                raw_date = str(row[0]).strip()
                raw_time = str(row[1]).strip()
                raw_status = str(row[2]).strip() if len(row) > 2 else "Вільний"
                
                # Normalize date
                parsed_date = None
                for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                    try:
                        parsed_date = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        pass
                if not parsed_date:
                    logger.warning("invalid_date_format_in_room_sheets_row", row=idx, date=raw_date, room_id=room_id)
                    continue
                    
                # Normalize time
                try:
                    parsed_time = datetime.strptime(raw_time, "%H:%M").strftime("%H:%M")
                except ValueError:
                    try:
                        parsed_time = datetime.strptime(raw_time, "%H.%M").strftime("%H:%M")
                    except ValueError:
                        logger.warning("invalid_time_format_in_room_sheets_row", row=idx, time=raw_time, room_id=room_id)
                        continue
                        
                is_booked = (raw_status.lower() in ("зайнятий", "заброньовано", "booked", "yes", "true", "1"))
                sheet_slot_keys.add((parsed_date, parsed_time))
                
                # Check if slot already exists in DB
                slot_query = select(RoomRentalSlot).where(
                    RoomRentalSlot.room_id == room_id,
                    RoomRentalSlot.date == parsed_date,
                    RoomRentalSlot.time == parsed_time
                )
                slot_res = await self.db.execute(slot_query)
                slot = slot_res.scalars().first()
                
                # Check for active bookings on this slot to prevent overbooking
                slot_start_time = datetime.strptime(f"{parsed_date} {parsed_time}", "%Y-%m-%d %H:%M")
                
                # 1. Check RoomBookings conflict
                from app.database.models.booking import RoomBooking
                rb_conflict_q = select(RoomBooking).where(
                    RoomBooking.room_id == room_id,
                    RoomBooking.start_time <= slot_start_time,
                    RoomBooking.end_time > slot_start_time,
                    RoomBooking.status.in_(["paid", "confirmed"])
                )
                rb_conflict_res = await self.db.execute(rb_conflict_q)
                has_active_rb = rb_conflict_res.scalars().first() is not None
                
                # 2. Check ConsultationBookings conflict (only for Room 1, offline format)
                has_active_cb = False
                if room_id == 1:
                    from app.database.models.booking import ConsultationBooking
                    cb_conflict_q = select(ConsultationBooking).where(
                        ConsultationBooking.start_time == slot_start_time,
                        ConsultationBooking.format == "offline",
                        ConsultationBooking.status.in_(["paid", "confirmed"])
                    )
                    cb_conflict_res = await self.db.execute(cb_conflict_q)
                    has_active_cb = cb_conflict_res.scalars().first() is not None
                    
                if has_active_rb or has_active_cb:
                    is_booked = True
                
                if slot:
                    if slot.is_booked != is_booked:
                        slot.is_booked = is_booked
                        logger.info("updated_room_slot_status_from_sheets", date=parsed_date, time=parsed_time, is_booked=is_booked, room_id=room_id)
                else:
                    new_slot = RoomRentalSlot(
                        room_id=room_id,
                        date=parsed_date,
                        time=parsed_time,
                        is_booked=is_booked
                    )
                    self.db.add(new_slot)
                    logger.info("inserted_room_slot_from_sheets", date=parsed_date, time=parsed_time, is_booked=is_booked, room_id=room_id)
                    
            await self.db.commit()
            
            # Delete any slots in PostgreSQL database that are NOT in Google Sheets (unless they are booked)
            all_slots_query = select(RoomRentalSlot).where(RoomRentalSlot.room_id == room_id)
            all_slots_res = await self.db.execute(all_slots_query)
            all_slots = all_slots_res.scalars().all()
            
            deleted_count = 0
            for slot in all_slots:
                key = (slot.date, slot.time)
                if key not in sheet_slot_keys and not slot.is_booked:
                    await self.db.delete(slot)
                    deleted_count += 1
                    
            if deleted_count > 0:
                await self.db.commit()
                logger.info("deleted_removed_unbooked_room_slots_from_db", count=deleted_count, room_id=room_id)
                
        except Exception as e:
            logger.error("failed_to_sync_room_slots_from_sheets", error=str(e), room_id=room_id)
            await self.db.rollback()

    async def lock_room_rental_slots(self, room_id: int, date_str: str, time_str: str, duration_hours: int, user_id: int) -> bool:
        """Acquires a distributed Redis Lock for consecutive slots to prevent double-booking slot races."""
        try:
            start_dt = datetime.strptime(time_str, "%H:%M")
            locked_keys = []
            for hour_offset in range(duration_hours):
                current_time_str = (start_dt + timedelta(hours=hour_offset)).strftime("%H:%M")
                lock_key = f"lock:room_slot:{room_id}:{date_str}:{current_time_str}"
                
                # Attempt to set distributed lock key if not exists (NX), expiring in 15 minutes (900s)
                acquired = await self.redis.set(lock_key, user_id, nx=True, ex=900)
                if acquired:
                    locked_keys.append(lock_key)
                else:
                    # Rollback previous locks on failure
                    for k in locked_keys:
                        await self.redis.delete(k)
                    logger.warning("room_slot_lock_failed", key=lock_key, client=user_id)
                    return False
            logger.info("room_slots_locked", keys=locked_keys, client=user_id)
            return True
        except Exception as e:
            logger.error("failed_to_lock_room_slots", error=str(e))
            return False

    async def lock_time_slot(self, psychologist_id: int, date_str: str, time_str: str, user_id: int) -> bool:
        """Acquires a distributed Redis Lock to prevent double-booking slot races."""
        lock_key = f"lock:slot:{psychologist_id}:{date_str}:{time_str}"
        # Set distributed lock key if not exists (NX), expiring in 15 minutes (900s)
        acquired = await self.redis.set(lock_key, user_id, nx=True, ex=900)
        if acquired:
            logger.info("slot_locked", key=lock_key, client=user_id)
            return True
        return False

    async def calculate_price(self, psychologist_id: int, booking_format: str) -> float:
        """Fetches pricing configuration based on formats chosen."""
        psych_query = select(Psychologist).where(Psychologist.id == psychologist_id)
        psych_res = await self.db.execute(psych_query)
        psychologist = psych_res.scalar_one_or_none()
        
        if not psychologist:
            return 1000.00 # Standard fallback price
            
        if booking_format == "online":
            return float(psychologist.price_online)
        return float(psychologist.price_offline)

    async def create_host_event_invoice(
        self,
        user_id: int,
        title: str,
        host: str,
        date_str: str,
        time_str: str,
        hours: int,
        limit: int,
        price: float,
        client_name: str,
        client_phone: str
    ) -> tuple[str, str]:
        """Generates WayForPay payment link for hosting event request (prepayment of 50 UAH)."""
        import uuid
        prepay_amount = 50.0
        description = f"Передплата за проведення заходу (50 грн): «{title}»"
        if not self.payment:
            mock_id = str(uuid.uuid4())
            invoice_url, invoice_id = f"https://checkout.wayforpay.com/pay/mock_host_{mock_id}", mock_id
        else:
            order_id = f"he_{uuid.uuid4().hex[:12]}"
            invoice_url, invoice_id = await self.payment.create_invoice(prepay_amount, order_id, client_name, product_name=description)
            
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(hours=hours)

        new_booking = RoomBooking(
            user_id=user_id,
            room_id=2,
            start_time=start_dt,
            end_time=end_dt,
            status="pending",
            price=prepay_amount
        )
        self.db.add(new_booking)
        await self.db.flush()

        new_payment = Payment(
            room_booking_id=new_booking.id,
            amount=prepay_amount,
            currency="UAH",
            status="pending",
            provider="wayforpay",
            invoice_id=invoice_id,
            payment_details={
                "type": "host_event",
                "title": title,
                "host": host,
                "date": date_str,
                "time": time_str,
                "hours": hours,
                "limit": limit,
                "price": price,
                "client_name": client_name,
                "client_phone": client_phone
            }
        )
        self.db.add(new_payment)
        await self.db.flush()
        
        return invoice_url, invoice_id

    async def create_consultation_invoice(
        self, 
        user_id: int,
        psychologist_id: int, 
        format_type: str, 
        date_str: str, 
        time_str: str, 
        price: float, 
        client_name: str,
        client_phone: str
    ) -> tuple[str, str]:
        """Generates dynamic checkout page URL using WayForPay Client API and saves pending booking."""
        prepay_amount = 100.0
        description = f"Передплата 100 грн: Консультація з психологом (ID #{psychologist_id}, {format_type})"
        
        import uuid
        if not self.payment:
            mock_id = str(uuid.uuid4())
            invoice_url, invoice_id = f"https://checkout.wayforpay.com/pay/mock_{mock_id}", mock_id
        else:
            order_id = f"cb_{uuid.uuid4().hex[:12]}"
            invoice_url, invoice_id = await self.payment.create_invoice(prepay_amount, order_id, client_name, product_name=description)
            
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(hours=1)
        
        new_booking = ConsultationBooking(
            user_id=user_id,
            psychologist_id=psychologist_id,
            format=format_type,
            start_time=start_dt,
            end_time=end_dt,
            status="pending",
            price=price
        )
        self.db.add(new_booking)
        await self.db.flush()
        
        new_payment = Payment(
            consultation_id=new_booking.id,
            amount=prepay_amount,
            currency="UAH",
            status="pending",
            provider="wayforpay",
            invoice_id=invoice_id,
            payment_details={
                "client_name": client_name,
                "client_phone": client_phone
            }
        )
        self.db.add(new_payment)
        await self.db.flush()
        
        return invoice_url, invoice_id

    async def create_rental_invoice(
        self,
        user_id: int,
        room_id: int,
        date_str: str,
        time_str: str,
        hours: int,
        price: float,
        client_name: str,
        client_phone: str
    ) -> tuple[str, str]:
        prepay_amount = 50.0
        description = f"Передплата 50 грн: Оренда кабінету #{room_id} на {hours} год."
        
        import uuid
        if not self.payment:
            mock_id = str(uuid.uuid4())
            invoice_url, invoice_id = f"https://checkout.wayforpay.com/pay/mock_{mock_id}", mock_id
        else:
            order_id = f"rb_{uuid.uuid4().hex[:12]}"
            invoice_url, invoice_id = await self.payment.create_invoice(prepay_amount, order_id, client_name, product_name=description)
            
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(hours=hours)
        
        new_booking = RoomBooking(
            user_id=user_id,
            room_id=room_id,
            start_time=start_dt,
            end_time=end_dt,
            status="pending",
            price=price
        )
        self.db.add(new_booking)
        await self.db.flush()
        
        new_payment = Payment(
            room_booking_id=new_booking.id,
            amount=prepay_amount,
            currency="UAH",
            status="pending",
            provider="wayforpay",
            invoice_id=invoice_id,
            payment_details={
                "client_name": client_name,
                "client_phone": client_phone
            }
        )
        self.db.add(new_payment)
        await self.db.flush()
        
        return invoice_url, invoice_id

    async def create_event_invoice(
        self,
        user_id: int,
        event_id: int,
        event_name: str,
        date_str: str,
        price: float,
        client_name: str,
        client_phone: str,
        prepay_amount: float = 1.0
    ) -> tuple[str, str]:
        """Generates dynamic checkout page URL using WayForPay API and saves pending event booking."""
        import uuid
        if event_name == "Жіноче коло" and prepay_amount == 1.0:
            prepay_amount = 200.0
        description = f"Передплата {prepay_amount:.0f} грн: {event_name}"
        if not self.payment:
            mock_id = str(uuid.uuid4())
            invoice_url, invoice_id = f"https://checkout.wayforpay.com/pay/mock_event_{mock_id}", mock_id
        else:
            order_id = f"eb_{uuid.uuid4().hex[:12]}"
            invoice_url, invoice_id = await self.payment.create_invoice(prepay_amount, order_id, client_name, product_name=description)

        try:
            start_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                start_dt = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                start_dt = datetime.utcnow()

        new_booking = EventBooking(
            user_id=user_id,
            event_id=event_id,
            event_name=event_name,
            client_name=client_name,
            client_phone=client_phone,
            start_time=start_dt,
            status="pending",
            price=price
        )
        self.db.add(new_booking)
        await self.db.flush()

        new_payment = Payment(
            event_booking_id=new_booking.id,
            amount=prepay_amount,
            currency="UAH",
            status="pending",
            provider="wayforpay",
            invoice_id=invoice_id
        )
        self.db.add(new_payment)
        await self.db.flush()

        return invoice_url, invoice_id

    async def confirm_payment_and_booking(self, invoice_id: str) -> None:
        """Updates FSM reservation models status upon successful Monobank Callback."""
        lock_key = f"lock:payment_process:{invoice_id}"
        acquired = await self.redis.set(lock_key, "1", nx=True, ex=300)
        if not acquired:
            logger.info("payment_processing_already_in_progress", invoice_id=invoice_id)
            return

        # Query matching booking from database
        from app.database.models.transaction import Payment
        pay_query = select(Payment).where(Payment.invoice_id == invoice_id)
        pay_res = await self.db.execute(pay_query)
        payment = pay_res.scalar_one_or_none()
        
        from sqlalchemy.orm import selectinload
        if payment and payment.status != "success":
            payment.status = "success"
            

            
            # Fetch client metadata from payment_details JSON if populated
            client_name = ""
            client_phone = ""
            if payment.payment_details:
                client_name = payment.payment_details.get("client_name", "")
                client_phone = payment.payment_details.get("client_phone", "")

            if payment.consultation_id:
                booking_query = select(ConsultationBooking).where(ConsultationBooking.id == payment.consultation_id).options(
                    selectinload(ConsultationBooking.user),
                    selectinload(ConsultationBooking.psychologist)
                )
                booking_res = await self.db.execute(booking_query)
                booking = booking_res.scalar_one_or_none()
                
                if booking:
                    booking.status = "confirmed"
                    
                    if not client_name:
                        client_name = booking.user.first_name

                    from zoneinfo import ZoneInfo
                    kyiv_tz = ZoneInfo("Europe/Kyiv")
                    local_start = booking.start_time.astimezone(kyiv_tz)
                    slot_date = local_start.strftime("%Y-%m-%d")
                    slot_time = local_start.strftime("%H:%M")
                    slot_date_dmy = local_start.strftime("%d.%m.%Y")

                    # 1. Sync to Google Calendar safely
                    if self.gcal and booking.psychologist.google_calendar_id:
                        try:
                            gcal_id = await self.gcal.create_booking_event(
                                calendar_id=booking.psychologist.google_calendar_id,
                                summary=f"Консультація: {client_name}",
                                start_time=booking.start_time,
                                duration_minutes=60
                            )
                            booking.google_event_id = gcal_id
                        except Exception as e:
                            logger.error("google_calendar_sync_failed_during_settlement", error=str(e))
                        
                    # 2. Release Redis distributed lock explicitly
                    lock_key = f"lock:slot:{booking.psychologist_id}:{slot_date}:{slot_time}"
                    await self.redis.delete(lock_key)
                    
                    # Mark slot as booked in database and Google Sheets
                    await self.mark_specialist_slot_booked(
                        psychologist_id=booking.psychologist_id,
                        date_str=slot_date,
                        time_str=slot_time
                    )
                    
                    # 3. Schedule reminders
                    if self.scheduler:
                        await self.scheduler.schedule_reminders_for_booking(
                            booking_id=booking.id,
                            user_telegram_id=booking.user.telegram_id,
                            start_time=booking.start_time
                        )
                        
                    # 4. Sync to Google Sheets safely
                    if self.sheets:
                        try:
                            payment_method = "готівка" if payment.provider == "cash" else "wayforpay"
                            # Формат: онлайн / офлайн — колонка між Спеціаліст та Дата
                            fmt_label = "онлайн" if booking.format == "online" else "офлайн"
                            await self.sheets.append_row(
                                "Бронювання до спеціаліста",
                                [
                                    booking.id,
                                    client_name,
                                    client_phone,
                                    booking.psychologist.name,
                                    fmt_label,
                                    slot_date_dmy,
                                    slot_time,
                                    payment_method,
                                    float(payment.amount),
                                    float(booking.price - payment.amount)
                                ]
                            )
                        except Exception as e:
                            logger.error("google_sheets_sync_failed_during_settlement", error=str(e))
                        
                    logger.info("booking_payment_settled_successfully", booking_id=booking.id)
                    msg_text = (
                        f"✅ *Ваша оплата успішно зарахована!*\n\n"
                        f"Бронювання на консультацію підтверджено:\n"
                        f"👤 Психолог: *{booking.psychologist.name}*\n"
                        f"📅 Дата та час: *{slot_date_dmy} о {slot_time}*\n"
                        f"💻 Формат: *{'Онлайн (Zoom/Google Meet)' if booking.format == 'online' else 'Офлайн (Студія/Кабінет)'}*\n\n"
                        f"✨ _Чекаємо на вас! За 24 години та за 2 години до початку мы надішлемо вам нагадування._"
                    )
                    await self.send_or_edit_confirmation_message(booking.user.telegram_id, msg_text, is_photo=False)
            elif payment.room_booking_id:
                booking_query = select(RoomBooking).where(RoomBooking.id == payment.room_booking_id).options(
                    selectinload(RoomBooking.user),
                    selectinload(RoomBooking.room)
                )
                booking_res = await self.db.execute(booking_query)
                booking = booking_res.scalar_one_or_none()
                
                if booking:
                    booking.status = "confirmed"
                    
                    if not client_name:
                        client_name = booking.user.first_name

                    from zoneinfo import ZoneInfo
                    kyiv_tz = ZoneInfo("Europe/Kyiv")
                    local_start = booking.start_time.astimezone(kyiv_tz)
                    rent_date = local_start.strftime("%Y-%m-%d")
                    rent_time = local_start.strftime("%H:%M")

                    # Mark slots as booked in database and Google Sheets
                    duration_hours = max(1, int((booking.end_time - booking.start_time).total_seconds() // 3600))
                    booked_time_strs = []
                    for h in range(duration_hours):
                        slot_time_str = (local_start + timedelta(hours=h)).strftime("%H:%M")
                        booked_time_strs.append(slot_time_str)

                    await self.mark_room_rental_slots_booked(
                        room_id=booking.room_id,
                        date_str=rent_date,
                        time_strs=booked_time_strs
                    )
 
                    # Sync to Google Calendar safely
                    from app.core.config import settings
                    if self.gcal and settings.GOOGLE_CALENDAR_ID:
                        try:
                            if booking.room_id == 2:
                                event_title = payment.payment_details.get("title", "Захід") if payment.payment_details else "Захід"
                                summary = f"ЗАЯВКА НА ЗАХІД: {event_title} (Організатор: {client_name})"
                            else:
                                summary = f"Оренда: Головний кабінет - {client_name}"
                            gcal_id = await self.gcal.create_booking_event(
                                calendar_id=settings.GOOGLE_CALENDAR_ID,
                                summary=summary,
                                start_time=booking.start_time,
                                duration_minutes=duration_hours * 60
                            )
                            booking.google_event_id = gcal_id
                        except Exception as e:
                            logger.error("google_calendar_rental_sync_failed", error=str(e))

                    # Sync to Google Sheets safely
                    if self.sheets:
                        try:
                            payment_method = "готівка" if payment.provider == "cash" else "wayforpay"
                            if booking.room_id == 2 and payment.payment_details and payment.payment_details.get("type") == "host_event":
                                try:
                                    rows = await self.sheets.read_sheet("Заявки організаторів на заходи (Афіши)")
                                    next_id = 1
                                    if rows and len(rows) > 1:
                                        last_row = rows[-1]
                                        if last_row and str(last_row[0]).isdigit():
                                            next_id = int(last_row[0]) + 1
                                    
                                    p_det = payment.payment_details
                                    
                                    # Convert date from YYYY-MM-DD to DD.MM.YYYY
                                    raw_date = p_det.get("date", "")
                                    try:
                                        date_obj = datetime.strptime(raw_date, "%Y-%m-%d")
                                        formatted_date_only = date_obj.strftime("%d.%m.%Y")
                                    except Exception:
                                        formatted_date_only = raw_date

                                    await self.sheets.append_row(
                                        "Заявки організаторів на заходи (Афіши)",
                                        [
                                            next_id,                          # ID
                                            p_det.get("title", ""),           # Назва
                                            p_det.get("host", ""),            # Ведучий
                                            p_det.get("time", ""),            # Час (e.g. 17:00)
                                            formatted_date_only,              # Дата (e.g. 23.06.2026)
                                            int(p_det.get("limit", 15)),      # Ліміт місць
                                            float(p_det.get("price", 0.0)),   # Ціна
                                            "Актуальний",                     # Статус
                                            payment_method                    # ОПЛАТА
                                        ]
                                    )
                                    logger.info("sheets_host_event_synced", event_id=next_id)
                                except Exception as sheet_err:
                                    logger.error("failed_to_sync_host_event_to_sheets", error=str(sheet_err))
                            else:
                                await self.sheets.append_row(
                                    "Бронювання кабінету",
                                    [
                                        booking.id,
                                        client_name,
                                        client_phone,
                                        booking.room.name,
                                        rent_date,
                                        rent_time,
                                        payment_method,
                                        float(payment.amount),
                                        float(booking.price - payment.amount)
                                    ]
                                )
                        except Exception as e:
                            logger.error("google_sheets_rental_sync_failed", error=str(e))
                    logger.info("rental_payment_settled_successfully", booking_id=booking.id)
                    is_host_event = (booking.room_id == 2 and payment.payment_details and payment.payment_details.get("type") == "host_event")
                    if is_host_event:
                        # Clear cache so the new event appears in the bot instantly
                        await self.redis.delete("cache:events_list")
                        p_det = payment.payment_details
                        msg_text = (
                            f"✅ *Передплату за реєстрацію заходу успішно отримано!*\n\n"
                            f"📅 Ваша дата та час зарезервовані. Захід *«{p_det.get('title')}»* успішно опубліковано в розділі *«Афіша заходів»*.\n"
                            f"🎫 Тепер користувачі мають можливість реєструватися та купувати квитки на ваш захід!"
                        )
                    else:
                        msg_text = (
                            f"✅ *Передплату за оренду успішно внесено!*\n\n"
                            f"🏢 Приміщення: *{booking.room.name}*\n"
                            f"📅 Дата: *{rent_date}*\n"
                            f"⏰ Час: *{rent_time}* ({duration_hours} год.)\n\n"
                            f"✨ _Чекаємо на вас!_"
                        )
                    await self.send_or_edit_confirmation_message(booking.user.telegram_id, msg_text, is_photo=is_host_event)
            elif payment.event_booking_id:
                booking_query = select(EventBooking).where(EventBooking.id == payment.event_booking_id).options(
                    selectinload(EventBooking.user)
                )
                booking_res = await self.db.execute(booking_query)
                booking = booking_res.scalar_one_or_none()
                
                if booking:
                    booking.status = "paid"
                    
                    # Sync to Google Calendar safely
                    from app.core.config import settings
                    if self.gcal and settings.GOOGLE_CALENDAR_ID:
                        try:
                            # 1.5 hours duration (90 mins) for events
                            gcal_id = await self.gcal.create_booking_event(
                                calendar_id=settings.GOOGLE_CALENDAR_ID,
                                summary=f"Захід: {booking.event_name} - {booking.client_name}",
                                start_time=booking.start_time,
                                duration_minutes=90
                            )
                            booking.google_event_id = gcal_id
                        except Exception as e:
                            logger.error("google_calendar_event_sync_failed", error=str(e))

                    from zoneinfo import ZoneInfo
                    kyiv_tz = ZoneInfo("Europe/Kyiv")
                    local_start = booking.start_time.astimezone(kyiv_tz)
                    event_date = local_start.strftime("%Y-%m-%d")
                    event_time = local_start.strftime("%H:%M")
                    event_date_dmy = local_start.strftime("%d.%m.%Y")

                    if self.sheets:
                        try:
                            payment_method = "готівка" if payment.provider == "cash" else "wayforpay"
                            if booking.event_id == 99:
                                await self.sheets.append_row(
                                    "Жіноче коло",
                                    [
                                        booking.id,
                                        booking.client_name,
                                        booking.client_phone,
                                        event_date_dmy,
                                        payment_method,
                                        float(payment.amount),
                                        float(booking.price - payment.amount)
                                    ]
                                )
                            else:
                                await self.sheets.append_row(
                                    "Бронювання на заходи (Афіши)",
                                    [
                                        booking.id,
                                        booking.client_name,
                                        booking.client_phone,
                                        booking.event_name,
                                        event_date_dmy,
                                        event_time,
                                        payment_method,
                                        float(booking.price)
                                    ]
                                )
                        except Exception as e:
                            logger.error("google_sheets_event_sync_failed_during_settlement", error=str(e))
                    # Release Redis event seat lock
                    lock_key = f"lock:event_seat:{booking.event_id}:{booking.user.telegram_id}"
                    await self.redis.delete(lock_key)
                    
                    logger.info("event_payment_settled_successfully", booking_id=booking.id)
                    msg_text = (
                        f"✅ *Передплату успішно внесено!*\n\n"
                        f"Бронювання квитка підтверджено:\n"
                        f"🎟️ Захід: *{booking.event_name}*\n"
                        f"👤 Ім'я: *{booking.client_name}*\n"
                        f"📅 Дата: *{event_date_dmy}*\n"
                        f"⏰ Час: *{event_time}*\n\n"
                        f"✨ _Чекаємо на вас!_"
                    )
                    await self.send_or_edit_confirmation_message(booking.user.telegram_id, msg_text, is_photo=False)
            
            await self.db.commit()

    async def confirm_cash_payment(self, invoice_id: str) -> None:
        """Helper to transition a pending invoice to cash payment status and trigger all confirmations."""
        from app.database.models.transaction import Payment
        pay_query = select(Payment).where(Payment.invoice_id == invoice_id)
        pay_res = await self.db.execute(pay_query)
        payment = pay_res.scalar_one_or_none()
        
        if payment:
            payment.provider = "cash"
            await self.db.flush()
            # standard confirmation pipeline which marks status as success, syncs sheets, calendar, reminders
            await self.confirm_payment_and_booking(invoice_id)

    async def reject_payment_and_booking(self, invoice_id: str, reason_code: str = None) -> None:
        """Processes failed payments: updates status to failed and alerts the user with retry options."""
        lock_key = f"lock:payment_process:{invoice_id}"
        acquired = await self.redis.set(lock_key, "1", nx=True, ex=300)
        if not acquired:
            logger.info("payment_processing_already_in_progress", invoice_id=invoice_id)
            return

        from app.database.models.transaction import Payment
        pay_query = select(Payment).where(Payment.invoice_id == invoice_id)
        pay_res = await self.db.execute(pay_query)
        payment = pay_res.scalar_one_or_none()

        if payment and payment.status == "pending":
            payment.status = "failed"
            if reason_code:
                if not payment.payment_details:
                    payment.payment_details = {}
                payment.payment_details["decline_reason_code"] = reason_code
            await self.db.flush()

            telegram_id = None
            amount = float(payment.amount)
            
            from sqlalchemy.orm import selectinload
            if payment.consultation_id:
                from app.database.models.booking import ConsultationBooking
                b_q = select(ConsultationBooking).where(ConsultationBooking.id == payment.consultation_id).options(selectinload(ConsultationBooking.user))
                b_res = await self.db.execute(b_q)
                b_obj = b_res.scalar_one_or_none()
                if b_obj and b_obj.user:
                    telegram_id = b_obj.user.telegram_id
            elif payment.room_booking_id:
                from app.database.models.booking import RoomBooking
                b_q = select(RoomBooking).where(RoomBooking.id == payment.room_booking_id).options(selectinload(RoomBooking.user))
                b_res = await self.db.execute(b_q)
                b_obj = b_res.scalar_one_or_none()
                if b_obj and b_obj.user:
                    telegram_id = b_obj.user.telegram_id
            elif payment.event_booking_id:
                from app.database.models.booking import EventBooking
                b_q = select(EventBooking).where(EventBooking.id == payment.event_booking_id).options(selectinload(EventBooking.user))
                b_res = await self.db.execute(b_q)
                b_obj = b_res.scalar_one_or_none()
                if b_obj and b_obj.user:
                    telegram_id = b_obj.user.telegram_id

            if telegram_id:
                decline_text = WayForPayPaymentClient.get_decline_reason(reason_code)
                
                msg_text = (
                    f"❌ **Оплату відхилено**\n\n"
                    f"На жаль, платіж на суму *{amount:.2f} UAH* не вдалося провести.\n"
                    f"Причина відмови: *{decline_text}*.\n\n"
                    f"⏳ *Ми зберегли ваше бронювання ще на 10 хвилин.* Ви можете спробувати оплатити повторно за допомогою кнопки нижче:"
                )
                
                from aiogram.utils.keyboard import InlineKeyboardBuilder
                from aiogram.types import InlineKeyboardButton
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(text="💳 Спробувати знову", callback_data=f"pay_retry:{payment.id}"))
                builder.row(InlineKeyboardButton(text="❌ Скасувати бронювання", callback_data=f"pay_cancel:{payment.id}"))
                
                from app.bot.bot_setup import bot
                try:
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=msg_text,
                        parse_mode="Markdown",
                        reply_markup=builder.as_markup()
                    )
                except Exception as bot_err:
                    logger.error("failed_to_send_payment_failed_notification", user=telegram_id, error=str(bot_err))

            await self.db.commit()

    async def retry_payment(self, payment_id: int, telegram_id: int) -> tuple[str, str] | None:
        """Generates a new WayForPay invoice for a failed payment, extending the slot lock if valid."""
        from app.database.models.transaction import Payment
        pay_query = select(Payment).where(Payment.id == payment_id)
        pay_res = await self.db.execute(pay_query)
        payment = pay_res.scalar_one_or_none()

        if not payment:
            return None
        
        booking_obj = None
        slot_date = ""
        slot_time = ""
        psychologist_id = None
        room_id = None
        room_times = []
        prefix = "eb"
        
        from sqlalchemy.orm import selectinload
        if payment.consultation_id:
            prefix = "cb"
            from app.database.models.booking import ConsultationBooking
            b_q = select(ConsultationBooking).where(ConsultationBooking.id == payment.consultation_id).options(
                selectinload(ConsultationBooking.psychologist)
            )
            b_res = await self.db.execute(b_q)
            booking_obj = b_res.scalar_one_or_none()
            if booking_obj:
                if booking_obj.status != "pending":
                    logger.warning("retry_booking_already_processed", status=booking_obj.status)
                    return None
                from zoneinfo import ZoneInfo
                kyiv_tz = ZoneInfo("Europe/Kyiv")
                local_start = booking_obj.start_time.astimezone(kyiv_tz)
                slot_date = local_start.strftime("%Y-%m-%d")
                slot_time = local_start.strftime("%H:%M")
                psychologist_id = booking_obj.psychologist_id
                
        elif payment.room_booking_id:
            prefix = "rb"
            from app.database.models.booking import RoomBooking
            b_q = select(RoomBooking).where(RoomBooking.id == payment.room_booking_id).options(
                selectinload(RoomBooking.room)
            )
            b_res = await self.db.execute(b_q)
            booking_obj = b_res.scalar_one_or_none()
            if booking_obj:
                if booking_obj.status != "pending":
                    return None
                from zoneinfo import ZoneInfo
                kyiv_tz = ZoneInfo("Europe/Kyiv")
                local_start = booking_obj.start_time.astimezone(kyiv_tz)
                slot_date = local_start.strftime("%Y-%m-%d")
                room_id = booking_obj.room_id
                duration_hours = max(1, int((booking_obj.end_time - booking_obj.start_time).total_seconds() // 3600))
                for h in range(duration_hours):
                    room_times.append((local_start + timedelta(hours=h)).strftime("%H:%M"))
                    
        elif payment.event_booking_id:
            prefix = "eb"
            from app.database.models.booking import EventBooking
            b_q = select(EventBooking).where(EventBooking.id == payment.event_booking_id)
            b_res = await self.db.execute(b_q)
            booking_obj = b_res.scalar_one_or_none()
            if booking_obj:
                if booking_obj.status not in ("pending", "failed"):
                    return None

        if not booking_obj:
            return None

        # Verify slots in DB are still unbooked and extend/lock Redis key for 10 min
        if psychologist_id:
            from app.database.models.booking import SpecialistSlot
            slot_q = select(SpecialistSlot).where(
                SpecialistSlot.psychologist_id == psychologist_id,
                SpecialistSlot.date == slot_date,
                SpecialistSlot.time == slot_time
            )
            slot_res = await self.db.execute(slot_q)
            db_slot = slot_res.scalar_one_or_none()
            if db_slot and db_slot.is_booked:
                return None
                
            lock_key = f"lock:slot:{psychologist_id}:{slot_date}:{slot_time}"
            await self.redis.set(lock_key, telegram_id, ex=600)
            
        elif room_id:
            from app.database.models.booking import RoomRentalSlot
            for t_str in room_times:
                slot_q = select(RoomRentalSlot).where(
                    RoomRentalSlot.room_id == room_id,
                    RoomRentalSlot.date == slot_date,
                    RoomRentalSlot.time == t_str
                )
                slot_res = await self.db.execute(slot_q)
                db_slot = slot_res.scalar_one_or_none()
                if db_slot and db_slot.is_booked:
                    return None
                    
                lock_key = f"lock:room_slot:{room_id}:{slot_date}:{t_str}"
                await self.redis.set(lock_key, telegram_id, ex=600)

        # Generate new WayForPay invoice
        import uuid
        order_id = f"{prefix}_{uuid.uuid4().hex[:12]}"
        client_name = payment.payment_details.get("client_name", "Клієнт") if payment.payment_details else "Клієнт"
        
        # Build description
        if payment.consultation_id:
            description = f"Передплата {float(payment.amount):.0f} грн: Консультація з психологом (ID #{booking_obj.psychologist_id}, {booking_obj.format})"
        elif payment.room_booking_id:
            if booking_obj.room_id == 1:
                description = f"Передплата {float(payment.amount):.0f} грн: Оренда кабінету #{booking_obj.room_id}"
            else:
                description = f"Передплата {float(payment.amount):.0f} грн: Оренда залу для проведення заходу"
        else:
            description = f"Передплата {float(payment.amount):.0f} грн: {booking_obj.event_name}"
        
        if not self.payment:
            invoice_url = f"https://checkout.wayforpay.com/pay/mock_retry_{order_id}"
        else:
            invoice_url, order_id = await self.payment.create_invoice(
                amount=float(payment.amount),
                order_id=order_id,
                client_name=client_name,
                product_name=description
            )

        # Update Payment row
        payment.invoice_id = order_id
        payment.status = "pending"
        await self.db.commit()
        
        return invoice_url, order_id

    async def confirm_event_booking_without_prepayment(
        self,
        user_id: int,
        event_id: int,
        event_name: str,
        date_str: str,
        price: float,
        client_name: str,
        client_phone: str,
        telegram_id: int
    ) -> None:
        """Registers an event booking directly without requiring prepayment, syncing with GCal and Sheets."""
        from app.database.models.booking import EventBooking
        
        try:
            start_dt = datetime.strptime(date_str, "%d.%m.%Y %H:%M")
        except ValueError:
            try:
                start_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            except ValueError:
                try:
                    start_dt = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    start_dt = datetime.utcnow()

        new_booking = EventBooking(
            user_id=user_id,
            event_id=event_id,
            event_name=event_name,
            client_name=client_name,
            client_phone=client_phone,
            start_time=start_dt,
            status="confirmed",  # Mark as confirmed directly
            price=price
        )
        self.db.add(new_booking)
        await self.db.flush()

        # 1. Sync to Google Calendar safely
        from app.core.config import settings
        if self.gcal and settings.GOOGLE_CALENDAR_ID:
            try:
                gcal_id = await self.gcal.create_booking_event(
                    calendar_id=settings.GOOGLE_CALENDAR_ID,
                    summary=f"Захід: {event_name} - {client_name}",
                    start_time=start_dt,
                    duration_minutes=90
                )
                new_booking.google_event_id = gcal_id
            except Exception as e:
                logger.error("google_calendar_event_sync_failed", error=str(e))

        # 2. Sync to Google Sheets safely
        if self.sheets:
            try:
                from zoneinfo import ZoneInfo
                kyiv_tz = ZoneInfo("Europe/Kyiv")
                local_start = start_dt.astimezone(kyiv_tz) if start_dt.tzinfo else start_dt
                event_date_dmy = local_start.strftime("%d.%m.%Y")
                event_time = local_start.strftime("%H:%M")
                
                payment_status = "без передплати" if price > 0.0 else "безкоштовно"
                
                await self.sheets.append_row(
                    "Бронювання на заходи (Афіши)",
                    [
                        new_booking.id,
                        client_name,
                        client_phone,
                        event_name,
                        event_date_dmy,
                        event_time,
                        payment_status,
                        float(price)
                    ]
                )
            except Exception as e:
                logger.error("google_sheets_event_sync_failed", error=str(e))

        await self.db.commit()
        logger.info("event_booking_confirmed_without_prepayment", booking_id=new_booking.id)

        # Release Redis event seat lock
        lock_key = f"lock:event_seat:{event_id}:{telegram_id}"
        await self.redis.delete(lock_key)

        # 3. Send confirmation message in Telegram
        try:
            formatted_date_str = start_dt.strftime("%d.%m.%Y о %H:%M")
        except Exception:
            formatted_date_str = date_str
            
        if price > 0.0:
            price_details = f"💵 Вартість: *{price:.2f} UAH* (сплачується організатору)\n\n"
        else:
            price_details = f"💵 Вартість: *Безкоштовно*\n\n"
            
        msg_text = (
            f"✅ *Ваше бронювання підтверджено!*\n\n"
            f"🎟️ Захід: *{event_name}*\n"
            f"👤 Ім'я: *{client_name}*\n"
            f"📅 Дата: *{formatted_date_str}*\n"
            f"{price_details}"
            f"✨ _Чекаємо на вас!_"
        )
        await self.send_or_edit_confirmation_message(telegram_id, msg_text, is_photo=False)

    async def confirm_free_event_booking(
        self,
        user_id: int,
        event_id: int,
        event_name: str,
        date_str: str,
        client_name: str,
        client_phone: str,
        telegram_id: int
    ) -> None:
        """Saves a free event booking directly, bypasses WayForPay, and triggers calendar/sheet syncs."""
        from app.database.models.booking import EventBooking
        
        try:
            start_dt = datetime.strptime(date_str, "%d.%m.%Y %H:%M")
        except ValueError:
            try:
                start_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            except ValueError:
                try:
                    start_dt = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    start_dt = datetime.utcnow()

        new_booking = EventBooking(
            user_id=user_id,
            event_id=event_id,
            event_name=event_name,
            client_name=client_name,
            client_phone=client_phone,
            start_time=start_dt,
            status="paid",  # Mark as paid immediately since it's free
            price=0.0
        )
        self.db.add(new_booking)
        await self.db.flush()

        # 1. Sync to Google Calendar safely
        from app.core.config import settings
        if self.gcal and settings.GOOGLE_CALENDAR_ID:
            try:
                # 1.5 hours duration (90 mins) for events
                gcal_id = await self.gcal.create_booking_event(
                    calendar_id=settings.GOOGLE_CALENDAR_ID,
                    summary=f"Захід: {event_name} - {client_name}",
                    start_time=start_dt,
                    duration_minutes=90
                )
                new_booking.google_event_id = gcal_id
            except Exception as e:
                logger.error("google_calendar_free_event_sync_failed", error=str(e))

        # 2. Sync to Google Sheets safely
        if self.sheets:
            try:
                from zoneinfo import ZoneInfo
                kyiv_tz = ZoneInfo("Europe/Kyiv")
                local_start = start_dt.astimezone(kyiv_tz) if start_dt.tzinfo else start_dt
                event_date_dmy = local_start.strftime("%d.%m.%Y")
                
                await self.sheets.append_row(
                    "Бронювання на заходи (Афіши)",
                    [
                        new_booking.id,
                        client_name,
                        client_phone,
                        event_name,
                        event_date_dmy,
                        local_start.strftime("%H:%M"),
                        "безкоштовно",
                        0.0
                    ]
                )
            except Exception as e:
                logger.error("google_sheets_free_event_sync_failed", error=str(e))

        await self.db.commit()
        logger.info("free_event_booking_confirmed_successfully", booking_id=new_booking.id)

        # Release Redis event seat lock
        lock_key = f"lock:event_seat:{event_id}:{telegram_id}"
        await self.redis.delete(lock_key)

        # 3. Send confirmation message in Telegram
        try:
            formatted_date_str = start_dt.strftime("%d.%m.%Y о %H:%M")
        except Exception:
            formatted_date_str = date_str
            
        msg_text = (
            f"🎉 *Ваше місце успішно заброньовано!*\n\n"
            f"Реєстрація на захід підтверджена:\n"
            f"🎟️ Захід: *{event_name}*\n"
            f"👤 Ім'я: *{client_name}*\n"
            f"📅 Дата: *{formatted_date_str}*\n\n"
            f"💵 Вартість: *Безкоштовно*\n\n"
            f"✨ _Чекаємо на вас!_"
        )
        await self.send_or_edit_confirmation_message(telegram_id, msg_text, is_photo=False)

    async def get_cached_events(self) -> list[dict]:
        """Reads events list from Google Sheets and counts bookings, using Redis for 5-minute caching."""
        import json
        import asyncio
        cache_key = "cache:events_list"
        
        # 1. Try to read from Redis cache
        cached_data = await self.redis.get(cache_key)
        if cached_data:
            try:
                return json.loads(cached_data)
            except Exception:
                pass
                
        # 2. Fetch fresh data from Google Sheets if client is configured
        if not self.sheets:
            # Fallback mock events if sheets client is missing
            return [
                {
                    "id": 1,
                    "title": "Воркшоп «Справитися зі стресом»",
                    "host": "Ольга Ковальчук",
                    "date": "30.05.2026 18:00",
                    "limit": 15,
                    "price": 400.0,
                    "month": "Травень",
                    "status": "Актуальний",
                    "booked_count": 11,
                    "seats_left": 4
                },
                {
                    "id": 2,
                    "title": "Лекція «Анатомія тривоги»",
                    "host": "Дмитро Петренко",
                    "date": "15.06.2026 18:00",
                    "limit": 30,
                    "price": 0.0,
                    "month": "Червень",
                    "status": "Анонс",
                    "booked_count": 0,
                    "seats_left": 30
                }
            ]
            
        try:
            # Read static events register and booked tickets concurrently
            events_rows, bookings_rows = await asyncio.gather(
                self.sheets.read_sheet("Заявки організаторів на заходи (Афіши)"),
                self.sheets.read_sheet("Бронювання на заходи (Афіши)")
            )
            
            if not events_rows or len(events_rows) <= 1:
                return []
                
            # Count bookings per event title
            booking_counts = {}
            if bookings_rows and len(bookings_rows) > 1:
                for row in bookings_rows[1:]:
                    if len(row) > 6:
                        event_title = row[3]  # Захід is index 3
                        status = row[6]       # Статус is index 6
                        if status in ["paid", "монобанк", "готівка", "success"]:
                            booking_counts[event_title] = booking_counts.get(event_title, 0) + 1

            # Fetch all event seat locks from Redis in a single query to avoid N+1 queries
            all_lock_keys = await self.redis.keys("lock:event_seat:*")
            redis_locked_counts = {}
            for key in all_lock_keys:
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
                parts = key.split(":")
                if len(parts) >= 4:
                    try:
                        ev_id = int(parts[2])
                        redis_locked_counts[ev_id] = redis_locked_counts.get(ev_id, 0) + 1
                    except ValueError:
                        continue

            events_list = []
            # Parse events (skipping header row)
            for row in events_rows[1:]:
                if len(row) < 2:
                    continue
                try:
                    event_id = int(row[0])
                except ValueError:
                    continue
                    
                title = row[1]
                host = row[2] if len(row) > 2 else ""
                time_str = row[3] if len(row) > 3 else ""
                date_only = row[4] if len(row) > 4 else ""
                date_str = f"{date_only} {time_str}".strip()
                
                # Filter out expired/past events
                from zoneinfo import ZoneInfo
                kyiv_tz = ZoneInfo("Europe/Kyiv")
                now_local = datetime.now(kyiv_tz)
                
                is_past = False
                if date_str:
                    event_dt = None
                    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
                        try:
                            event_dt = datetime.strptime(date_str, fmt).replace(tzinfo=kyiv_tz)
                            if "%H:%M" not in fmt:
                                event_dt = event_dt.replace(hour=23, minute=59)
                            break
                        except ValueError:
                            continue
                    if event_dt and event_dt < now_local:
                        is_past = True
                        
                if is_past:
                    continue
                
                try:
                    limit = int(row[5]) if len(row) > 5 and row[5] else 15
                except ValueError:
                    limit = 15
                    
                try:
                    price = float(row[6]) if len(row) > 6 and row[6] else 0.0
                except ValueError:
                    price = 0.0
                    
                month = ""
                raw_status = str(row[7]).strip().lower() if len(row) > 7 and row[7] else "актуальний"
                if raw_status in ("анонс", "announcement"):
                    status = "Анонс"
                elif raw_status in ("скасовано", "cancelled", "cancel"):
                    status = "Скасовано"
                else:
                    status = "Актуальний"
                
                redis_locked_count = redis_locked_counts.get(event_id, 0)
                booked_count = booking_counts.get(title, 0) + redis_locked_count
                seats_left = max(0, limit - booked_count)
                
                events_list.append({
                    "id": event_id,
                    "title": title,
                    "host": host,
                    "date": date_str,
                    "limit": limit,
                    "price": price,
                    "month": month,
                    "status": status,
                    "booked_count": booked_count,
                    "seats_left": seats_left
                })
                
            # Save to cache for 24 hours (background job refreshes it every 5 minutes)
            await self.redis.setex(cache_key, 86400, json.dumps(events_list))
            return events_list
            
        except Exception as e:
            logger.error("failed_to_fetch_and_cache_events", error=str(e))
            return []

    async def send_or_edit_confirmation_message(self, telegram_id: int, text: str, is_photo: bool = False):
        from app.bot.bot_setup import dp, bot
        
        state_context = dp.fsm.resolve_context(bot, chat_id=telegram_id, user_id=telegram_id)
        state_data = await state_context.get_data()
        main_msg_id = state_data.get("main_msg_id")
        
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Головне меню", callback_data="menu:home")]
        ])
        
        success = False
        if main_msg_id:
            try:
                if is_photo:
                    await bot.edit_message_caption(
                        chat_id=telegram_id,
                        message_id=main_msg_id,
                        caption=text,
                        parse_mode="Markdown",
                        reply_markup=keyboard
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=telegram_id,
                        message_id=main_msg_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=keyboard
                    )
                success = True
            except Exception:
                pass
                
        if not success:
            try:
                await bot.send_message(
                    chat_id=telegram_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            except Exception as bot_err:
                logger.error("failed_to_send_fallback_confirmation_msg", error=str(bot_err))
                
        # Clear state after successful payment
        await state_context.clear()

    async def mark_specialist_slot_booked(self, psychologist_id: int, date_str: str, time_str: str) -> None:
        """Helper to mark a specialist slot as booked in DB and sync to Google Sheets 'Вільні слоти консультації'."""
        # 1. Update DB
        slot_q = select(SpecialistSlot).where(
            SpecialistSlot.psychologist_id == psychologist_id,
            SpecialistSlot.date == date_str,
            SpecialistSlot.time == time_str
        )
        slot_res = await self.db.execute(slot_q)
        db_slot = slot_res.scalar_one_or_none()
        if db_slot:
            db_slot.is_booked = True
        await self.db.flush()
        
        # 2. Update Google Sheet
        if self.sheets:
            try:
                # Get psychologist name
                psych_q = select(Psychologist).where(Psychologist.id == psychologist_id)
                psych_res = await self.db.execute(psych_q)
                psychologist = psych_res.scalar_one_or_none()
                if psychologist:
                    spec_name = psychologist.name
                    rows = await self.sheets.read_sheet("Вільні слоти консультації")
                    if rows:
                        parsed_date_dt = datetime.strptime(date_str, "%Y-%m-%d")
                        date_dmy = parsed_date_dt.strftime("%d.%m.%Y")
                        for idx, row in enumerate(rows):
                            if idx == 0:
                                continue
                            if len(row) >= 3:
                                r_date = str(row[0]).strip()
                                r_spec = str(row[1]).strip()
                                r_time = str(row[2]).strip()
                                
                                date_matches = (r_date == date_str or r_date == date_dmy)
                                if date_matches and r_spec.lower() == spec_name.strip().lower() and r_time == time_str:
                                    await self.sheets.update_cell("Вільні слоти консультації", idx + 1, "D", "Зайнятий")
                                    logger.info("sheets_specialist_slot_booked", row=idx + 1)
                                    break
            except Exception as e:
                logger.error("failed_to_sync_specialist_slot_booked_to_sheets", error=str(e))

    async def mark_room_rental_slots_booked(self, room_id: int, date_str: str, time_strs: list[str]) -> None:
        """Helper to mark slots as booked in DB and sync to Google Sheets."""
        rooms_to_block = [room_id]
        if room_id == 2:
            rooms_to_block.append(1)
        elif room_id == 1:
            rooms_to_block.append(2)
            
        # 1. Update DB
        for r_id in rooms_to_block:
            for t_str in time_strs:
                slot_q = select(RoomRentalSlot).where(
                    RoomRentalSlot.room_id == r_id,
                    RoomRentalSlot.date == date_str,
                    RoomRentalSlot.time == t_str
                )
                slot_res = await self.db.execute(slot_q)
                db_slot = slot_res.scalar_one_or_none()
                if db_slot:
                    db_slot.is_booked = True
                    
                # Release Redis lock
                lock_key = f"lock:room_slot:{r_id}:{date_str}:{t_str}"
                await self.redis.delete(lock_key)
                
        await self.db.flush()
        
        # 2. Update Google Sheet
        if self.sheets:
            try:
                sheet_name = "ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ" if room_id == 1 else "ВІЛЬНІ СЛОТИ ОРЕНДА НА ЗАХОДИ (АФІШИ)"
                rows = await self.sheets.read_sheet(sheet_name)
                if rows:
                    for idx, row in enumerate(rows):
                        if len(row) >= 2:
                            r_date = str(row[0]).strip()
                            r_time = str(row[1]).strip()
                            
                            # Normalize date format for comparison
                            norm_date = None
                            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                                try:
                                    norm_date = datetime.strptime(r_date, fmt).strftime("%Y-%m-%d")
                                    break
                                except ValueError:
                                    pass
                            if norm_date != date_str:
                                continue
                                
                            # Normalize time format
                            norm_time = None
                            try:
                                norm_time = datetime.strptime(r_time, "%H:%M").strftime("%H:%M")
                            except ValueError:
                                try:
                                    norm_time = datetime.strptime(r_time, "%H.%M").strftime("%H:%M")
                                except ValueError:
                                    continue
                                    
                            if norm_time in time_strs:
                                # Update column C (3rd column) to "Зайнятий"
                                # Rows in sheets API are 1-based (index + 1)
                                await self.sheets.update_cell(sheet_name, idx + 1, "C", "Зайнятий")
            except Exception as e:
                logger.error("failed_to_update_room_slots_in_google_sheets", error=str(e), room_id=room_id)
