# app/database/repositories/user.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User
from app.database.repositories.base import BaseRepository

class UserRepository(BaseRepository[User]):
    """Manages custom queries for the User table including Telegram unique lookup scopes."""

    def __init__(self, session: AsyncSession):
        super().__init__(User, session)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Retrieves a client user scope matching their active Telegram user ID."""
        query = select(User).where(User.telegram_id == telegram_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
