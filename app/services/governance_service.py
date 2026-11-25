from app.extensions import db
from app.models import (
    AuditLog,
    PointLedger,
    Project,
    ProjectShareLink,
    Task,
    User,
)


class GovernanceServiceError(Exception):
    """Domain validation error surfaced cleanly to routes."""


def _assert_no_completed_or_awarded_work(project: Project) -> None:
    """Blocks permanent deletion if any task is Done or has PointLedger entries."""
    completed_tasks = [t for t in project.tasks if t.board_status == "done"]
    if completed_tasks:
        raise GovernanceServiceError(
            f"Cannot permanently delete: {len(completed_tasks)} task(s) are already Done. Archive the project instead."
        )

    task_ids = [t.id for t in project.tasks]
    if task_ids:
        awarded_points = PointLedger.query.filter(PointLedger.task_id.in_(task_ids)).first()
        if awarded_points is not None:
            raise GovernanceServiceError(
                "Cannot permanently delete: recognition points have already been recorded on this project. Archive it instead."
            )


# ============================================================================
# 1. Project Archiving & Role-Tiered Unarchiving
# ============================================================================

def archive_project(
    *,
    project_id: int,
    actor: User,
    reason: str,
) -> Project:
    """Locks a project into read-only archived mode while preserving all history & points."""
    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    if actor.role != "super_admin" and project.lead_id != actor.id:
        raise GovernanceServiceError("Only the Project Lead or Super Admin can archive this project.")

    if project.status == "archived":
        raise GovernanceServiceError("This project is already archived.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("Please provide an audit reason (at least 5 characters) to archive this project.")

    old_status = project.status
    project.status = "archived"

    # Revoke any active public client share links automatically
    active_links = ProjectShareLink.query.filter_by(project_id=project.id).all()
    revoked_count = len(active_links)
    for link in active_links:
        db.session.delete(link)

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
                "after": {"status": "archived", "revoked_share_links": revoked_count},
            },
        )
    )
    db.session.commit()
    return project


def unarchive_project(
    *,
    project_id: int,
    actor: User,
    reason: str,
) -> Project:
    """Super Admins have unlimited unarchives; Project Leads are capped at 1 reopen."""
    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    if actor.role != "super_admin" and project.lead_id != actor.id:
        raise GovernanceServiceError("Only the Project Lead or Super Admin can reopen an archived project.")

    if project.status != "archived":
        raise GovernanceServiceError("Only archived projects can be reopened.")

    prior_reopens = AuditLog.query.filter_by(
        organization_id=actor.organization_id,
        project_id=project.id,
        action="project_unarchived",
    ).count()
    if actor.role != "super_admin" and prior_reopens >= 1:
        raise GovernanceServiceError(
            "One-time reopen limit reached for Project Leads. Contact a Super Admin to reopen this project again."
        )

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("Please provide an audit reason (at least 5 characters) to reopen this project.")

    project.status = "active"
    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            project_id=project.id,
            target_type="project",
            target_id=project.id,
            action="project_unarchived",
            reason=clean_reason,
            diff_payload={
                "before": {"status": "archived"},
                "after": {"status": "active", "reopen_count": prior_reopens + 1},
            },
        )
    )
    db.session.commit()
    return project


# ============================================================================
# 2. Dual-Control Project Deletion Request & Approved Force-Purge
# ============================================================================

def request_project_deletion(
    *,
    project_id: int,
    actor: User,
    reason: str,
) -> AuditLog:
    """Allows a Project Lead to request deletion of a project where no work has been completed."""
    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    if actor.role != "super_admin" and project.lead_id != actor.id:
        raise GovernanceServiceError("Only the Project Lead can request project deletion.")

    existing_req = AuditLog.query.filter_by(
        organization_id=actor.organization_id,
        project_id=project.id,
        action="project_delete_requested",
    ).first()
    if existing_req is not None:
        raise GovernanceServiceError("A deletion request for this project is already awaiting Super Admin review.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("Please provide a clear reason (at least 5 characters) for deletion.")

    _assert_no_completed_or_awarded_work(project)

    entry = AuditLog(
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
            "task_count": len(project.tasks),
        },
    )
    db.session.add(entry)
    db.session.commit()
    return entry


