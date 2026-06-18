# app/database/models/review.py
from sqlalchemy import String, Integer, ForeignKey, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class Review(Base):
    """Stores customer satisfaction scores, comments, and approval moderation status."""
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    
    rating: Mapped[int] = mapped_column(Integer, nullable=False) # NPS scale (1-10) or Star rating (1-5)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    is_moderated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    user: Mapped["User"] = relationship("User")
