# app/admin/routes/psychologists.py
from typing import List, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db_session
from app.admin.auth import get_current_active_admin, CurrentAdminUser
from app.database.repositories.psychologist import PsychologistRepository

router = APIRouter(prefix="/admin/psychologists", tags=["Admin Psychologists Management"])

class PsychologistCreateDTO(BaseModel):
    name: str
    bio: str
    experience_years: int
    specializations: List[str]
    price_online: float
    price_offline: float
    photo_url: str | None = None

class PsychologistResponseDTO(PsychologistCreateDTO):
    id: int
    is_active: bool

@router.post("/", response_model=PsychologistResponseDTO, status_code=status.HTTP_201_CREATED)
async def register_new_psychologist(
    payload: PsychologistCreateDTO,
    current_admin: CurrentAdminUser = Depends(get_current_active_admin),
    db: AsyncSession = Depends(get_db_session)
) -> Any:
    """Enables managers to onboard new certified psychologists onto the tenant platform."""
    if current_admin.role not in ["admin", "superadmin", "manager"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недостатньо прав доступу для створення спеціаліста"
        )
        
    repo = PsychologistRepository(db)
    try:
        new_psych = await repo.create_for_tenant(
            tenant_id=current_admin.tenant_id,
            name=payload.name,
            bio=payload.bio,
            experience=payload.experience_years,
            specs=",".join(payload.specializations),
            price_on=payload.price_online,
            price_off=payload.price_offline,
            photo=payload.photo_url
        )
        
        # Build response manually
        return PsychologistResponseDTO(
            id=new_psych.id,
            name=new_psych.name,
            bio=new_psych.bio,
            experience_years=new_psych.experience_years,
            specializations=new_psych.specializations.split(","),
            price_online=float(new_psych.price_online),
            price_offline=float(new_psych.price_offline),
            photo_url=new_psych.photo_url,
            is_active=new_psych.is_active
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Помилка при створенні запису: {str(e)}"
        )
