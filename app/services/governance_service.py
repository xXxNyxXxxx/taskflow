from datetime import datetime, timezone
from app.extensions import db
from app.models import (
    AuditLog,
    DeadlineExtension,
    PointLedger,
    Project,
    ProjectShareLink,
    Task,
    TaskComment,
    User,
)


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

def _check_project_deletable(project: Project, actor: User) -> tuple[bool, str | None]:
    """Evaluates whether a project has zero external work/history and qualifies for hard-deletion."""
    if project.tasks:
        task_ids = [t.id for t in project.tasks]

        # 1. Any task assigned to someone other than the creator/actor?
        if any(t.assignee_id is not None and t.assignee_id != actor.id for t in project.tasks):
            return False, "Project has tasks assigned to team members. Archive it instead."

        # 2. Any task moved past 'todo' or marked blocked?
        if any(t.board_status != "todo" or t.is_blocked for t in project.tasks):
            return False, "Project has in-progress, blocked, or completed tasks. Archive it instead."

        # 3. Any comments, deadline extensions, or recognition points recorded?
        if TaskComment.query.filter(TaskComment.task_id.in_(task_ids)).count() > 0:
            return False, "Project contains task discussion history. Archive it instead."

        if DeadlineExtension.query.filter(DeadlineExtension.task_id.in_(task_ids)).count() > 0:
            return False, "Project contains deadline extension audit records. Archive it instead."

        if PointLedger.query.filter(PointLedger.task_id.in_(task_ids)).count() > 0:
            return False, "Project has awarded recognition points. Archive it instead."

    return True, None


def archive_project(
    *,
    project_id: int,
    actor: User,
    reason: str,
) -> Project:
    """Marks a project read-only ('archived'), revokes active client share links, and preserves all history."""
    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    if actor.role != "super_admin" and project.lead_id != actor.id:
        raise GovernanceServiceError("Only the Project Lead or Super Admin can archive this project.")

    if project.status == "archived":
        raise GovernanceServiceError("Project is already archived.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("Please provide an audit reason (at least 5 characters) to archive this project.")

    old_status = project.status
    project.status = "archived"

    # Revoke any active public client share links automatically
    active_links = ProjectShareLink.query.filter_by(
        project_id=project.id, is_revoked=False
    ).all()
    for link in active_links:
        link.is_revoked = True

    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            project_id=project.id,
            target_type="project",
            target_id=project.id,
            action="project_archived",
            reason=clean_reason,
            diff_payload={
                "before": {"status": old_status},
                "after": {"status": "archived", "revoked_share_links": len(active_links)},
            },
        )
    )
    db.session.commit()
    return project


def request_project_deletion(
    *,
    project_id: int,
    actor: User,
    reason: str,
) -> AuditLog:
    """Allows a Project Lead to request permanent deletion of an unused/accidental project from Super Admin."""
    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    if actor.role != "super_admin" and project.lead_id != actor.id:
        raise GovernanceServiceError("Only the assigned Project Lead can request project deletion.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("Please provide a clear reason (at least 5 characters) for the Super Admin.")

    is_deletable, block_reason = _check_project_deletable(project, actor)
    if not is_deletable:
        raise GovernanceServiceError(block_reason)

    # Prevent duplicate pending requests for the same project
    existing = AuditLog.query.filter_by(
        organization_id=actor.organization_id,
        project_id=project.id,
        action="project_delete_requested",
    ).first()
    if existing is not None:
        raise GovernanceServiceError("A deletion request for this project is already awaiting Super Admin review.")

    log_entry = AuditLog(
        organization_id=actor.organization_id,
        actor_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        action="project_delete_requested",
        reason=clean_reason,
        diff_payload={
            "project_title": project.title,
            "requested_by": actor.full_name,
            "status": "pending_review",
        },
    )
    db.session.add(log_entry)
    db.session.commit()
    return log_entry


def delete_empty_project(
    *,
    project_id: int,
    actor: User,
    reason: str,
) -> str:
    """Super Admin permanent purge of an empty/unused project with full audit trail preservation."""
    if actor.role != "super_admin":
        raise GovernanceServiceError("Only a Super Admin can permanently delete a project.")

    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("A clear audit reason (at least 5 characters) is required for permanent deletion.")

    is_deletable, block_reason = _check_project_deletable(project, actor)
    if not is_deletable:
        raise GovernanceServiceError(block_reason)

    deleted_title = project.title
    deleted_id = project.id

    # Nullify project_id on existing audit logs for this project before deleting row
    AuditLog.query.filter_by(
        organization_id=actor.organization_id, project_id=deleted_id
    ).update({"project_id": None})

    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            project_id=None,
            target_type="project",
            target_id=deleted_id,
            action="project_deleted",
            reason=clean_reason,
            diff_payload={
                "before": {"project_id": deleted_id, "title": deleted_title},
                "after": None,
            },
        )
    )
    db.session.delete(project)
    db.session.commit()
    return deleted_title