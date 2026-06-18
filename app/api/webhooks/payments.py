# app/api/webhooks/payments.py
import os
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

from app.database.session import get_db_session
from app.services.booking import BookingService
from app.core.config import settings
from app.integrations.payments import WayForPayPaymentClient
from app.integrations.google_sheets import GoogleSheetsClient
from structlog import get_logger

logger = get_logger()
router = APIRouter(prefix="/payments", tags=["Payment Webhooks Gateway"])

async def get_redis_client() -> redis.Redis:
    """Helper connection dependency injector for Redis."""
    return redis.from_url(settings.REDIS_URL)

@router.post("/wayforpay/callback")
async def process_wayforpay_callback(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    r_client: redis.Redis = Depends(get_redis_client)
) -> dict:
    """Receives secure WayForPay checkout status events, settling orders dynamically."""
    
    payload = await request.json()
    logger.info("incoming_wayforpay_callback", payload=payload)

    # Instantiate Payment Client
    payment_client = WayForPayPaymentClient(
        merchant_account=settings.WAYFORPAY_MERCHANT_ACCOUNT or "",
        secret_key=settings.WAYFORPAY_MERCHANT_SECRET.get_secret_value() if settings.WAYFORPAY_MERCHANT_SECRET else "",
        webhook_url=f"{settings.TELEGRAM_WEBHOOK_URL}/api/v1/payments/wayforpay/callback" if settings.TELEGRAM_WEBHOOK_URL else "",
        domain=settings.WAYFORPAY_MERCHANT_DOMAIN
    )
    
    # 1. Cryptographic Signature validation
    if settings.ENVIRONMENT == "production":
        valid = payment_client.verify_webhook_signature(payload)
        if not valid:
            logger.error("invalid_wayforpay_webhook_signature", orderReference=payload.get("orderReference"))
            return {"error": "Signature validation failed"}
            
    # 2. Process invoice state transitions
    status = payload.get("status")
    order_ref = payload.get("orderReference")
    
    if status == "Approved":
        sheets_client = None
        has_sa = settings.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(settings.GOOGLE_SERVICE_ACCOUNT_FILE)
        has_oauth = settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REFRESH_TOKEN
        
        if settings.GOOGLE_SHEET_ID and (has_sa or has_oauth):
            try:
                sheets_client = GoogleSheetsClient(
                    spreadsheet_id=settings.GOOGLE_SHEET_ID,
                    client_id=settings.GOOGLE_CLIENT_ID,
                    client_secret=settings.GOOGLE_CLIENT_SECRET.get_secret_value() if settings.GOOGLE_CLIENT_SECRET else None,
                    refresh_token=settings.GOOGLE_REFRESH_TOKEN.get_secret_value() if settings.GOOGLE_REFRESH_TOKEN else None,
                    service_account_file=settings.GOOGLE_SERVICE_ACCOUNT_FILE
                )
            except Exception:
                pass
                
        booking_service = BookingService(
            redis_client=r_client,
            db_session=db,
            payment_client=payment_client,
            sheets_client=sheets_client,
            scheduler=request.app.state.scheduler
        )
        await booking_service.confirm_payment_and_booking(invoice_id=order_ref)
        logger.info("wayforpay_invoice_settled_successfully", orderReference=order_ref)
        
    return payment_client.generate_webhook_response(order_ref)
