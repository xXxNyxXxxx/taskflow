from datetime import date, datetime, timedelta, timezone
from app.extensions import db
from app.models import (
    AuditLog,
    DeadlineExtension,
    OrganizationSettings,
    Project,
    ProjectTemplate,
    Task,
    TaskComment,
    User,
)


class TaskServiceError(Exception):
    """Domain validation error surfaced cleanly to routes/JSON endpoints."""


class ConcurrencyConflictError(TaskServiceError):
    """Raised on stale client mutations (HTTP 409)."""


VALID_BOARD_STATUSES = ("todo", "in_progress", "review", "done")
VALID_PRIORITIES = ("low", "medium", "high", "critical")
VALID_BLOCKER_TYPES = ("client", "internal")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_tenant_project(project_id: int, organization_id: int) -> Project:
    project = Project.query.filter_by(
        id=project_id, organization_id=organization_id
    ).first()
    if project is None:
        raise TaskServiceError("Project not found in your workspace.")
    return project


def get_tenant_task(task_id: int, organization_id: int) -> Task:
    task = (
        Task.query.join(Project, Task.project_id == Project.id)
        .filter(Task.id == task_id, Project.organization_id == organization_id)
        .first()
    )
    if task is None:
        raise TaskServiceError("Task not found in your workspace.")
    return task


def _assert_project_mutable(project: Project) -> None:
    if project.status == "archived":
        raise TaskServiceError("This project is archived and read-only.")


def _assert_task_unlocked(task: Task, actor: User) -> None:
    if task.board_status == "done" and actor.role != "super_admin":
        raise TaskServiceError(
            "Completed tasks are locked. Only a Super Admin can reopen or modify a Done task."
        )


def _resolve_initial_assignment(
    *,
    assignee_id: int | None,
    organization_id: int,
    settings: OrganizationSettings,
) -> tuple[int | None, str, datetime | None]:
    if not assignee_id:
        return None, "unassigned", None

    assignee = User.query.filter_by(
        id=assignee_id, organization_id=organization_id, is_active=True
    ).first()
    if assignee is None:
        raise TaskServiceError("Selected assignee must be an active member of your workspace.")

    status = "confirmed" if settings.acceptance_mode == "direct" else "pending"
    return assignee.id, status, utc_now()


# ============================================================================
# 1. Projects & Reusable Project Task Templates
# ============================================================================

def create_project(
    *,
    actor: User,
    title: str,
    description: str | None,
    est_completion_date: date,
    lead_id: int | None = None,
    template_id: int | None = None,
) -> Project:
    if actor.role not in ("super_admin", "project_lead"):
        raise TaskServiceError("Only Project Leads or Super Admins can create projects.")

    clean_title = (title or "").strip()
    if len(clean_title) < 2:
        raise TaskServiceError("Project title must be at least 2 characters.")

    if est_completion_date < date.today():
        raise TaskServiceError("Estimated completion date cannot be in the past.")

    target_lead_id = actor.id
    if lead_id and actor.role == "super_admin":
        lead_user = User.query.filter_by(
            id=lead_id, organization_id=actor.organization_id, is_active=True
        ).first()
        if lead_user is None or lead_user.role not in ("super_admin", "project_lead"):
            raise TaskServiceError("Assigned project lead must be an active Lead or Super Admin.")
        target_lead_id = lead_user.id

    project = Project(
        organization_id=actor.organization_id,
        lead_id=target_lead_id,
        title=clean_title,
        description=(description or "").strip() or None,
        status="active",
        est_completion_date=est_completion_date,
    )
    db.session.add(project)
    db.session.flush()

    if template_id:
        instantiate_project_template(project=project, template_id=template_id, actor=actor, commit=False)

    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            project_id=project.id,
            target_type="project",
            target_id=project.id,
            action="project_created",
            reason=f"Created project '{project.title}'.",
        )
    )
    db.session.commit()
    return project


