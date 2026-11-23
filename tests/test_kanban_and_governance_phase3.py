from datetime import date, timedelta
import pytest
from werkzeug.security import generate_password_hash

from app.models import AuditLog, Organization, Project, Task, User
from app.services.governance_service import (
    GovernanceServiceError,
    soft_remove_member,
    transfer_project_lead,
)
from app.services.task_service import (
    TaskServiceError,
    create_project,
    create_task,
    extend_task_deadline,
    move_task_status,
    respond_to_assignment,
    save_project_as_template,
    toggle_task_blocker,
)


def seed_workspace(db_session, slug="phase3-org"):
    org = Organization(name="Phase 3 Studio", slug=slug, is_active=True)
    db_session.add(org)
    db_session.flush()

    pw = generate_password_hash("StrongPass123!")
    admin = User(
        organization_id=org.id,
        email=f"admin@{slug}.dev",
        password_hash=pw,
        full_name="Nyx Admin",
        role="super_admin",
        is_active=True,
    )
    lead1 = User(
        organization_id=org.id,
        email=f"lead1@{slug}.dev",
        password_hash=pw,
        full_name="Camille Lead",
        role="project_lead",
        is_active=True,
    )
    lead2 = User(
        organization_id=org.id,
        email=f"lead2@{slug}.dev",
        password_hash=pw,
        full_name="Jordan Backup Lead",
        role="project_lead",
        is_active=True,
    )
    member = User(
        organization_id=org.id,
        email=f"member@{slug}.dev",
        password_hash=pw,
        full_name="Maya Member",
        role="member",
        is_active=True,
    )
    db_session.add_all([admin, lead1, lead2, member])
    db_session.commit()
    return org, admin, lead1, lead2, member


def login_as(client, email: str):
    return client.post(
        "/login",
        data={"email": email, "password": "StrongPass123!"},
        follow_redirects=False,
    )


def test_cascading_date_caps_and_two_tier_limit(db_session):
    _, _, lead, _, member = seed_workspace(db_session, slug="datecap-org")
    project_target = date.today() + timedelta(days=14)
    project = create_project(
        actor=lead,
        title="Master Cap Project",
        description="Testing cascading date limits",
        est_completion_date=project_target,
    )

    # 1. Task due date exceeding project est_completion_date must fail
    with pytest.raises(TaskServiceError, match="cannot exceed project completion target"):
        create_task(
            project_id=project.id,
            actor=lead,
            title="Too Late Task",
            due_date=project_target + timedelta(days=1),
        )

    # 2. Valid parent task inside project target
    parent_due = date.today() + timedelta(days=7)
    parent = create_task(
        project_id=project.id,
        actor=lead,
        title="Parent Deliverable",
        due_date=parent_due,
        assignee_id=member.id,
    )

    # 3. Subtask due date exceeding parent.due_date (even if <= project target) must fail
    with pytest.raises(TaskServiceError, match="cannot exceed parent task due date"):
        create_task(
            project_id=project.id,
            actor=member,
            title="Late Subtask",
            due_date=parent_due + timedelta(days=2),
            parent_task_id=parent.id,
        )

    # 4. Valid subtask inside parent due date succeeds
    subtask = create_task(
        project_id=project.id,
        actor=member,
        title="Valid Subtask",
        due_date=parent_due - timedelta(days=1),
        parent_task_id=parent.id,
    )
    assert subtask.is_subtask is True

    # 5. Attempting a 3rd-tier grandchild subtask must fail
    with pytest.raises(TaskServiceError, match="Maximum 2-tier hierarchy enforced"):
        create_task(
            project_id=project.id,
            actor=lead,
            title="Grandchild Subtask",
            due_date=parent_due - timedelta(days=2),
            parent_task_id=subtask.id,
        )


