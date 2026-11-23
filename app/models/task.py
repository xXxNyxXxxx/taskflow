from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.governance import PointLedger
    from app.models.project import Project
    from app.models.user import User

from datetime import date, datetime, timezone
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.extensions import db


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Task(db.Model):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'critical')",
            name="valid_task_priority",
        ),
        CheckConstraint(
            "board_status IN ('todo', 'in_progress', 'review', 'done')",
            name="valid_board_status",
        ),
        CheckConstraint(
            "assignment_status IN ('unassigned', 'pending', 'confirmed', 'declined')",
            name="valid_assignment_status",
        ),
        CheckConstraint(
            "blocker_type IN ('client', 'internal') OR blocker_type IS NULL",
            name="valid_blocker_type",
        ),
        CheckConstraint(
            "(is_blocked IS FALSE AND blocker_type IS NULL) OR (is_blocked IS TRUE AND blocker_type IS NOT NULL)",
            name="blocker_type_required_when_blocked",
        ),
        CheckConstraint(
            "parent_task_id IS NULL OR parent_task_id != id",
            name="no_self_parent_task",
        ),
        Index("ix_tasks_project_board_status", "project_id", "board_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    board_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="todo", index=True
    )
    assignment_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unassigned", index=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Blocker & Extension Tracking
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocker_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    blocker_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    has_internal_extension: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Idempotent Recognition Lock
    points_awarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="tasks")
    assignee: Mapped["User | None"] = relationship(
        back_populates="assigned_tasks", foreign_keys=[assignee_id]
    )
    parent_task: Mapped["Task | None"] = relationship(
        remote_side=[id], back_populates="subtasks"
    )
    subtasks: Mapped[list["Task"]] = relationship(
        back_populates="parent_task", cascade="all, delete-orphan"
    )
    extensions: Mapped[list["DeadlineExtension"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    comments: Mapped[list["TaskComment"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    point_ledger_entry: Mapped["PointLedger | None"] = relationship(
        back_populates="task", uselist=False
    )

    @property
    def is_subtask(self) -> bool:
        return self.parent_task_id is not None


class DeadlineExtension(db.Model):
    __tablename__ = "deadline_extensions"
    __table_args__ = (
        CheckConstraint(
            "extension_type IN ('internal', 'client_blocked')",
            name="valid_extension_type",
        ),
        CheckConstraint("new_due_date > old_due_date", name="extension_moves_forward"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    old_due_date: Mapped[date] = mapped_column(Date, nullable=False)
    new_due_date: Mapped[date] = mapped_column(Date, nullable=False)
    extension_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    task: Mapped["Task"] = relationship(back_populates="extensions")


class TaskComment(db.Model):
    __tablename__ = "task_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    task: Mapped["Task"] = relationship(back_populates="comments")