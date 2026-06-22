# app/scheduler/jobs.py
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.redis import RedisJobStore
from aiogram import Bot
from structlog import get_logger

logger = get_logger()

global_scheduler_service = None

class SchedulerService:
    """Orchestrates dynamic notifications reminders and client NPS survey loops."""
    
    def __init__(self, bot: Bot, redis_url: str):
        self.bot = bot
        global global_scheduler_service
        global_scheduler_service = self
        # Using Redis as persistent Job Store backend
        redis_host = "localhost"
        redis_port = 6379
        
        # Parse host details out of redis DSN
        if "://" in redis_url:
            clean_url = redis_url.split("://")[1]
            if "@" in clean_url:
                clean_url = clean_url.split("@")[1]
            if ":" in clean_url:
                redis_host = clean_url.split(":")[0]
                redis_port = int(clean_url.split(":")[1].split("/")[0])
            else:
                redis_host = clean_url.split("/")[0]

        job_stores = {
            "default": RedisJobStore(
                host=redis_host,
                port=redis_port,
                db=1
            )
        }
        self.scheduler = AsyncIOScheduler(jobstores=job_stores, timezone="Europe/Kyiv")

    def start(self) -> None:
        """Starts the scheduler thread loop."""
        self.scheduler.start()
        logger.info("apscheduler_service_loop_started")
        
        # Schedule periodic slot synchronization from Google Sheets (every 5 minutes)
        self.scheduler.add_job(
            sync_specialist_slots_job,
            'interval',
            minutes=5,
            id="sync_specialist_slots_job",
            replace_existing=True
        )
        self.scheduler.add_job(
            sync_room_rental_slots_job,
            'interval',
            minutes=5,
            id="sync_room_rental_slots_job",
            replace_existing=True
        )
        # Schedule Thursday reminders for Women's Circle at 19:00 Europe/Kyiv time
        self.scheduler.add_job(
            send_thursday_reminders_job,
            'cron',
            day_of_week='thu',
            hour=19,
            minute=0,
            id="send_thursday_reminders_job",
            replace_existing=True
        )
        # Schedule pending payments checker (every 1 minute)
        self.scheduler.add_job(
            check_pending_payments_job,
            'interval',
            minutes=1,
            id="check_pending_payments_job",
            replace_existing=True
        )

    def shutdown(self) -> None:
        """Stops the scheduler threads."""
        self.scheduler.shutdown()
        logger.info("apscheduler_service_loop_shutdown")

    async def schedule_reminders_for_booking(self, booking_id: int, user_telegram_id: int, start_time: datetime) -> None:
        """Schedules 24h & 2h client reminders and NPS feedback triggers."""
        
        now_dt = datetime.now(start_time.tzinfo) if start_time.tzinfo else datetime.now()
        
        # 24-hour notification reminder
        run_24h = start_time - timedelta(hours=24)
        if run_24h > now_dt:
            self.scheduler.add_job(
                send_reminder_message_job,
                'date',
                run_date=run_24h,
                args=[user_telegram_id, "🌟 Нагадуємо, що ваша консультація відбудеться завтра о " + start_time.strftime("%H:%M")],
                id=f"rem_24h_{booking_id}",
                replace_existing=True
            )
 
        # 2-hour notification reminder
        run_2h = start_time - timedelta(hours=2)
        if run_2h > now_dt:
            self.scheduler.add_job(
                send_reminder_message_job,
                'date',
                run_date=run_2h,
                args=[user_telegram_id, "⏰ Нагадуємо, що ваша консультація розпочнеться вже за 2 години!"],
                id=f"rem_2h_{booking_id}",
                replace_existing=True
            )
 
        # NPS Review collector (triggered 2 hours after start time)
        nps_run_time = start_time + timedelta(hours=2)
        self.scheduler.add_job(
            send_nps_survey_job,
            'date',
            run_date=nps_run_time,
            args=[booking_id, user_telegram_id],
            id=f"nps_collect_{booking_id}",
            replace_existing=True
        )


