from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.governance import PointLedger
    from app.models.project import Project
    from app.models.task import Task
    from app.models.tenant import Organization

from datetime import datetime, timezone
from flask_login import UserMixin
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.extensions import db


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('super_admin', 'project_lead', 'member')",
            name="valid_user_role",
        ),
        CheckConstraint(
            "current_tier IN ('slate', 'emerald', 'indigo', 'gold')",
            name="valid_user_tier",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    current_tier: Mapped[str] = mapped_column(
        String(20), nullable=False, default="slate"
    )
    ics_token_hash: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True, index=True
    )
    last_digest_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="users")
    led_projects: Mapped[list["Project"]] = relationship(
        back_populates="lead", foreign_keys="Project.lead_id"
    )
    assigned_tasks: Mapped[list["Task"]] = relationship(
        back_populates="assignee", foreign_keys="Task.assignee_id"
    )
    point_entries: Mapped[list["PointLedger"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"

    @property
    def is_project_lead(self) -> bool:
        return self.role == "project_lead"

    def get_id(self) -> str:
        return str(self.id)