def test_strict_vs_direct_acceptance_and_review_gate(db_session):
    org, _, lead, _, member = seed_workspace(db_session, slug="handshake-org")
    project = create_project(
        actor=lead,
        title="Handshake Workflow",
        description=None,
        est_completion_date=date.today() + timedelta(days=10),
    )

    # Strict mode (default): task starts as 'pending' and member cannot move it to in_progress
    task_strict = create_task(
        project_id=project.id,
        actor=lead,
        title="Strict Task",
        due_date=date.today() + timedelta(days=5),
        assignee_id=member.id,
    )
    assert task_strict.assignment_status == "pending"

    with pytest.raises(TaskServiceError, match="assigned and confirmed"):
        move_task_status(task_id=task_strict.id, actor=member, new_status="in_progress")

    # Member accepts task -> moves to in_progress -> hits Review Gate when trying to skip to Done
    respond_to_assignment(task_id=task_strict.id, actor=member, decision="confirm")
    assert task_strict.assignment_status == "confirmed"

    move_task_status(task_id=task_strict.id, actor=member, new_status="in_progress")
    with pytest.raises(TaskServiceError, match="Review Gate is active"):
        move_task_status(task_id=task_strict.id, actor=member, new_status="done")

    # Member moves to Review; Lead approves to Done
    move_task_status(task_id=task_strict.id, actor=member, new_status="review")
    move_task_status(task_id=task_strict.id, actor=lead, new_status="done")
    assert task_strict.board_status == "done"
    assert task_strict.completed_at is not None

    # Switch org to Direct mode: new task auto-confirms immediately
    org.settings.acceptance_mode = "direct"
    db_session.commit()

    task_direct = create_task(
        project_id=project.id,
        actor=lead,
        title="Direct Task",
        due_date=date.today() + timedelta(days=6),
        assignee_id=member.id,
    )
    assert task_direct.assignment_status == "confirmed"


def test_blocker_guard_and_client_blocked_vs_internal_extensions(db_session):
    _, _, lead, _, member = seed_workspace(db_session, slug="blocker-ext-org")
    project = create_project(
        actor=lead,
        title="Client Deliverable",
        description=None,
        est_completion_date=date.today() + timedelta(days=20),
    )
    task = create_task(
        project_id=project.id,
        actor=lead,
        title="Custom Ring Engraving",
        due_date=date.today() + timedelta(days=5),
        assignee_id=member.id,
    )
    respond_to_assignment(task_id=task.id, actor=member, decision="confirm")
    move_task_status(task_id=task.id, actor=member, new_status="in_progress")

    # Set Client Blocker -> blocks advancing to Review
    toggle_task_blocker(
        task_id=task.id,
        actor=member,
        is_blocked=True,
        blocker_type="client",
        blocker_reason="Client hasn't confirmed font size.",
    )
    with pytest.raises(TaskServiceError, match="Resolve the active blocker"):
        move_task_status(task_id=task.id, actor=member, new_status="review")

    # Approve Client-Blocked extension -> preserves has_internal_extension = False
    extend_task_deadline(
        task_id=task.id,
        actor=lead,
        new_due_date=date.today() + timedelta(days=9),
        extension_type="client_blocked",
        reason="Verified client delay on font approval.",
    )
    assert task.has_internal_extension is False

    # Approve Internal extension -> sets has_internal_extension = True
    extend_task_deadline(
        task_id=task.id,
        actor=lead,
        new_due_date=date.today() + timedelta(days=12),
        extension_type="internal",
        reason="Developer needed extra QA time.",
    )
    assert task.has_internal_extension is True


def test_template_save_and_instantiate_clamps_to_shorter_project(db_session):
    _, _, lead, _, _ = seed_workspace(db_session, slug="template-org")
    long_project = create_project(
        actor=lead,
        title="Long Reference Project",
        description=None,
        est_completion_date=date.today() + timedelta(days=30),
    )
    create_task(
        project_id=long_project.id,
        actor=lead,
        title="20-Day Milestone",
        due_date=date.today() + timedelta(days=20),
        priority="high",
    )

    tpl = save_project_as_template(
        project_id=long_project.id,
        actor=lead,
        name="Standard Launch Template",
    )

    # Instantiate onto a shorter 7-day project -> 20-day task must clamp to 7 days
    short_target = date.today() + timedelta(days=7)
    short_project = create_project(
        actor=lead,
        title="Rush 7-Day Project",
        description=None,
        est_completion_date=short_target,
        template_id=tpl.id,
    )
    assert len(short_project.tasks) == 1
    assert short_project.tasks[0].due_date == short_target


