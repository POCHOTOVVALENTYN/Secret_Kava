# app/admin/auth.py
from datetime import datetime, timedelta
from typing import Any
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from app.core.config import settings
from app.database.session import get_db_session
from app.database.models.user import User

logger = get_logger()

# Password hashing configuration using robust bcrypt
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/admin/login")

class TokenData(BaseModel):
    user_id: int
    role: str
    tenant_id: int

class CurrentAdminUser(BaseModel):
    id: int
    telegram_id: int
    role: str
    tenant_id: int

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies that clear text matches hashed passwords."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Generates strong cryptographic hashes for raw passwords."""
    return pwd_context.hash(password)

def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Signs secure HS256 JWT sessions for administrative users."""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    
    encoded_jwt = jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM
    )
    return encoded_jwt

async def get_current_active_admin(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db_session)
) -> CurrentAdminUser:
    """Dependency validator parsing active JWT payload and ensuring correct administrative privilege flags."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM]
        )
        user_id: int = int(payload.get("sub", 0))
        role: str = payload.get("role", "client")
        tenant_id: int = int(payload.get("tenant_id", 0))
        
        if user_id == 0 or role not in ["admin", "superadmin", "manager"]:
            raise credentials_exception
            
        token_data = TokenData(user_id=user_id, role=role, tenant_id=tenant_id)
    except jwt.PyJWTError:
        raise credentials_exception

    # Query active User details from database
    user_query = select(User).where(User.id == token_data.user_id)
    user_res = await db.execute(user_query)
    user = user_res.scalar_one_or_none()
    
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Помилка: Користувач заблокований або не існує"
        )
        
    return CurrentAdminUser(
        id=user.id,
        telegram_id=user.telegram_id,
        role=user.role,
        tenant_id=user.tenant_id
    )
