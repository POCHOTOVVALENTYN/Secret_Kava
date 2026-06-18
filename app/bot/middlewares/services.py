# app/bot/middlewares/services.py
from collections.abc import Awaitable, Callable
from typing import Any
import os
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from app.services.booking import BookingService
from app.integrations.google_cal import GoogleCalendarClient
from app.integrations.payments import WayForPayPaymentClient
from app.integrations.google_sheets import GoogleSheetsClient
from app.core.config import settings
import redis.asyncio as redis
from structlog import get_logger

logger = get_logger()

class ServicesMiddleware(BaseMiddleware):
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.gcal = None
        
        has_sa = settings.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(settings.GOOGLE_SERVICE_ACCOUNT_FILE)
        has_oauth = settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REFRESH_TOKEN
        
        if has_sa or has_oauth:
            try:
                self.gcal = GoogleCalendarClient(
                    client_id=settings.GOOGLE_CLIENT_ID,
                    client_secret=settings.GOOGLE_CLIENT_SECRET.get_secret_value() if settings.GOOGLE_CLIENT_SECRET else None,
                    refresh_token=settings.GOOGLE_REFRESH_TOKEN.get_secret_value() if settings.GOOGLE_REFRESH_TOKEN else None,
                    service_account_file=settings.GOOGLE_SERVICE_ACCOUNT_FILE
                )
            except Exception as e:
                logger.error("google_calendar_client_init_failed", error=str(e))
            
        self.sheets = None
        if settings.GOOGLE_SHEET_ID and (has_sa or has_oauth):
            try:
                self.sheets = GoogleSheetsClient(
                    spreadsheet_id=settings.GOOGLE_SHEET_ID,
                    client_id=settings.GOOGLE_CLIENT_ID,
                    client_secret=settings.GOOGLE_CLIENT_SECRET.get_secret_value() if settings.GOOGLE_CLIENT_SECRET else None,
                    refresh_token=settings.GOOGLE_REFRESH_TOKEN.get_secret_value() if settings.GOOGLE_REFRESH_TOKEN else None,
                    service_account_file=settings.GOOGLE_SERVICE_ACCOUNT_FILE
                )
            except Exception as e:
                logger.error("google_sheets_client_init_failed", error=str(e))
            
        self.payment = None
        if settings.WAYFORPAY_MERCHANT_ACCOUNT and settings.WAYFORPAY_MERCHANT_SECRET:
            self.payment = WayForPayPaymentClient(
                merchant_account=settings.WAYFORPAY_MERCHANT_ACCOUNT,
                secret_key=settings.WAYFORPAY_MERCHANT_SECRET.get_secret_value(),
                webhook_url=f"{settings.TELEGRAM_WEBHOOK_URL}/api/v1/payments/wayforpay/callback" if settings.TELEGRAM_WEBHOOK_URL else "",
                domain=settings.WAYFORPAY_MERCHANT_DOMAIN
            )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        db_session = data.get("db_session")
        if not db_session:
            return await handler(event, data)
            
        redis_client = redis.from_url(settings.REDIS_URL)
        
        booking_service = BookingService(
            redis_client=redis_client,
            db_session=db_session,
            gcal_client=self.gcal,
            payment_client=self.payment,
            sheets_client=self.sheets,
            scheduler=self.scheduler
        )
        
        data["booking_service"] = booking_service
        # Expose sheets client directly for admin handlers
        data["sheets"] = self.sheets
        data["payment"] = self.payment
        
        try:
            return await handler(event, data)
        finally:
            await redis_client.close()
