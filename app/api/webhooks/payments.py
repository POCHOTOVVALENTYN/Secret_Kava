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
    status = payload.get("transactionStatus") or payload.get("status")
    order_ref = payload.get("orderReference")
    
    # Fail-safe: if status is Pending or InProcessing, query WayForPay CHECK_STATUS API directly
    if status in ("Pending", "InProcessing"):
        try:
            import hmac, hashlib, httpx
            sign_string = f"{payment_client.merchant_account};{order_ref}"
            signature = hmac.new(
                payment_client.secret_key.encode("utf-8"),
                sign_string.encode("utf-8"),
                hashlib.md5
            ).hexdigest()
            
            check_payload = {
                "transactionType": "CHECK_STATUS",
                "merchantAccount": payment_client.merchant_account,
                "merchantSignature": signature,
                "apiVersion": 1,
                "orderReference": order_ref
            }
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(payment_client.base_url, json=check_payload)
                if res.status_code == 200:
                    res_data = res.json()
                    checked_status = res_data.get("transactionStatus", status)
                    checked_reason = str(res_data.get("reasonCode", ""))
                    logger.info("wayforpay_queried_status", orderReference=order_ref, status=checked_status, reasonCode=checked_reason)
                    # WayForPay returns Declined + 1151 for invoices awaiting payment — NOT a real failure
                    if checked_status == "Declined" and checked_reason in ("1151", "1127"):
                        logger.info("wayforpay_invoice_still_awaiting_payment", orderReference=order_ref)
                    else:
                        status = checked_status
        except Exception as check_err:
            logger.error("failed_to_query_wayforpay_status", error=str(check_err))
            
    # Instantiate BookingService first
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
            logger.error("failed_to_init_gcal_client_in_webhook", error=str(e))
            
        if settings.GOOGLE_SHEET_ID:
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
        gcal_client=gcal_client,
        scheduler=request.app.state.scheduler
    )

    if status == "Approved":
        await booking_service.confirm_payment_and_booking(invoice_id=order_ref)
        logger.info("wayforpay_invoice_settled_successfully", orderReference=order_ref)
    elif status in ("Declined", "Expired", "Voided", "Refunded"):
        reason_code = payload.get("reasonCode")
        # Skip false declines: 1151 = awaiting payment, 1127 = order not found
        if str(reason_code) not in ("1151", "1127"):
            await booking_service.reject_payment_and_booking(invoice_id=order_ref, reason_code=reason_code)
            logger.info("wayforpay_invoice_rejected_successfully", orderReference=order_ref, status=status, reason=reason_code)
        else:
            logger.info("wayforpay_webhook_ignored_awaiting_status", orderReference=order_ref, reasonCode=reason_code)
        
    return payment_client.generate_webhook_response(order_ref)

