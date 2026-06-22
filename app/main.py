# app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status, Response, Header
from aiogram import types
import redis.asyncio as redis

from app.core.config import settings
from app.core.logger import setup_logging
from app.database.session import async_session_factory
from app.bot.bot_setup import bot, dp
from app.bot.middlewares.database import DatabaseSessionMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.auth import AuthRegistrationMiddleware
from app.bot.middlewares.services import ServicesMiddleware
from app.bot.handlers import start, booking, rental, events, mac_cards, reviews, admin as admin_handler, womens_circle, host_event, payment_retry
from app.scheduler.jobs import SchedulerService
from app.api.webhooks.payments import router as payment_webhook_router
from app.admin.routes.psychologists import router as admin_psych_router
from structlog import get_logger

logger = get_logger()

# 1. Setup Structured Logging configuration
setup_logging()

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """Handles async setups, webhooks publishing, and plans reminders threads."""
    logger.info("application_startup_sequence_initiated")
    
    # Register global bot middlewares
    dp.update.outer_middleware.register(DatabaseSessionMiddleware(async_session_factory))
    dp.update.outer_middleware.register(ThrottlingMiddleware(limit_seconds=0.8))
    dp.update.outer_middleware.register(AuthRegistrationMiddleware())
    
    # Initialize background task scheduler
    scheduler = SchedulerService(bot=bot, redis_url=settings.REDIS_URL)
    scheduler.start()
    app.state.scheduler = scheduler
    
    dp.update.outer_middleware.register(ServicesMiddleware(scheduler))
    
    # Register standard event routers
    dp.include_router(start.router)
    dp.include_router(booking.router)
    dp.include_router(rental.router)
    dp.include_router(events.router)
    dp.include_router(mac_cards.router)
    dp.include_router(reviews.router)
    dp.include_router(womens_circle.router)
    dp.include_router(admin_handler.router)
    dp.include_router(host_event.router)
    dp.include_router(payment_retry.router)
    
    # Configure Telegram webhook mapping
    if settings.TELEGRAM_WEBHOOK_URL:
        webhook_info = await bot.get_webhook_info()
        target_webhook = f"{settings.TELEGRAM_WEBHOOK_URL}/webhooks/telegram"
        
        if webhook_info.url != target_webhook:
            await bot.set_webhook(
                url=target_webhook,
                secret_token=settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
            )
            logger.info("telegram_webhook_published", url=target_webhook)
    else:
        logger.warning("webhook_url_not_configured_polling_mode_suggested")
        
    yield
    
    # Shutdown sequence
    scheduler.shutdown()
    if settings.TELEGRAM_WEBHOOK_URL:
        await bot.delete_webhook()
        logger.info("telegram_webhook_retracted")
        
    await bot.session.close()
    logger.info("application_shutdown_sequence_completed")

# 2. FastAPI app definition
app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=app_lifespan,
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None
)

# Register external integration routers
app.include_router(payment_webhook_router, prefix="/api/v1")
app.include_router(admin_psych_router, prefix="/api/v1")

@app.get("/health", status_code=status.HTTP_200_OK, tags=["System Telemetry"])
async def system_health_status() -> dict[str, str]:
    """Basic healthcheck ping indicator."""
    return {"status": "healthy", "service": settings.PROJECT_NAME}

@app.post("/webhooks/telegram", tags=["Bot Webhooks Gateway"])
async def process_telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(None, alias="X-Telegram-Bot-Api-Secret-Token")
) -> Response:
    """Entry point for inbound Telegram updates. Protected via secret webhook tokens."""
    
    # Verify secure secret token against configuration settings
    if settings.ENVIRONMENT == "production" and settings.TELEGRAM_WEBHOOK_SECRET:
        configured_secret = settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
        if x_telegram_bot_api_secret_token != configured_secret:
            logger.error("invalid_telegram_webhook_secret_header_received")
            return Response(status_code=status.HTTP_403_FORBIDDEN)
            
    # Process updates asynchronously using aiogram
    update_data = await request.json()
    telegram_update = types.Update(**update_data)
    msg_text = telegram_update.message.text if telegram_update.message else None
    logger.info("incoming_telegram_update", update_id=telegram_update.update_id, has_message=bool(telegram_update.message), has_callback=bool(telegram_update.callback_query), text=msg_text)
    try:
        await dp.feed_update(bot, telegram_update)
    except Exception as e:
        logger.exception("telegram_webhook_processing_failed", error=str(e))
        
    return Response(status_code=status.HTTP_200_OK)
