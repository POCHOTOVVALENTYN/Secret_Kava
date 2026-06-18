# app/bot/middlewares/database.py
from collections.abc import Awaitable, Callable
from typing import Any
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from structlog import get_logger

logger = get_logger()

class DatabaseSessionMiddleware(BaseMiddleware):
    """Injects a transactional async SQLAlchemy session into the handler lifecycle context data."""

    def __init__(self, session_pool: async_sessionmaker[AsyncSession]):
        self.session_pool = session_pool

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        async with self.session_pool() as session:
            data["db_session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception as e:
                logger.error("middleware_session_failed_rolling_back", error=str(e))
                await session.rollback()
                raise
            finally:
                await session.close()
