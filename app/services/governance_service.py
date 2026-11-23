from datetime import datetime, timezone
from app.extensions import db
from app.models import AuditLog, Project, Task, User


class GovernanceServiceError(Exception):
    """Domain validation error surfaced cleanly to Super Admin / Lead forms."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def transfer_project_lead(
    *,
    project_id: int,
    new_lead_id: int,
    actor: User,
    reason: str,
) -> Project:
    """Super Admin emergency handover of Project ownership with mandatory audit trail."""
    if actor.role != "super_admin":
        raise GovernanceServiceError("Only a Super Admin can perform a Project Lead handover.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("A clear reason (at least 5 characters) is required for lead handover.")

    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    new_lead = User.query.filter_by(
        id=new_lead_id, organization_id=actor.organization_id, is_active=True
    ).first()
    if new_lead is None:
        raise GovernanceServiceError("Target user must be an active member of your workspace.")

    if new_lead.role not in ("super_admin", "project_lead"):
        raise GovernanceServiceError("Target user must hold a Project Lead or Super Admin role.")

    if project.lead_id == new_lead.id:
        raise GovernanceServiceError("Selected user is already the lead for this project.")

    old_lead_id = project.lead_id
    old_lead_name = project.lead.full_name if project.lead else "Unknown"
    project.lead_id = new_lead.id

    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            project_id=project.id,
            target_type="project",
            target_id=project.id,
            action="lead_transferred",
            reason=clean_reason,
            diff_payload={
                "before": {"lead_id": old_lead_id, "lead_name": old_lead_name},
                "after": {"lead_id": new_lead.id, "lead_name": new_lead.full_name},
            },
        )
    )
    db.session.commit()
    return project


def update_member_role(
    *,
    target_user_id: int,
    new_role: str,
    actor: User,
    reason: str = "Updated workspace member role.",
) -> User:
    """Promotes or demotes an active workspace user while preventing last-admin lockout."""
    if actor.role != "super_admin":
        raise GovernanceServiceError("Only a Super Admin can modify member roles.")

    if new_role not in ("super_admin", "project_lead", "member"):
        raise GovernanceServiceError("Invalid role specified.")

    target = User.query.filter_by(
        id=target_user_id, organization_id=actor.organization_id, is_active=True
    ).first()
    if target is None:
        raise GovernanceServiceError("Active workspace user not found.")

    if target.role == new_role:
        return target

    # Prevent demoting the sole active Super Admin
    if target.role == "super_admin" and new_role != "super_admin":
        active_admins = User.query.filter_by(
            organization_id=actor.organization_id, role="super_admin", is_active=True
        ).count()
        if active_admins <= 1:
            raise GovernanceServiceError("Cannot demote the only active Super Admin in the workspace.")

    old_role = target.role
    target.role = new_role

    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            target_type="user",
            target_id=target.id,
            action="role_changed",
            reason=(reason or "").strip() or "Updated workspace member role.",
            diff_payload={"before": {"role": old_role}, "after": {"role": new_role}},
        )
    )
    db.session.commit()
    return target


def soft_remove_member(
    *,
    target_user_id: int,
    actor: User,
    reason: str,
) -> User:
    """Deactivates a user (`is_active=False`), unassigns open tasks, and preserves historical records."""
    if actor.role != "super_admin":
        raise GovernanceServiceError("Only a Super Admin can deactivate workspace members.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("A clear reason (at least 5 characters) is required when removing a user.")

    target = User.query.filter_by(
        id=target_user_id, organization_id=actor.organization_id, is_active=True
    ).first()
    if target is None:
        raise GovernanceServiceError("Active workspace user not found.")

    if target.id == actor.id:
        raise GovernanceServiceError("You cannot deactivate your own Super Admin account.")

    # Block deactivation if user still leads active projects (forces explicit handover first)
    led_projects = Project.query.filter_by(
        organization_id=actor.organization_id, lead_id=target.id, status="active"
    ).count()
    if led_projects > 0:
        raise GovernanceServiceError(
            f"User still leads {led_projects} active project(s). Transfer project ownership before deactivating."
        )

    # Unassign open tasks so cards return cleanly to the queue without deleting history
    open_tasks = (
        Task.query.join(Project, Task.project_id == Project.id)
        .filter(
            Project.organization_id == actor.organization_id,
            Task.assignee_id == target.id,
            Task.board_status != "done",
        )
        .all()
    )
    unassigned_ids = []
    for task in open_tasks:
        task.assignee_id = None
        task.assignment_status = "unassigned"
        task.assigned_at = None
        unassigned_ids.append(task.id)

    target.is_active = False

    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            target_type="user",
            target_id=target.id,
            action="user_deactivated",
            reason=clean_reason,
            diff_payload={
                "before": {"is_active": True},
                "after": {"is_active": False, "unassigned_task_ids": unassigned_ids},
            },
        )
    )
    db.session.commit()
    return target


def query_audit_logs(
    *,
    organization_id: int,
    action: str | None = None,
    project_id: int | None = None,
    limit: int = 100,
) -> list[AuditLog]:
    """Returns immutable tenant-scoped audit trail entries ordered newest first."""
    q = AuditLog.query.filter_by(organization_id=organization_id)
    if action:
        q = q.filter_by(action=action.strip())
    if project_id:
        q = q.filter_by(project_id=project_id)
    return q.order_by(AuditLog.created_at.desc()).limit(min(limit, 500)).all()