async def send_reminder_message_job(telegram_id: int, text: str) -> None:
    """Delivers raw text notifications directly to TG clients."""
    from app.bot.bot_setup import bot
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
        logger.info("scheduled_reminder_message_sent", recipient=telegram_id)
    except Exception as e:
        logger.error("scheduled_reminder_message_delivery_failed", recipient=telegram_id, error=str(e))


async def send_nps_survey_job(booking_id: int, telegram_id: int) -> None:
    """Triggers NPS survey form selection keyboards."""
    from app.bot.bot_setup import bot
    try:
        from app.bot.keyboards.inline import get_nps_keyboard
        await bot.send_message(
            chat_id=telegram_id,
            text="🌱 *Як пройшла ваша сьогоднішня сесія?*\nБудь ласка, оцініть якість послуг від 1 до 10:",
            parse_mode="Markdown",
            reply_markup=get_nps_keyboard(booking_id)
        )
        logger.info("nps_survey_triggered", booking=booking_id)
    except Exception as e:
        logger.error("nps_survey_trigger_failed", booking=booking_id, error=str(e))


async def sync_specialist_slots_job() -> None:
    """Synchronizes slots from Google Sheets to PostgreSQL periodically."""
    from app.database.session import async_session_factory
    from app.services.booking import BookingService
    from app.integrations.google_sheets import GoogleSheetsClient
    from app.core.config import settings
    import redis.asyncio as redis
    import os
    
    logger.info("background_specialist_slots_sync_started")
    async with async_session_factory() as session:
        has_sa = settings.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(settings.GOOGLE_SERVICE_ACCOUNT_FILE)
        has_oauth = settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REFRESH_TOKEN
        
        sheets_client = None
        if settings.GOOGLE_SHEET_ID and (has_sa or has_oauth):
            try:
                sheets_client = GoogleSheetsClient(
                    spreadsheet_id=settings.GOOGLE_SHEET_ID,
                    client_id=settings.GOOGLE_CLIENT_ID,
                    client_secret=settings.GOOGLE_CLIENT_SECRET.get_secret_value() if settings.GOOGLE_CLIENT_SECRET else None,
                    refresh_token=settings.GOOGLE_REFRESH_TOKEN.get_secret_value() if settings.GOOGLE_REFRESH_TOKEN else None,
                    service_account_file=settings.GOOGLE_SERVICE_ACCOUNT_FILE
                )
            except Exception as e:
                logger.error("sheets_client_init_failed_in_job", error=str(e))
                
        redis_client = redis.from_url(settings.REDIS_URL)
        
        booking_service = BookingService(
            redis_client=redis_client,
            db_session=session,
            sheets_client=sheets_client
        )
        
        await booking_service.sync_specialist_slots_from_sheets()
        await redis_client.close()
    logger.info("background_specialist_slots_sync_completed")


async def sync_room_rental_slots_job() -> None:
    """Synchronizes room rental slots from Google Sheets to PostgreSQL periodically."""
    from app.database.session import async_session_factory
    from app.services.booking import BookingService
    from app.integrations.google_sheets import GoogleSheetsClient
    from app.core.config import settings
    import redis.asyncio as redis
    import os
    
    logger.info("background_room_rental_slots_sync_started")
    async with async_session_factory() as session:
        has_sa = settings.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(settings.GOOGLE_SERVICE_ACCOUNT_FILE)
        has_oauth = settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REFRESH_TOKEN
        
        sheets_client = None
        if settings.GOOGLE_SHEET_ID and (has_sa or has_oauth):
            try:
                sheets_client = GoogleSheetsClient(
                    spreadsheet_id=settings.GOOGLE_SHEET_ID,
                    client_id=settings.GOOGLE_CLIENT_ID,
                    client_secret=settings.GOOGLE_CLIENT_SECRET.get_secret_value() if settings.GOOGLE_CLIENT_SECRET else None,
                    refresh_token=settings.GOOGLE_REFRESH_TOKEN.get_secret_value() if settings.GOOGLE_REFRESH_TOKEN else None,
                    service_account_file=settings.GOOGLE_SERVICE_ACCOUNT_FILE
                )
            except Exception as e:
                logger.error("sheets_client_init_failed_in_job", error=str(e))
                
        redis_client = redis.from_url(settings.REDIS_URL)
        
        booking_service = BookingService(
            redis_client=redis_client,
            db_session=session,
            sheets_client=sheets_client
        )
        
        await booking_service.sync_room_rental_slots_from_sheets(room_id=1)
        await booking_service.sync_room_rental_slots_from_sheets(room_id=2)
        await redis_client.close()
    logger.info("background_room_rental_slots_sync_completed")