def save_project_as_template(
    *,
    project_id: int,
    actor: User,
    name: str,
    description: str | None = None,
) -> ProjectTemplate:
    project = get_tenant_project(project_id, actor.organization_id)
    if actor.role != "super_admin" and project.lead_id != actor.id:
        raise TaskServiceError("Only the Project Lead or Super Admin can save a project template.")

    clean_name = (name or "").strip()
    if len(clean_name) < 2:
        raise TaskServiceError("Template name must be at least 2 characters.")

    anchor_date = project.created_at.date() if project.created_at else date.today()
    parent_tasks = [t for t in project.tasks if t.parent_task_id is None]

    serialized_tasks = []
    for parent in parent_tasks:
        parent_offset = max(0, (parent.due_date - anchor_date).days)
        subtasks_payload = []
        for sub in parent.subtasks:
            sub_offset = max(0, (sub.due_date - anchor_date).days)
            subtasks_payload.append(
                {
                    "title": sub.title,
                    "description": sub.description,
                    "priority": sub.priority,
                    "offset_days": min(sub_offset, parent_offset),
                }
            )
        serialized_tasks.append(
            {
                "title": parent.title,
                "description": parent.description,
                "priority": parent.priority,
                "offset_days": parent_offset,
                "subtasks": subtasks_payload,
            }
        )

    template = ProjectTemplate(
        organization_id=actor.organization_id,
        created_by_id=actor.id,
        name=clean_name,
        description=(description or "").strip() or None,
        template_payload={"tasks": serialized_tasks},
    )
    db.session.add(template)
    db.session.commit()
    return template


def instantiate_project_template(
    *,
    project: Project,
    template_id: int,
    actor: User,
    commit: bool = True,
) -> list[Task]:
    """Clones template tasks onto a project while clamping all due dates <= est_completion_date."""
    _assert_project_mutable(project)

    template = ProjectTemplate.query.filter_by(
        id=template_id, organization_id=project.organization_id
    ).first()
    if template is None:
        raise TaskServiceError("Selected project template was not found in your workspace.")

    base_date = date.today()
    created_tasks: list[Task] = []

    for item in (template.template_payload or {}).get("tasks", []):
        raw_due = base_date + timedelta(days=int(item.get("offset_days", 1)))
        clamped_parent_due = min(raw_due, project.est_completion_date)
        priority = item.get("priority", "medium")
        if priority not in VALID_PRIORITIES:
            priority = "medium"

        parent = Task(
            project_id=project.id,
            created_by_id=actor.id,
            title=str(item.get("title", "Untitled Task"))[:180],
            description=item.get("description"),
            priority=priority,
            board_status="todo",
            assignment_status="unassigned",
            due_date=clamped_parent_due,
        )
        db.session.add(parent)
        db.session.flush()
        created_tasks.append(parent)

        for sub_item in item.get("subtasks", []):
            raw_sub_due = base_date + timedelta(days=int(sub_item.get("offset_days", 1)))
            clamped_sub_due = min(raw_sub_due, clamped_parent_due)
            sub_priority = sub_item.get("priority", "medium")
            if sub_priority not in VALID_PRIORITIES:
                sub_priority = "medium"

            sub = Task(
                project_id=project.id,
                parent_task_id=parent.id,
                created_by_id=actor.id,
                title=str(sub_item.get("title", "Untitled Subtask"))[:180],
                description=sub_item.get("description"),
                priority=sub_priority,
                board_status="todo",
                assignment_status="unassigned",
                due_date=clamped_sub_due,
            )
            db.session.add(sub)
            created_tasks.append(sub)

    if commit:
        db.session.commit()
    return created_tasks


# ============================================================================
# 2. 2-Tier Task Creation, Strict Self-Ownership & Cascading Date Caps
# ============================================================================

