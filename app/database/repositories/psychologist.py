# app/database/repositories/psychologist.py
from typing import Sequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.psychologist import Psychologist
from app.database.repositories.base import BaseRepository

class PsychologistRepository(BaseRepository[Psychologist]):
    """Manages custom database selectors for psychologists scoped under specific tenants."""

    def __init__(self, session: AsyncSession):
        super().__init__(Psychologist, session)

    async def get_active_by_tenant(self, tenant_id: int) -> Sequence[Psychologist]:
        """Retrieves all active certified therapists scoped under a franchise tenant."""
        query = (
            select(Psychologist)
            .where(Psychologist.tenant_id == tenant_id)
            .where(Psychologist.is_active == True)
        )
        result = await self.session.execute(query)
        return result.scalars().all()
        
    async def create_for_tenant(
        self,
        tenant_id: int,
        name: str,
        bio: str,
        experience: int,
        specs: str,
        price_on: float,
        price_off: float,
        photo: str | None = None
    ) -> Psychologist:
        """Helper to create and commit a Psychologist profile under an administrative tenant."""
        psych = await self.create(
            tenant_id=tenant_id,
            name=name,
            bio=bio,
            experience_years=experience,
            specializations=specs,
            price_online=price_on,
            price_offline=price_off,
            photo_url=photo,
            is_active=True
        )
        return psych