def test_ajax_board_move_optimistic_concurrency_409(client, db_session):
    _, _, lead, _, member = seed_workspace(db_session, slug="concurrency-org")
    project = create_project(
        actor=lead,
        title="Realtime Board",
        description=None,
        est_completion_date=date.today() + timedelta(days=10),
    )
    task = create_task(
        project_id=project.id,
        actor=lead,
        title="API Integration",
        due_date=date.today() + timedelta(days=4),
        assignee_id=member.id,
    )
    respond_to_assignment(task_id=task.id, actor=member, decision="confirm")

    login_as(client, member.email)

    # First move succeeds (todo -> in_progress)
    res1 = client.post(
        f"/tasks/{task.id}/move",
        json={"board_status": "in_progress", "expected_status": "todo"},
    )
    assert res1.status_code == 200
    assert res1.get_json()["board_status"] == "in_progress"

    # Second tab sending stale expected_status="todo" gets HTTP 409 Conflict
    res_stale = client.post(
        f"/tasks/{task.id}/move",
        json={"board_status": "review", "expected_status": "todo"},
    )
    assert res_stale.status_code == 409
    assert res_stale.get_json()["conflict"] is True


def test_emergency_lead_handover_and_soft_removal_governance(db_session):
    org, admin, lead1, lead2, member = seed_workspace(db_session, slug="gov-org")
    project = create_project(
        actor=lead1,
        title="Critical Storefront",
        description=None,
        est_completion_date=date.today() + timedelta(days=15),
    )
    task = create_task(
        project_id=project.id,
        actor=lead1,
        title="Design Checkout",
        due_date=date.today() + timedelta(days=5),
        assignee_id=member.id,
    )

    # Cannot deactivate lead1 while still leading an active project
    with pytest.raises(GovernanceServiceError, match="Transfer project ownership before deactivating"):
        soft_remove_member(target_user_id=lead1.id, actor=admin, reason="Leaving company")

    # Super Admin executes Emergency Lead Handover to lead2
    transfer_project_lead(
        project_id=project.id,
        new_lead_id=lead2.id,
        actor=admin,
        reason="Lead 1 transitioning to another client.",
    )
    assert project.lead_id == lead2.id

    #lead1 can be deactivated cleanly, and member soft-removal unassigns open task
    soft_remove_member(target_user_id=lead1.id, actor=admin, reason="Offboarding completed")
    assert lead1.is_active is False

    soft_remove_member(target_user_id=member.id, actor=admin, reason="Contractor contract ended")
    db_session.refresh(task)
    assert member.is_active is False
    assert task.assignee_id is None
    assert task.assignment_status == "unassigned"

    handover_audit = AuditLog.query.filter_by(
        organization_id=org.id, action="lead_transferred"
    ).first()
    assert handover_audit is not None
    assert handover_audit.diff_payload["after"]["lead_id"] == lead2.id


def test_board_html_renders_comments_and_subtasks_cleanly(client, db_session):
    from app.services.task_service import add_task_comment

    _, _, lead, _, member = seed_workspace(db_session, slug="render-org")
    project = create_project(
        actor=lead,
        title="Render Check Project",
        description="Ensures Jinja relationships render cleanly",
        est_completion_date=date.today() + timedelta(days=10),
    )
    parent = create_task(
        project_id=project.id,
        actor=lead,
        title="Parent Card",
        due_date=date.today() + timedelta(days=6),
        assignee_id=member.id,
    )
    create_task(
        project_id=project.id,
        actor=member,
        title="Child Subtask",
        due_date=date.today() + timedelta(days=4),
        parent_task_id=parent.id,
    )
    add_task_comment(task_id=parent.id, actor=lead, body="Checking comment author rendering.")

    login_as(client, lead.email)
    res = client.get(f"/projects/{project.id}/board")
    assert res.status_code == 200
    assert b"Render Check Project" in res.data
    assert b"Parent Card" in res.data
    assert b"Child Subtask" in res.data
    assert b"Checking comment author rendering." in res.data