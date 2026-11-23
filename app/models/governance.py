from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.task import Task
    from app.models.tenant import Organization
    from app.models.user import User

from datetime import datetime, timezone
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.extensions import db


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PointLedger(db.Model):
    __tablename__ = "point_ledger"
    __table_args__ = (
        CheckConstraint("base_points >= 0", name="non_negative_base_points"),
        CheckConstraint("bonus_points >= 0", name="non_negative_bonus_points"),
        CheckConstraint(
            "total_points = base_points + bonus_points",
            name="total_points_matches_sum",
        ),
        Index("ix_point_ledger_org_user_period", "organization_id", "user_id", "period_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # UNIQUE constraint guarantees a task can award points at most once
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    base_points: Mapped[int] = mapped_column(Integer, nullable=False)
    bonus_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_points: Mapped[int] = mapped_column(Integer, nullable=False)
    period_key: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    user: Mapped["User"] = relationship(back_populates="point_entries")
    task: Mapped["Task"] = relationship(back_populates="point_ledger_entry")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_org_created", "organization_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )

    organization: Mapped["Organization"] = relationship(back_populates="audit_logs")