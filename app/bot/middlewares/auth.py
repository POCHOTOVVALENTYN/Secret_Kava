# app/bot/middlewares/auth.py
from collections.abc import Awaitable, Callable
from typing import Any
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.repositories.user import UserRepository
from app.database.models.tenant import Tenant
from sqlalchemy import select
from structlog import get_logger

logger = get_logger()

class AuthRegistrationMiddleware(BaseMiddleware):
    """Automatically records/registers client users in PostgreSQL database upon contact."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        user_info = None
        from aiogram.types import Update
        if isinstance(event, Update):
            if event.message and event.message.from_user:
                user_info = event.message.from_user
            elif event.callback_query and event.callback_query.from_user:
                user_info = event.callback_query.from_user
        elif isinstance(event, Message) and event.from_user:
            user_info = event.from_user
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_info = event.from_user

        db_session: AsyncSession = data.get("db_session") # type: ignore
        if db_session:
            # Auto seed default tenant so registration doesn't break
            tenant_query = select(Tenant).limit(1)
            tenant_res = await db_session.execute(tenant_query)
            default_tenant = tenant_res.scalar_one_or_none()
            
            if not default_tenant:
                default_tenant = Tenant(
                    name="Головна студія",
                    slug="main-studio",
                    is_active=True
                )
                db_session.add(default_tenant)
                await db_session.flush()

            # Auto seed default room to prevent ForeignKeyViolationError for room_bookings
            from app.database.models.booking import Room
            room_query = select(Room).where(Room.id == 1)
            room_res = await db_session.execute(room_query)
            default_room = room_res.scalar_one_or_none()
            if not default_room:
                default_room = Room(
                    id=1,
                    tenant_id=default_tenant.id,
                    name="Головний кабінет",
                    description="М'які терапевтичні крісла, торшер, фліпчарт, папір.",
                    hourly_rate=200.0,
                    is_active=True
                )
                db_session.add(default_room)
                await db_session.flush()

            room2_query = select(Room).where(Room.id == 2)
            room2_res = await db_session.execute(room2_query)
            room2 = room2_res.scalar_one_or_none()
            if not room2:
                room2 = Room(
                    id=2,
                    tenant_id=default_tenant.id,
                    name="Зал для заходів",
                    description="Оренда залу для групових заходів",
                    hourly_rate=500.0,
                    is_active=True
                )
                db_session.add(room2)
                await db_session.flush()

            # Auto seed default psychologist
            from app.database.models.psychologist import Psychologist
            psych_query = select(Psychologist).limit(1)
            psych_res = await db_session.execute(psych_query)
            default_psych = psych_res.scalar_one_or_none()
            if not default_psych:
                default_psych = Psychologist(
                    tenant_id=default_tenant.id,
                    name="Анна Зозуля",
                    bio="Засновниця, психотерапевт",
                    experience_years=10,
                    specializations="Психолог",
                    price_online=1000.0,
                    price_offline=1200.0,
                    is_active=True
                )
                db_session.add(default_psych)
                await db_session.flush()

            if user_info and not user_info.is_bot:
                user_repo = UserRepository(db_session)
                user = await user_repo.get_by_telegram_id(user_info.id)
                
                if not user:
                    # Save new client record
                    user = await user_repo.create(
                        telegram_id=user_info.id,
                        username=user_info.username,
                        first_name=user_info.first_name,
                        last_name=user_info.last_name,
                        role="client",
                        tenant_id=default_tenant.id
                    )
                    logger.info("new_user_auto_registered", user_id=user_info.id, tenant=default_tenant.slug)
                
                # Expose user model to handler parameters
                data["current_user"] = user

        return await handler(event, data)