async def send_thursday_reminders_job() -> None:
    """Finds all participants registered for the upcoming Friday's Women's Circle and sends reminders."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from sqlalchemy import select
    from app.database.session import async_session_factory
    from app.database.models.booking import EventBooking
    from app.bot.bot_setup import bot
    from sqlalchemy.orm import selectinload
    
    logger.info("background_thursday_reminders_job_started")
    
    kyiv_tz = ZoneInfo("Europe/Kyiv")
    now_local = datetime.now(kyiv_tz)
    
    # Next day (Friday) date
    friday_date = (now_local + timedelta(days=1)).date()
    event_start = datetime.combine(friday_date, datetime.strptime("17:00", "%H:%M").time())
    
    async with async_session_factory() as session:
        query = select(EventBooking).where(
            EventBooking.event_id == 99,
            EventBooking.start_time == event_start,
            EventBooking.status.in_(["paid", "confirmed"])
        ).options(selectinload(EventBooking.user))
        
        res = await session.execute(query)
        bookings = res.scalars().all()
        
        reminder_text = (
            "🍷 *Нагадуємо, що завтра о 17:00 чекаємо вас на Жіночому колі!*\n\n"
            "📍 Наша адреса: *вул. Канатна, 100/4*\n"
            "Зустріч триватиме з 17:00 до 20:00. До зустрічі! ✨"
        )
        
        sent_count = 0
        for b in bookings:
            if b.user and b.user.telegram_id:
                try:
                    await bot.send_message(
                        chat_id=b.user.telegram_id,
                        text=reminder_text,
                        parse_mode="Markdown"
                    )
                    sent_count += 1
                except Exception as e:
                    logger.error("failed_to_send_thursday_reminder_to_user", user=b.user.telegram_id, error=str(e))
                    
        logger.info("background_thursday_reminders_job_completed", sent_count=sent_count)


async def check_pending_payments_job() -> None:
    """Periodically queries WayForPay for any pending payments to verify if they have been settled."""
    global global_scheduler_service
    scheduler_service = global_scheduler_service
    from sqlalchemy import select
    from app.database.session import async_session_factory
    from app.database.models.transaction import Payment
    from app.integrations.payments import WayForPayPaymentClient
    from app.integrations.google_sheets import GoogleSheetsClient
    from app.services.booking import BookingService
    from app.core.config import settings
    import redis.asyncio as redis
    import hmac, hashlib, httpx, os
    
    logger.info("background_pending_payments_check_started")
    
    async with async_session_factory() as session:
        from datetime import datetime, timedelta, timezone
        time_threshold = datetime.now(timezone.utc) - timedelta(hours=24)
        
        query = select(Payment).where(
            Payment.status == "pending",
            Payment.provider != "cash",
            Payment.created_at >= time_threshold
        )
        res = await session.execute(query)
        pending_payments = res.scalars().all()
        
        if not pending_payments:
            logger.info("background_pending_payments_check_no_pending")
            return
            
        payment_client = WayForPayPaymentClient(
            merchant_account=settings.WAYFORPAY_MERCHANT_ACCOUNT or "",
            secret_key=settings.WAYFORPAY_MERCHANT_SECRET.get_secret_value() if settings.WAYFORPAY_MERCHANT_SECRET else "",
            webhook_url="",
            domain=settings.WAYFORPAY_MERCHANT_DOMAIN
        )
        
        sheets_client = None
        gcal_client = None
        has_sa = settings.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(settings.GOOGLE_SERVICE_ACCOUNT_FILE)
        has_oauth = settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REFRESH_TOKEN
        
        if has_sa or has_oauth:
            from app.integrations.google_cal import GoogleCalendarClient
            try:
                gcal_client = GoogleCalendarClient(
                    client_id=settings.GOOGLE_CLIENT_ID,
                    client_secret=settings.GOOGLE_CLIENT_SECRET.get_secret_value() if settings.GOOGLE_CLIENT_SECRET else None,
                    refresh_token=settings.GOOGLE_REFRESH_TOKEN.get_secret_value() if settings.GOOGLE_REFRESH_TOKEN else None,
                    service_account_file=settings.GOOGLE_SERVICE_ACCOUNT_FILE
                )
            except Exception as e:
                logger.error("failed_to_init_gcal_client_in_check_job", error=str(e))
                
            if settings.GOOGLE_SHEET_ID:
                try:
                    sheets_client = GoogleSheetsClient(
                        spreadsheet_id=settings.GOOGLE_SHEET_ID,
                        client_id=settings.GOOGLE_CLIENT_ID,
                        client_secret=settings.GOOGLE_CLIENT_SECRET.get_secret_value() if settings.GOOGLE_CLIENT_SECRET else None,
                        refresh_token=settings.GOOGLE_REFRESH_TOKEN.get_secret_value() if settings.GOOGLE_REFRESH_TOKEN else None,
                        service_account_file=settings.GOOGLE_SERVICE_ACCOUNT_FILE
                    )
                except Exception as e:
                    logger.error("failed_to_init_sheets_client_in_check_job", error=str(e))
                
        redis_client = redis.from_url(settings.REDIS_URL)
        
        booking_service = BookingService(
            redis_client=redis_client,
            db_session=session,
            payment_client=payment_client,
            sheets_client=sheets_client,
            gcal_client=gcal_client,
            scheduler=scheduler_service
        )
        
        for p in pending_payments:
            order_id = p.invoice_id
            # Query CHECK_STATUS directly
            sign_string = f"{payment_client.merchant_account};{order_id}"
            signature = hmac.new(
                payment_client.secret_key.encode("utf-8"),
                sign_string.encode("utf-8"),
                hashlib.md5
            ).hexdigest()
            
            payload = {
                "transactionType": "CHECK_STATUS",
                "merchantAccount": payment_client.merchant_account,
                "merchantSignature": signature,
                "apiVersion": 1,
                "orderReference": order_id
            }
            
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(payment_client.base_url, json=payload)
                    if response.status_code == 200:
                        res_data = response.json()
                        status = res_data.get("transactionStatus")
                        if status == "Approved":
                            logger.info("pending_payment_found_approved_triggering_settlement", order_ref=order_id)
                            await booking_service.confirm_payment_and_booking(invoice_id=order_id)
                            await session.commit()
                        elif status in ("Declined", "Expired", "Voided", "Refunded"):
                            reason_code = res_data.get("reasonCode")
                            # WayForPay returns Declined + reasonCode 1151 for invoices awaiting payment
                            # and Declined + reasonCode 1127 for orders not yet found — these are NOT real failures
                            if str(reason_code) in ("1151", "1127"):
                                logger.info("pending_payment_still_awaiting", order_ref=order_id, reason_code=reason_code)
                            else:
                                logger.info("pending_payment_found_failed_triggering_rejection", order_ref=order_id, status=status, reason=reason_code)
                                await booking_service.reject_payment_and_booking(invoice_id=order_id, reason_code=reason_code)
                                await session.commit()
                        else:
                            logger.info("pending_payment_status_polled", order_ref=order_id, status=status)
            except Exception as wfp_err:
                logger.error("failed_to_poll_pending_payment_status", order_ref=order_id, error=str(wfp_err))
                
        await redis_client.close()
    logger.info("background_pending_payments_check_completed")