def create_task(
    *,
    project_id: int,
    actor: User,
    title: str,
    due_date: date,
    priority: str = "medium",
    description: str | None = None,
    assignee_id: int | None = None,
    parent_task_id: int | None = None,
) -> Task:
    project = get_tenant_project(project_id, actor.organization_id)
    _assert_project_mutable(project)
    settings = project.organization.settings

    clean_title = (title or "").strip()
    if len(clean_title) < 2:
        raise TaskServiceError("Task title must be at least 2 characters.")

    if priority not in VALID_PRIORITIES:
        raise TaskServiceError("Invalid task priority.")

    if due_date > project.est_completion_date:
        raise TaskServiceError(
            f"Task due date ({due_date}) cannot exceed project completion target ({project.est_completion_date})."
        )

    parent_task: Task | None = None
    if parent_task_id is not None:
        parent_task = Task.query.filter_by(
            id=parent_task_id, project_id=project.id
        ).first()
        if parent_task is None:
            raise TaskServiceError("Parent task not found in this project.")
        _assert_task_unlocked(parent_task, actor)
        if parent_task.parent_task_id is not None:
            raise TaskServiceError("Maximum 2-tier hierarchy enforced: subtasks cannot have child subtasks.")
        if due_date > parent_task.due_date:
            raise TaskServiceError(
                f"Subtask due date ({due_date}) cannot exceed parent task due date ({parent_task.due_date})."
            )
        # Only Super Admin, Project Lead, or Parent Task Assignee can create subtasks
        if actor.role == "member" and parent_task.assignee_id != actor.id:
            raise TaskServiceError("Members can only add subtasks under parent tasks assigned to them.")

        # Strict Self-Ownership: Members can only self-assign or leave unassigned
        if actor.role == "member":
            if assignee_id not in (None, actor.id):
                raise TaskServiceError(
                    "Members cannot assign subtasks to other teammates. Flag the subtask for help to escalate to your Project Lead."
                )
    else:
        if actor.role not in ("super_admin", "project_lead"):
            raise TaskServiceError("Only Project Leads or Super Admins can create top-level tasks.")

    resolved_assignee_id, assignment_status, assigned_at = _resolve_initial_assignment(
        assignee_id=assignee_id,
        organization_id=actor.organization_id,
        settings=settings,
    )

    # Auto-confirm when a Member self-assigns their own subtask
    if (
        parent_task is not None
        and actor.role == "member"
        and resolved_assignee_id == actor.id
    ):
        assignment_status = "confirmed"

    task = Task(
        project_id=project.id,
        parent_task_id=parent_task.id if parent_task else None,
        created_by_id=actor.id,
        assignee_id=resolved_assignee_id,
        title=clean_title,
        description=(description or "").strip() or None,
        priority=priority,
        board_status="todo",
        assignment_status=assignment_status,
        assigned_at=assigned_at,
        due_date=due_date,
    )
    db.session.add(task)
    db.session.commit()
    return task


# ============================================================================
# 3. Acceptance Handshake, Pending SLA & Kanban Board Transitions
# ============================================================================

def is_pending_sla_expired(task: Task, pending_sla_hours: int) -> bool:
    if task.assignment_status != "pending" or task.assigned_at is None:
        return False
    deadline = normalize_utc(task.assigned_at) + timedelta(hours=pending_sla_hours)
    return utc_now() > deadline


def respond_to_assignment(
    *,
    task_id: int,
    actor: User,
    decision: str,
    decline_reason: str | None = None,
) -> Task:
    task = get_tenant_task(task_id, actor.organization_id)
    _assert_project_mutable(task.project)
    _assert_task_unlocked(task, actor)

    if task.assignee_id != actor.id:
        raise TaskServiceError("Only the assigned user can accept or decline this task.")

    if task.assignment_status != "pending":
        raise TaskServiceError("This task is not awaiting acceptance.")

    if decision == "confirm":
        task.assignment_status = "confirmed"
    elif decision == "decline":
        clean_reason = (decline_reason or "").strip()
        if len(clean_reason) < 3:
            raise TaskServiceError("Please provide a brief reason when declining a task.")
        task.assignment_status = "declined"
        db.session.add(
            TaskComment(
                task_id=task.id,
                author_id=actor.id,
                body=f"[Assignment Declined]: {clean_reason}",
            )
        )
    else:
        raise TaskServiceError("Invalid acceptance decision.")

    db.session.commit()
    return task


