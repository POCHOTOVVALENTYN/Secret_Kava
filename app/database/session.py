# app/database/session.py
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.core.config import settings
from structlog import get_logger

logger = get_logger()

# Configure high-performance async database pooling engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,  # Set to True only when debugging raw SQL queries
    pool_size=20,
    max_overflow=10,
    pool_timeout=30.0,
    pool_recycle=1800.0,  # Recycle connections every 30 minutes
    pool_pre_ping=True,   # Verifies connection availability before returning from pool
)

# Async session factory
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection helper that yields transactional async database sessions."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            logger.error("db_session_transaction_failed", error=str(e))
            await session.rollback()
            raise
        finally:
            await session.close()
