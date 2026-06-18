# app/database/repositories/base.py
from typing import Generic, TypeVar, Sequence
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.base import Base

ModelType = TypeVar("ModelType", bound=Base)

class BaseRepository(Generic[ModelType]):
    """Generic async repository providing unified SQL operations for database models."""

    def __init__(self, model: type[ModelType], session: AsyncSession):
        self.model = model
        self.session = session

    async def get_by_id(self, id_val: int) -> ModelType | None:
        """Retrieves a single record by its primary key ID."""
        query = select(self.model).where(self.model.id == id_val) # type: ignore
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_all(self, limit: int = 100, offset: int = 0) -> Sequence[ModelType]:
        """Retrieves a slice list of records with pagination bounds."""
        query = select(self.model).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def create(self, **kwargs) -> ModelType:
        """Inserts a new record into the database."""
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def update(self, id_val: int, **kwargs) -> ModelType | None:
        """Updates fields of an existing record matched by ID."""
        query = (
            update(self.model)
            .where(self.model.id == id_val) # type: ignore
            .values(**kwargs)
            .returning(self.model)
        )
        result = await self.session.execute(query)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def delete(self, id_val: int) -> bool:
        """Deletes a record matching primary key ID."""
        query = delete(self.model).where(self.model.id == id_val) # type: ignore
        result = await self.session.execute(query)
        return (result.rowcount or 0) > 0
