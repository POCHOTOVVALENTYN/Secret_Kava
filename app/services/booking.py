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
            
            available = []
            for s in slots:
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
        if room_id == 1:
            query = select(RoomRentalSlot.date).where(
                RoomRentalSlot.room_id == 1,
                RoomRentalSlot.is_booked == False,
                RoomRentalSlot.date.like(f"{prefix}%")
            ).distinct()
            res = await self.db.execute(query)
            return set(res.scalars().all())
        elif room_id == 2:
            import calendar
            from datetime import datetime
            now = datetime.now()
            today = now.date()
            
            cal = calendar.Calendar(firstweekday=0)
            month_days = [d for d in cal.itermonthdates(year, month) if d.month == month and d >= today]
            
            available_dates = set()
            for day in month_days:
                slots = await self.get_available_room_slots(room_id=2, date_str=day.strftime("%Y-%m-%d"))
                if slots:
                    available_dates.add(day.strftime("%Y-%m-%d"))
            return available_dates
        return set()

    async def get_available_room_slots(self, room_id: int, date_str: str) -> list[str]:
        """Returns list of free 'HH:MM' slot times for room_id on date_str."""
        if room_id == 1:
            query = select(RoomRentalSlot.time).where(
                RoomRentalSlot.room_id == 1,
                RoomRentalSlot.date == date_str,
                RoomRentalSlot.is_booked == False
            ).order_by(RoomRentalSlot.time.asc())
            res = await self.db.execute(query)
            available_times = res.scalars().all()
            
            free_times = []
            for t in available_times:
                lock_key = f"lock:room_slot:1:{date_str}:{t}"
                if not await self.redis.get(lock_key):
                    free_times.append(t)
            return free_times
            
        elif room_id == 2:
            from datetime import datetime, timedelta
            try:
                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return []
            
            is_friday = (date_obj.weekday() == 4)
            hours = [f"{h:02d}:00" for h in range(8, 20)]
            
            from app.database.models.booking import RoomBooking
            
            day_start = datetime.combine(date_obj, datetime.min.time())
            day_end = datetime.combine(date_obj, datetime.max.time())
            
            r_query = select(RoomBooking).where(
                RoomBooking.room_id == 2,
                RoomBooking.start_time >= day_start,
                RoomBooking.start_time <= day_end,
                RoomBooking.status.in_(["pending", "paid", "confirmed"])
            )
            r_res = await self.db.execute(r_query)
            bookings = r_res.scalars().all()
            
            from zoneinfo import ZoneInfo
            kyiv_tz = ZoneInfo("Europe/Kyiv")
            
            free_times = []
            for t_str in hours:
                if is_friday:
                    h_val = int(t_str.split(":")[0])
                    if 17 <= h_val < 20:
                        continue
                        
                slot_time = datetime.strptime(f"{date_str} {t_str}", "%Y-%m-%d %H:%M")
                
                has_overlap = False
                for b in bookings:
                    b_start = b.start_time.astimezone(kyiv_tz).replace(tzinfo=None)
                    b_end = b.end_time.astimezone(kyiv_tz).replace(tzinfo=None)
                    if b_start <= slot_time < b_end:
                        has_overlap = True
                        break
                if has_overlap:
                    continue
                    
                lock_key2 = f"lock:room_slot:2:{date_str}:{t_str}"
                if await self.redis.get(lock_key2):
                    continue
                    
                free_times.append(t_str)
                
            return free_times
        return []

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
        """Synchronizes slots from Google Sheets tab 'Вільні слоти' into PostgreSQL database."""
        if not self.sheets:
            logger.warning("sheets_client_not_configured_skipping_slot_sync")
            return
            
        try:
            # 1. Read sheet rows
            rows = await self.sheets.read_sheet("Вільні слоти")
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
                slot = slot_res.scalar_one_or_none()
                
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

    async def sync_room_rental_slots_from_sheets(self) -> None:
        """Synchronizes room rental slots from Google Sheets tab 'ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ' into PostgreSQL."""
        if not self.sheets:
            logger.warning("sheets_client_not_configured_skipping_room_slot_sync")
            return
            
        try:
            # 1. Read sheet rows
            rows = await self.sheets.read_sheet("ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ")
            if not rows:
                logger.info("no_room_slots_found_in_sheet_or_empty")
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
                    logger.warning("invalid_date_format_in_room_sheets_row", row=idx, date=raw_date)
                    continue
                    
                # Normalize time
                try:
                    parsed_time = datetime.strptime(raw_time, "%H:%M").strftime("%H:%M")
                except ValueError:
                    try:
                        parsed_time = datetime.strptime(raw_time, "%H.%M").strftime("%H:%M")
                    except ValueError:
                        logger.warning("invalid_time_format_in_room_sheets_row", row=idx, time=raw_time)
                        continue
                        
                is_booked = (raw_status.lower() in ("зайнятий", "заброньовано", "booked", "yes", "true", "1"))
                sheet_slot_keys.add((parsed_date, parsed_time))
                
                # Check if slot already exists in DB for Room ID 1 (Головний кабінет)
                slot_query = select(RoomRentalSlot).where(
                    RoomRentalSlot.room_id == 1,
                    RoomRentalSlot.date == parsed_date,
                    RoomRentalSlot.time == parsed_time
                )
                slot_res = await self.db.execute(slot_query)
                slot = slot_res.scalar_one_or_none()
                
                if slot:
                    if slot.is_booked != is_booked:
                        slot.is_booked = is_booked
                        logger.info("updated_room_slot_status_from_sheets", date=parsed_date, time=parsed_time, is_booked=is_booked)
                else:
                    new_slot = RoomRentalSlot(
                        room_id=1,
                        date=parsed_date,
                        time=parsed_time,
                        is_booked=is_booked
                    )
                    self.db.add(new_slot)
                    logger.info("inserted_room_slot_from_sheets", date=parsed_date, time=parsed_time, is_booked=is_booked)
                    
            await self.db.commit()
            
            # Delete any slots in PostgreSQL database that are NOT in Google Sheets (unless they are booked)
            all_slots_query = select(RoomRentalSlot).where(RoomRentalSlot.room_id == 1)
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
                logger.info("deleted_removed_unbooked_room_slots_from_db", count=deleted_count)
                
        except Exception as e:
            logger.error("failed_to_sync_room_slots_from_sheets", error=str(e))
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
        limit: int,
        price: float,
        client_name: str,
        client_phone: str
    ) -> tuple[str, str]:
        """Generates WayForPay payment link for hosting event request (prepayment of 100 UAH)."""
        import uuid
        prepay_amount = 100.0
        description = f"Передплата за проведення заходу: «{title}»"
        if not self.payment:
            mock_id = str(uuid.uuid4())
            invoice_url, invoice_id = f"https://checkout.wayforpay.com/pay/mock_host_{mock_id}", mock_id
        else:
            order_id = f"he_{uuid.uuid4().hex[:12]}"
            invoice_url, invoice_id = await self.payment.create_invoice(prepay_amount, order_id, client_name, product_name=description)
            
        new_payment = Payment(
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
        description = f"Передплата 50 грн: Консультація з психологом (ID #{psychologist_id}, {format_type})"
        
        import uuid
        if not self.payment:
            mock_id = str(uuid.uuid4())
            invoice_url, invoice_id = f"https://checkout.wayforpay.com/pay/mock_{mock_id}", mock_id
        else:
            order_id = f"cb_{uuid.uuid4().hex[:12]}"
            invoice_url, invoice_id = await self.payment.create_invoice(50.0, order_id, client_name, product_name=description)
            
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
            amount=50.0,
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
        """Generates dynamic checkout page URL for room rental and saves pending booking."""
        prepay_amount = 200.0 if room_id == 2 else 50.0
        description = f"Передплата {prepay_amount:.0f} грн: Оренда кабінету #{room_id} на {hours} год."
        
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
        client_phone: str
    ) -> tuple[str, str]:
        """Generates dynamic checkout page URL using WayForPay API and saves pending event booking."""
        import uuid
        prepay_amount = 100.0 if event_id == 99 else 50.0
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
        # Query matching booking from database
        from app.database.models.transaction import Payment
        pay_query = select(Payment).where(Payment.invoice_id == invoice_id)
        pay_res = await self.db.execute(pay_query)
        payment = pay_res.scalar_one_or_none()
        
        from sqlalchemy.orm import selectinload
        if payment and payment.status != "success":
            payment.status = "success"
            
            if payment.payment_details and payment.payment_details.get("type") == "host_event":
                if self.sheets:
                    try:
                        rows = await self.sheets.read_sheet("Заявки організаторів на заходи (Афіши)")
                        next_id = 1
                        if rows and len(rows) > 1:
                            last_row = rows[-1]
                            if last_row and str(last_row[0]).isdigit():
                                next_id = int(last_row[0]) + 1
                        p_det = payment.payment_details
                        payment_method = "готівка" if payment.provider == "cash" else "wayforpay"
                        await self.sheets.append_row(
                            "Заявки організаторів на заходи (Афіши)",
                            [
                                next_id,
                                p_det.get("title", ""),
                                p_det.get("host", ""),
                                p_det.get("date", ""),
                                int(p_det.get("limit", 15)),
                                float(p_det.get("price", 0.0)),
                                "Новий",
                                payment_method
                            ]
                        )
                        logger.info("sheets_host_event_synced", event_id=next_id)
                    except Exception as e:
                        logger.error("failed_to_sync_host_event_to_sheets", error=str(e))
                
                # Sync to Google Calendar safely
                from app.core.config import settings
                if self.gcal and settings.GOOGLE_CALENDAR_ID:
                    try:
                        p_det = payment.payment_details
                        event_date_str = p_det.get("date", "")
                        event_dt = datetime.utcnow()
                        import re
                        match = re.search(r"(\d{1,2})\.(\d{1,2})", event_date_str)
                        if match:
                            day = int(match.group(1))
                            month = int(match.group(2))
                            year = datetime.now().year
                            hour = 18
                            minute = 0
                            time_match = re.search(r"(\d{1,2})[:.](\d{2})", event_date_str)
                            if time_match:
                                hour = int(time_match.group(1))
                                minute = int(time_match.group(2))
                            event_dt = datetime(year, month, day, hour, minute)
                        
                        await self.gcal.create_booking_event(
                            calendar_id=settings.GOOGLE_CALENDAR_ID,
                            summary=f"ЗАЯВКА НА ЗАХІД: {p_det.get('title')} (Організатор: {p_det.get('client_name')})",
                            start_time=event_dt,
                            duration_minutes=120
                        )
                    except Exception as e:
                        logger.error("google_calendar_host_event_sync_failed", error=str(e))
                await self.db.commit()
                return
            
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
                    slot_date = booking.start_time.strftime("%Y-%m-%d")
                    slot_time = booking.start_time.strftime("%H:%M")
                    lock_key = f"lock:slot:{booking.psychologist_id}:{slot_date}:{slot_time}"
                    await self.redis.delete(lock_key)
                    
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
                                    slot_date,
                                    slot_time,
                                    payment_method,
                                    float(booking.price)
                                ]
                            )
                        except Exception as e:
                            logger.error("google_sheets_sync_failed_during_settlement", error=str(e))
                        
                    logger.info("booking_payment_settled_successfully", booking_id=booking.id)
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
                            room_label = "Зал для заходів" if booking.room_id == 2 else "Головний кабінет"
                            gcal_id = await self.gcal.create_booking_event(
                                calendar_id=settings.GOOGLE_CALENDAR_ID,
                                summary=f"Оренда: {room_label} - {client_name}",
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
                                    float(booking.price)
                                ]
                            )
                        except Exception as e:
                            logger.error("google_sheets_rental_sync_failed", error=str(e))
                    logger.info("rental_payment_settled_successfully", booking_id=booking.id)
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

                    if self.sheets:
                        try:
                            event_date = booking.start_time.strftime("%Y-%m-%d")
                            event_time = booking.start_time.strftime("%H:%M")
                            payment_method = "готівка" if payment.provider == "cash" else "wayforpay"
                            if booking.event_id == 99:
                                await self.sheets.append_row(
                                    "Жіноче коло",
                                    [
                                        booking.id,
                                        booking.client_name,
                                        booking.client_phone,
                                        event_date,
                                        payment_method,
                                        100.0,
                                        300.0
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
                                        event_date,
                                        event_time,
                                        payment_method,
                                        float(booking.price)
                                    ]
                                )
                        except Exception as e:
                            logger.error("google_sheets_event_sync_failed_during_settlement", error=str(e))
                    logger.info("event_payment_settled_successfully", booking_id=booking.id)
            
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

    async def get_cached_events(self) -> list[dict]:
        """Reads events list from Google Sheets and counts bookings, using Redis for 5-minute caching."""
        import json
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
            # Read static events register
            events_rows = await self.sheets.read_sheet("Заявки організаторів на заходи (Афіши)")
            if not events_rows or len(events_rows) <= 1:
                return []
                
            # Read booked tickets to count seats left
            bookings_rows = await self.sheets.read_sheet("Бронювання на заходи (Афіши)")
            
            # Count bookings per event title
            booking_counts = {}
            if bookings_rows and len(bookings_rows) > 1:
                for row in bookings_rows[1:]:
                    if len(row) > 6:
                        event_title = row[3]  # Захід is index 3
                        status = row[6]       # Статус is index 6
                        if status in ["paid", "монобанк", "готівка", "success"]:
                            booking_counts[event_title] = booking_counts.get(event_title, 0) + 1

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
                date_str = row[3] if len(row) > 3 else ""
                
                try:
                    limit = int(row[4]) if len(row) > 4 and row[4] else 15
                except ValueError:
                    limit = 15
                    
                try:
                    price = float(row[5]) if len(row) > 5 and row[5] else 0.0
                except ValueError:
                    price = 0.0
                    
                month = ""
                status = row[6] if len(row) > 6 else "Актуальний"
                
                booked_count = booking_counts.get(title, 0)
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
                
            # Save to cache for 5 minutes
            await self.redis.setex(cache_key, 300, json.dumps(events_list))
            return events_list
            
        except Exception as e:
            logger.error("failed_to_fetch_and_cache_events", error=str(e))
            return []

    async def mark_room_rental_slots_booked(self, room_id: int, date_str: str, time_strs: list[str]) -> None:
        """Helper to mark slots as booked in DB and sync to Google Sheets."""
        rooms_to_block = [room_id]
            
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
        
        # 2. Update Google Sheet 'ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ'
        if self.sheets:
            try:
                rows = await self.sheets.read_sheet("ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ")
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
                                await self.sheets.update_cell("ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ", idx + 1, "C", "Зайнятий")
            except Exception as e:
                logger.error("failed_to_update_room_slots_in_google_sheets", error=str(e))