def reject_project_deletion(
    *,
    project_id: int,
    actor: User,
    reason: str,
) -> Project:
    """Allows a Super Admin to decline a Lead's deletion request and clear the pending badge."""
    if actor.role != "super_admin":
        raise GovernanceServiceError("Only a Super Admin can review deletion requests.")

    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 3:
        raise GovernanceServiceError("Please provide a brief reason when declining a deletion request.")

    pending_reqs = AuditLog.query.filter_by(
        organization_id=actor.organization_id,
        project_id=project.id,
        action="project_delete_requested",
    ).all()
    if not pending_reqs:
        raise GovernanceServiceError("No pending deletion request found for this project.")

    for req in pending_reqs:
        req.action = "project_delete_request_closed"

    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            project_id=project.id,
            target_type="project",
            target_id=project.id,
            action="project_delete_rejected",
            reason=clean_reason,
            diff_payload={"project_title": project.title},
        )
    )
    db.session.commit()
    return project


def delete_empty_project(
    *,
    project_id: int,
    actor: User,
    reason: str,
) -> str:
    """Super Admin permanent purge: enforces dual-control Lead request & cascade-cleans unstarted tasks."""
    if actor.role != "super_admin":
        raise GovernanceServiceError("Only a Super Admin can permanently delete a project.")

    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    pending_req = AuditLog.query.filter_by(
        organization_id=actor.organization_id,
        project_id=project.id,
        action="project_delete_requested",
    ).first()

    # Dual-control check: Super Admin cannot delete another Lead's project unless requested
    if project.lead_id != actor.id and pending_req is None:
        raise GovernanceServiceError(
            "This project belongs to another Project Lead. They must submit a deletion request before you can purge it."
        )

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("A clear audit reason (at least 5 characters) is required for permanent deletion.")

    _assert_no_completed_or_awarded_work(project)

    # Direct Super Admin purge (without a Lead request) still requires 0 tasks
    if pending_req is None and len(project.tasks) > 0:
        raise GovernanceServiceError(
            "Project has active tasks. Archive it instead, or clear all tasks before purging."
        )

    deleted_title = project.title
    deleted_id = project.id
    purged_task_count = len(project.tasks)

    # Clear self-referential parent_task_id first so SQLite/PostgreSQL deletes tasks cleanly in any order
    Task.query.filter_by(project_id=deleted_id).update({"parent_task_id": None})

    # Nullify project_id on historical audit logs so the immutable audit trail survives deletion
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
                "before": {
                    "project_id": deleted_id,
                    "title": deleted_title,
                    "purged_unstarted_tasks": purged_task_count,
                },
                "after": None,
            },
        )
    )
    db.session.delete(project)
    db.session.commit()
    return deleted_title


# ============================================================================
# 3. Emergency Project Lead Handover, Soft-Removal & Audit Query
# ============================================================================

def transfer_project_lead(
    *,
    project_id: int,
    new_lead_id: int,
    actor: User,
    reason: str,
) -> Project:
    """Super Admin emergency project lead transfer with mandatory audit logging."""
    if actor.role != "super_admin":
        raise GovernanceServiceError("Only a Super Admin can perform an emergency Project Lead handover.")

    project = Project.query.filter_by(
        id=project_id, organization_id=actor.organization_id
    ).first()
    if project is None:
        raise GovernanceServiceError("Project not found in your workspace.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("An audit reason (at least 5 characters) is required for lead handover.")

    new_lead = User.query.filter_by(
        id=new_lead_id, organization_id=actor.organization_id, is_active=True
    ).first()
    if new_lead is None or new_lead.role not in ("super_admin", "project_lead"):
        raise GovernanceServiceError("Replacement lead must be an active Project Lead or Super Admin.")

    old_lead_id = project.lead_id
    if old_lead_id == new_lead.id:
        raise GovernanceServiceError("Selected user is already the lead of this project.")

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
                "before": {"lead_id": old_lead_id},
                "after": {"lead_id": new_lead.id},
            },
        )
    )
    db.session.commit()
    return project


