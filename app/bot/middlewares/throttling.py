# app/bot/middlewares/throttling.py
import time
from collections.abc import Awaitable, Callable
from typing import Any
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from structlog import get_logger

logger = get_logger()

class ThrottlingMiddleware(BaseMiddleware):
    """Simple in-memory rate-limiting (throttling) middleware to shield bot against flooding spikes."""

    def __init__(self, limit_seconds: float = 0.8):
        self.limit_seconds = limit_seconds
        # In-memory dictionary tracking last interaction time per user
        self.last_interactions: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        user_id = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id:
            current_time = time.time()
            last_time = self.last_interactions.get(user_id, 0.0)
            
            if current_time - last_time < self.limit_seconds:
                logger.warning("user_throttled", user_id=user_id)
                # Fail silently or notify (ignoring event to prevent spam answers)
                if isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Сповільнюйтесь! Ви натискаєте кнопки занадто часто.", show_alert=True)
                return None
                
            self.last_interactions[user_id] = current_time

        # Cleanup obsolete keys to prevent memory leak growth
        if len(self.last_interactions) > 10000:
            threshold = time.time() - 60
            self.last_interactions = {
                uid: t for uid, t in self.last_interactions.items() if t > threshold
            }

        return await handler(event, data)
