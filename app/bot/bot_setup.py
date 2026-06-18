# app/bot/bot_setup.py
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation, MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from redis.asyncio import Redis

from app.core.config import settings
from structlog import get_logger

logger = get_logger()

# 1. Initialize core Bot client
bot = Bot(token=settings.TELEGRAM_BOT_TOKEN.get_secret_value())

# 2. Configure FSM Storage backend (Robust Redis in production, memory fallback in dev/testing)
storage = MemoryStorage()

if settings.ENVIRONMENT == "production":
    try:
        redis_client = Redis.from_url(settings.REDIS_URL)
        # Using DefaultKeyBuilder with prefix to avoid namespace clashes
        storage = RedisStorage(
            redis=redis_client,
            key_builder=DefaultKeyBuilder(prefix="psy_bot_fsm")
        )
        logger.info("bot_storage_configured", backend="redis")
    except Exception as e:
        logger.critical("redis_fsm_storage_failed_falling_back_to_memory", error=str(e))
else:
    logger.info("bot_storage_configured", backend="memory")

# 3. Setup Dispatcher and event isolations
dp = Dispatcher(
    storage=storage,
    events_isolation=SimpleEventIsolation()
)

def register_global_middlewares(db_pool) -> None:
    """Helper method to inject session pools and generic middlewares later."""
    pass
