from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.governance import AuditLog
    from app.models.project import Project, ProjectTemplate
    from app.models.user import User


from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.extensions import db


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_column_labels() -> dict:
    return {
        "todo": "To Do",
        "in_progress": "In Progress",
        "review": "In Review",
        "done": "Done",
    }


def default_priority_weights() -> dict:
    return {
        "low": 10,
        "medium": 25,
        "high": 50,
        "critical": 100,
    }


def default_tier_thresholds() -> dict:
    return {
        "slate": 0,
        "emerald": 250,
        "indigo": 750,
        "gold": 2000,
    }


class Organization(db.Model):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    # Relationships
    settings: Mapped["OrganizationSettings"] = relationship(
        back_populates="organization",
        uselist=False,
        cascade="all, delete-orphan",
    )
    users: Mapped[list["User"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    projects: Mapped[list["Project"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    templates: Mapped[list["ProjectTemplate"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    invite_codes: Mapped[list["InviteCode"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )


class OrganizationSettings(db.Model):
    __tablename__ = "organization_settings"
    __table_args__ = (
        CheckConstraint(
            "acceptance_mode IN ('strict', 'direct')",
            name="valid_acceptance_mode",
        ),
        CheckConstraint(
            "pending_sla_hours BETWEEN 1 AND 720",
            name="valid_pending_sla_hours",
        ),
        CheckConstraint(
            "invite_expiry_hours BETWEEN 1 AND 720",
            name="valid_invite_expiry_hours",
        ),
        CheckConstraint(
            "on_time_bonus_pct BETWEEN 0 AND 100",
            name="valid_on_time_bonus_pct",
        ),
        CheckConstraint(
            "internal_ext_penalty_pct BETWEEN 0 AND 100",
            name="valid_internal_ext_penalty_pct",
        ),
        CheckConstraint(
            "tier_reset_cadence IN ('lifetime', 'monthly', 'quarterly')",
            name="valid_tier_reset_cadence",
        ),
        CheckConstraint(
            "daily_digest_mode IN ('off', 'banner', 'banner_and_email')",
            name="valid_daily_digest_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    # White-Label Branding (Super Admin only)
    brand_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    logo_public_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    theme_preset: Mapped[str] = mapped_column(
        String(30), nullable=False, default="calm_sage"
    )
    custom_accent_hex: Mapped[str | None] = mapped_column(String(7), nullable=True)

    # Workflow & Governance Toggles
    acceptance_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="strict"
    )
    pending_sla_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=48)
    require_review_gate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    allow_client_share_links: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    invite_expiry_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=72
    )
    daily_digest_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="banner"
    )

    # Kanban Column Display Mapping
    column_labels: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=default_column_labels
    )

    # Recognition & Performance Tier Engine
    recognition_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    priority_weights: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=default_priority_weights
    )
    on_time_bonus_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    internal_ext_penalty_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50
    )
    tier_thresholds: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=default_tier_thresholds
    )
    tier_reset_cadence: Mapped[str] = mapped_column(
        String(20), nullable=False, default="lifetime"
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    organization: Mapped["Organization"] = relationship(back_populates="settings")

    @property
    def effective_brand_name(self) -> str:
        return self.brand_name or (
            self.organization.name if self.organization else "TaskFlow"
        )


@event.listens_for(Organization, "after_insert")
def auto_create_organization_settings(mapper, connection, target):
    """Guarantees every new Organization has default settings immediately."""
    connection.execute(
        OrganizationSettings.__table__.insert().values(
            organization_id=target.id,
            theme_preset="calm_sage",
            acceptance_mode="strict",
            pending_sla_hours=48,
            require_review_gate=True,
            allow_client_share_links=True,
            invite_expiry_hours=72,
            daily_digest_mode="banner",
            column_labels=default_column_labels(),
            recognition_enabled=True,
            priority_weights=default_priority_weights(),
            on_time_bonus_pct=20,
            internal_ext_penalty_pct=50,
            tier_thresholds=default_tier_thresholds(),
            tier_reset_cadence="lifetime",
            updated_at=utc_now(),
        )
    )


class InviteCode(db.Model):
    __tablename__ = "invite_codes"
    __table_args__ = (
        CheckConstraint(
            "assigned_role IN ('project_lead', 'member')",
            name="valid_invite_role",
        ),
        CheckConstraint("max_uses >= 1", name="positive_max_uses"),
        CheckConstraint("use_count >= 0 AND use_count <= max_uses", name="valid_use_count"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    assigned_role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="member"
    )
    created_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    used_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    organization: Mapped["Organization"] = relationship(back_populates="invite_codes")