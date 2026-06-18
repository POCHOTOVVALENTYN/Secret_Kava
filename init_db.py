import asyncio
from app.database.session import engine
from app.database.models import Base

async def init_models():
    async with engine.begin() as conn:
        # Drop all tables first for a clean migration in dev environment
        await conn.run_sync(Base.metadata.drop_all)
        # Create all tables
        await conn.run_sync(Base.metadata.create_all)
    print("Database tables created successfully!")

if __name__ == "__main__":
    asyncio.run(init_models())