def request_task_reclaim(
    *,
    task_id: int,
    actor: User,
    reason: str,
) -> Task:
    """Returns a declined task to the Unassigned queue and logs a reclaim request for Lead review."""
    task = get_tenant_task(task_id, actor.organization_id)
    _assert_project_mutable(task.project)
    _assert_task_unlocked(task, actor)

    if task.assignee_id != actor.id:
        raise TaskServiceError("Only the previously appointed assignee can request to reclaim this task.")

    if task.assignment_status != "declined":
        raise TaskServiceError("Only declined tasks can be requested for reclaim.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 3:
        raise TaskServiceError("Please provide a brief note for the Project Lead.")

    task.assignee_id = None
    task.assignment_status = "unassigned"
    task.assigned_at = None

    db.session.add(
        TaskComment(
            task_id=task.id,
            author_id=actor.id,
            body=f"[Reclaim Requested by {actor.full_name} — Awaiting Lead Reassignment]: {clean_reason}",
        )
    )
    db.session.commit()
    return task


def reassign_task(
    *,
    task_id: int,
    actor: User,
    new_assignee_id: int | None,
    resolve_blocker: bool = True,
) -> Task:
    task = get_tenant_task(task_id, actor.organization_id)
    project = task.project
    _assert_project_mutable(project)
    _assert_task_unlocked(task, actor)
    settings = project.organization.settings

    # Strict RBAC: Only Super Admin or Project Lead can assign/reassign tasks or subtasks
    can_reassign = actor.role == "super_admin" or project.lead_id == actor.id
    if not can_reassign:
        raise TaskServiceError("Only the Project Lead or Super Admin can assign or reassign tasks.")

    old_assignee_id = task.assignee_id
    was_blocked_subtask = task.parent_task_id is not None and task.is_blocked

    resolved_id, status, assigned_at = _resolve_initial_assignment(
        assignee_id=new_assignee_id,
        organization_id=actor.organization_id,
        settings=settings,
    )
    task.assignee_id = resolved_id
    task.assignment_status = status
    task.assigned_at = assigned_at

    if was_blocked_subtask and resolved_id is not None:
        if resolve_blocker:
            task.is_blocked = False
            task.blocker_type = None
            task.blocker_reason = None

        db.session.add(
            AuditLog(
                organization_id=actor.organization_id,
                actor_id=actor.id,
                project_id=project.id,
                target_type="task",
                target_id=task.id,
                action="subtask_help_assigned",
                reason=f"Assigned helper (User #{resolved_id}) to escalated subtask '{task.title}'.",
                diff_payload={
                    "before": {"assignee_id": old_assignee_id, "is_blocked": True},
                    "after": {"assignee_id": resolved_id, "is_blocked": task.is_blocked},
                },
            )
        )

    db.session.commit()
    return task


def move_task_status(
    *,
    task_id: int,
    actor: User,
    new_status: str,
    expected_current_status: str | None = None,
) -> Task:
    if new_status not in VALID_BOARD_STATUSES:
        raise TaskServiceError("Invalid Kanban board column.")

    task = get_tenant_task(task_id, actor.organization_id)
    project = task.project
    _assert_project_mutable(project)
    _assert_task_unlocked(task, actor)
    settings = project.organization.settings

    if expected_current_status and task.board_status != expected_current_status:
        raise ConcurrencyConflictError(
            "This card was updated by another teammate. Refreshing latest board state."
        )

    is_lead_or_admin = actor.role == "super_admin" or project.lead_id == actor.id
    is_assignee = task.assignee_id == actor.id
    is_creator = task.created_by_id == actor.id
    is_unassigned_subtask_owner = (
        task.parent_task is not None
        and task.assignee_id is None
        and task.parent_task.assignee_id == actor.id
    )

    if not (is_lead_or_admin or is_assignee or is_unassigned_subtask_owner):
        raise TaskServiceError(
            "Only the assigned teammate, Project Lead, or Super Admin can update this subtask's status."
        )

    # Strict acceptance guard: unconfirmed assignees cannot advance a card
    if (
        settings.acceptance_mode == "strict"
        and task.assignment_status in ("pending", "declined", "unassigned")
        and new_status != "todo"
    ):
        raise TaskServiceError(
            "Task must be assigned and confirmed before moving into active progress."
        )

    # Active blocker guard: blocked tasks cannot enter Review or Done
    if task.is_blocked and new_status in ("review", "done"):
        raise TaskServiceError(
            "Resolve the active blocker flag before moving this task to Review or Done."
        )

    # Parent completion guard: all child subtasks must be Done first
    if new_status == "done" and task.subtasks:
        incomplete = [s for s in task.subtasks if s.board_status != "done"]
        if incomplete:
            raise TaskServiceError(
                f"Complete all {len(incomplete)} remaining subtask(s) before marking the parent task Done."
            )

   # Review Gate guard: applies to top-level tasks for non-creator members
    if (
        new_status == "done"
        and settings.require_review_gate
        and task.parent_task_id is None
        and not (is_lead_or_admin or is_creator)
    ):
        raise TaskServiceError(
            "Workspace Review Gate is active: move this card to Review for Project Lead sign-off."
        )

    # Super Admin override audit when reopening a locked 'done' task
    if task.board_status == "done" and new_status != "done" and actor.role == "super_admin":
        db.session.add(
            AuditLog(
                organization_id=actor.organization_id,
                actor_id=actor.id,
                project_id=project.id,
                target_type="task",
                target_id=task.id,
                action="done_task_reopened",
                reason=f"Super Admin reopened completed task from Done to {new_status}.",
                diff_payload={
                    "before": {"board_status": "done"},
                    "after": {"board_status": new_status},
                },
            )
        )

    task.board_status = new_status
    if new_status == "done":
        if task.completed_at is None:
            task.completed_at = utc_now()
    else:
        task.completed_at = None

    db.session.commit()
    return task


# ============================================================================
# 4. Blocker Flags, Subtask Help Escalation & Deadline Extensions
# ============================================================================

def toggle_task_blocker(
    *,
    task_id: int,
    actor: User,
    is_blocked: bool,
    blocker_type: str | None = None,
    blocker_reason: str | None = None,
) -> Task:
    task = get_tenant_task(task_id, actor.organization_id)
    project = task.project
    _assert_project_mutable(project)
    _assert_task_unlocked(task, actor)

    is_parent_owner = task.parent_task is not None and task.parent_task.assignee_id == actor.id
    if not (
        actor.role == "super_admin"
        or project.lead_id == actor.id
        or task.assignee_id == actor.id
        or is_parent_owner
    ):
        raise TaskServiceError("Not authorized to update blocker status on this task.")

    if is_blocked:
        if blocker_type not in VALID_BLOCKER_TYPES:
            raise TaskServiceError("Blocker type must be 'client' or 'internal'.")
        clean_reason = (blocker_reason or "").strip()
        if len(clean_reason) < 3:
            raise TaskServiceError("Please provide a brief blocker reason.")
        task.is_blocked = True
        task.blocker_type = blocker_type
        task.blocker_reason = clean_reason

        # Escalate subtask help requests to Project Lead & Workspace Admin via AuditLog
        if task.parent_task_id is not None:
            db.session.add(
                AuditLog(
                    organization_id=actor.organization_id,
                    actor_id=actor.id,
                    project_id=project.id,
                    target_type="task",
                    target_id=task.id,
                    action="subtask_help_requested",
                    reason=clean_reason,
                    diff_payload={
                        "subtask_title": task.title,
                        "parent_task_id": task.parent_task_id,
                        "blocker_type": blocker_type,
                    },
                )
            )
    else:
        task.is_blocked = False
        task.blocker_type = None
        task.blocker_reason = None

    db.session.commit()
    return task


def extend_task_deadline(
    *,
    task_id: int,
    actor: User,
    new_due_date: date,
    extension_type: str,
    reason: str,
) -> DeadlineExtension:
    task = get_tenant_task(task_id, actor.organization_id)
    project = task.project
    _assert_project_mutable(project)
    _assert_task_unlocked(task, actor)

    if actor.role != "super_admin" and project.lead_id != actor.id:
        raise TaskServiceError("Only the Project Lead or Super Admin can approve deadline extensions.")

    if extension_type not in ("internal", "client_blocked"):
        raise TaskServiceError("Extension type must be 'internal' or 'client_blocked'.")

    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise TaskServiceError("An audit reason (at least 5 characters) is required for deadline extensions.")

    if new_due_date <= task.due_date:
        raise TaskServiceError("New due date must be later than the current due date.")

    if new_due_date > project.est_completion_date:
        raise TaskServiceError(
            f"Extended due date ({new_due_date}) cannot exceed the project target ({project.est_completion_date})."
        )

    if task.parent_task is not None and new_due_date > task.parent_task.due_date:
        raise TaskServiceError(
            f"Subtask extended due date ({new_due_date}) cannot exceed parent due date ({task.parent_task.due_date})."
        )

    old_due = task.due_date
    task.due_date = new_due_date
    if extension_type == "internal":
        task.has_internal_extension = True

    ext = DeadlineExtension(
        task_id=task.id,
        requested_by_id=task.assignee_id or actor.id,
        approved_by_id=actor.id,
        old_due_date=old_due,
        new_due_date=new_due_date,
        extension_type=extension_type,
        reason=clean_reason,
    )
    db.session.add(ext)
    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            project_id=project.id,
            target_type="task",
            target_id=task.id,
            action="deadline_extended",
            reason=clean_reason,
            diff_payload={
                "before": {"due_date": old_due.isoformat()},
                "after": {
                    "due_date": new_due_date.isoformat(),
                    "extension_type": extension_type,
                },
            },
        )
    )
    db.session.commit()
    return ext


def add_task_comment(*, task_id: int, actor: User, body: str) -> TaskComment:
    task = get_tenant_task(task_id, actor.organization_id)
    _assert_project_mutable(task.project)

    clean_body = (body or "").strip()
    if not clean_body:
        raise TaskServiceError("Comment cannot be empty.")

    comment = TaskComment(
        task_id=task.id,
        author_id=actor.id,
        body=clean_body,
    )
    db.session.add(comment)
    db.session.commit()
    return comment