def soft_remove_member(
    *,
    target_user_id: int,
    actor: User,
    reason: str,
) -> User:
    """Soft-removes a user, blocks deactivation if they lead active projects, and unassigns open tasks."""
    if actor.role != "super_admin":
        raise GovernanceServiceError("Only a Super Admin can deactivate workspace members.")

    target = User.query.filter_by(
        id=target_user_id, organization_id=actor.organization_id
    ).first()
    if target is None:
        raise GovernanceServiceError("User not found in your workspace.")

    if target.id == actor.id:
        raise GovernanceServiceError("You cannot deactivate your own Super Admin account.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise GovernanceServiceError("An audit reason (at least 5 characters) is required to deactivate a user.")

    active_led_projects = Project.query.filter_by(
        organization_id=actor.organization_id,
        lead_id=target.id,
        status="active",
    ).all()
    if active_led_projects:
        titles = ", ".join(p.title for p in active_led_projects)
        raise GovernanceServiceError(
            f"Cannot deactivate {target.full_name} while they lead active project(s): {titles}. Transfer project ownership before deactivating."
        )

    target.is_active = False

    # Unassign all open (non-Done) tasks assigned to this member across the workspace
    open_tasks = (
        Task.query.join(Project, Task.project_id == Project.id)
        .filter(
            Project.organization_id == actor.organization_id,
            Task.assignee_id == target.id,
            Task.board_status != "done",
        )
        .all()
    )
    for t in open_tasks:
        t.assignee_id = None
        t.assignment_status = "unassigned"

    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            project_id=None,
            target_type="user",
            target_id=target.id,
            action="user_deactivated",
            reason=clean_reason,
            diff_payload={
                "before": {"is_active": True},
                "after": {
                    "is_active": False,
                    "unassigned_open_tasks": len(open_tasks),
                },
            },
        )
    )
    db.session.commit()
    return target


# Alias preserved in case any test imports deactivate_member_with_governance
deactivate_member_with_governance = soft_remove_member

def update_member_role(
    *,
    target_user_id: int,
    new_role: str,
    actor: User,
    reason: str | None = None,
) -> User:
    """Allows a Super Admin to promote/demote workspace members while guarding active project leads."""
    if actor.role != "super_admin":
        raise GovernanceServiceError("Only a Super Admin can update member roles.")

    if new_role not in ("super_admin", "project_lead", "member"):
        raise GovernanceServiceError("Invalid workspace role selected.")

    target = User.query.filter_by(
        id=target_user_id, organization_id=actor.organization_id
    ).first()
    if target is None:
        raise GovernanceServiceError("User not found in your workspace.")

    if target.id == actor.id and new_role != "super_admin":
        raise GovernanceServiceError("You cannot demote your own Super Admin account.")

    old_role = target.role
    if old_role == new_role:
        return target

    # Prevent demoting a Project Lead to Member while they still own active projects
    if old_role in ("super_admin", "project_lead") and new_role == "member":
        active_led_projects = Project.query.filter_by(
            organization_id=actor.organization_id,
            lead_id=target.id,
            status="active",
        ).all()
        if active_led_projects:
            titles = ", ".join(p.title for p in active_led_projects)
            raise GovernanceServiceError(
                f"Cannot demote {target.full_name} to Member while they lead active project(s): {titles}. Transfer leadership first."
            )

    clean_reason = (reason or "").strip() or f"Role updated from {old_role} to {new_role}."
    target.role = new_role

    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            project_id=None,
            target_type="user",
            target_id=target.id,
            action="member_role_updated",
            reason=clean_reason,
            diff_payload={
                "before": {"role": old_role},
                "after": {"role": new_role},
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
    actor_id: int | None = None,
    limit: int = 100,
) -> list[AuditLog]:
    """Tenant-scoped audit trail query for Super Admin governance inspection."""
    q = AuditLog.query.filter_by(organization_id=organization_id)
    if action:
        q = q.filter(AuditLog.action == action.strip())
    if project_id:
        q = q.filter(AuditLog.project_id == project_id)
    if actor_id:
        q = q.filter(AuditLog.actor_id == actor_id)
    return q.order_by(AuditLog.created_at.desc()).limit(limit).all()