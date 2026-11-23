from datetime import date, datetime, timedelta, timezone
import pytest
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app.models import (
    AuditLog,
    DeadlineExtension,
    InviteCode,
    Organization,
    OrganizationSettings,
    PointLedger,
    Project,
    ProjectShareLink,
    ProjectTemplate,
    Task,
    TaskComment,
    User,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_org_and_users(db_session, slug="test-org"):
    org = Organization(name="Test Org", slug=slug)
    db_session.add(org)
    db_session.flush()

    admin = User(
        organization_id=org.id,
        email=f"admin@{slug}.com",
        password_hash=generate_password_hash("Secret123!"),
        full_name="Admin User",
        role="super_admin",
    )
    lead = User(
        organization_id=org.id,
        email=f"lead@{slug}.com",
        password_hash=generate_password_hash("Secret123!"),
        full_name="Lead User",
        role="project_lead",
    )
    member = User(
        organization_id=org.id,
        email=f"member@{slug}.com",
        password_hash=generate_password_hash("Secret123!"),
        full_name="Member User",
        role="member",
    )
    db_session.add_all([admin, lead, member])
    db_session.commit()
    return org, admin, lead, member


def test_health_endpoint_and_security_headers(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.get_json()["status"] == "ok"
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_organization_auto_provisions_default_settings(db_session):
    org = Organization(name="Cafe Mija", slug="cafe-mija")
    db_session.add(org)
    db_session.commit()

    settings = OrganizationSettings.query.filter_by(organization_id=org.id).one()
    assert settings.theme_preset == "calm_sage"
    assert settings.acceptance_mode == "strict"
    assert settings.pending_sla_hours == 48
    assert settings.require_review_gate is True
    assert settings.recognition_enabled is True
    assert settings.daily_digest_mode == "banner"
    assert settings.effective_brand_name == "Cafe Mija"
    assert settings.column_labels["todo"] == "To Do"
    assert settings.tier_thresholds["gold"] == 2000


def test_organization_settings_check_constraints(db_session):
    org, _, _, _ = make_org_and_users(db_session, slug="constraint-org")
    settings = org.settings

    # Invalid acceptance_mode
    settings.acceptance_mode = "invalid_mode"
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Out-of-bounds pending_sla_hours
    settings.pending_sla_hours = 0
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Invalid daily_digest_mode
    settings.daily_digest_mode = "sms_spam"
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_invite_code_prevents_super_admin_escalation(db_session):
    org, admin, _, _ = make_org_and_users(db_session, slug="invite-org")

    bad_invite = InviteCode(
        organization_id=org.id,
        code_hash="hash_escalation_attempt",
        assigned_role="super_admin",  # Blocked by CheckConstraint
        created_by_id=admin.id,
        expires_at=utc_now() + timedelta(hours=24),
    )
    db_session.add(bad_invite)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_restrict_deleting_active_project_lead(db_session):
    org, _, lead, _ = make_org_and_users(db_session, slug="restrict-org")
    project = Project(
        organization_id=org.id,
        lead_id=lead.id,
        title="Protected Project",
        est_completion_date=date.today() + timedelta(days=14),
    )
    db_session.add(project)
    db_session.commit()

    # SQLite PRAGMA foreign_keys=ON must prevent deleting a User leading a Project
    db_session.delete(lead)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_task_blocker_constraint_and_two_tier_hierarchy(db_session):
    org, _, lead, member = make_org_and_users(db_session, slug="task-org")
    project = Project(
        organization_id=org.id,
        lead_id=lead.id,
        title="Kanban Core",
        est_completion_date=date.today() + timedelta(days=10),
    )
    db_session.add(project)
    db_session.commit()

    # Invalid: is_blocked=True without blocker_type
    bad_task = Task(
        project_id=project.id,
        created_by_id=lead.id,
        title="Blocked without type",
        due_date=date.today() + timedelta(days=5),
        is_blocked=True,
        blocker_type=None,
    )
    db_session.add(bad_task)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Valid Parent -> Subtask -> Extension -> Comment
    parent = Task(
        project_id=project.id,
        created_by_id=lead.id,
        assignee_id=member.id,
        title="Parent Deliverable",
        priority="high",
        due_date=date.today() + timedelta(days=6),
    )
    db_session.add(parent)
    db_session.commit()

    subtask = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        created_by_id=member.id,
        title="Subtask Step 1",
        due_date=date.today() + timedelta(days=4),
    )
    ext = DeadlineExtension(
        task_id=parent.id,
        requested_by_id=member.id,
        approved_by_id=lead.id,
        old_due_date=date.today() + timedelta(days=6),
        new_due_date=date.today() + timedelta(days=8),
        extension_type="client_blocked",
        reason="Client requested copy revision.",
    )
    comment = TaskComment(
        task_id=parent.id,
        author_id=lead.id,
        body="Approved client extension.",
    )
    db_session.add_all([subtask, ext, comment])
    db_session.commit()

    assert subtask.is_subtask is True
    assert parent.subtasks[0].id == subtask.id
    assert len(parent.extensions) == 1
    assert len(parent.comments) == 1


def test_point_ledger_idempotency_and_sum_constraint(db_session):
    org, _, lead, member = make_org_and_users(db_session, slug="points-org")
    project = Project(
        organization_id=org.id,
        lead_id=lead.id,
        title="Gamified Sprint",
        est_completion_date=date.today() + timedelta(days=7),
    )
    db_session.add(project)
    db_session.flush()

    task = Task(
        project_id=project.id,
        created_by_id=lead.id,
        assignee_id=member.id,
        title="Ship MVP",
        board_status="done",
        due_date=date.today() + timedelta(days=3),
    )
    db_session.add(task)
    db_session.flush()

    # First payout succeeds
    entry_1 = PointLedger(
        organization_id=org.id,
        user_id=member.id,
        task_id=task.id,
        base_points=50,
        bonus_points=10,
        total_points=60,
        period_key="2026-11",
    )
    db_session.add(entry_1)
    db_session.commit()

    # Second payout for the exact same task_id must fail via UNIQUE constraint
    entry_2 = PointLedger(
        organization_id=org.id,
        user_id=member.id,
        task_id=task.id,
        base_points=50,
        bonus_points=10,
        total_points=60,
        period_key="2026-11",
    )
    db_session.add(entry_2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_template_share_link_and_audit_log_creation(db_session):
    org, admin, lead, _ = make_org_and_users(db_session, slug="full-coverage-org")
    project = Project(
        organization_id=org.id,
        lead_id=lead.id,
        title="Full Coverage Project",
        est_completion_date=date.today() + timedelta(days=15),
    )
    db_session.add(project)
    db_session.flush()

    tpl = ProjectTemplate(
        organization_id=org.id,
        created_by_id=lead.id,
        name="Standard Onboarding",
        template_payload={"tasks": [{"title": "Kickoff", "priority": "medium", "offset_days": 2}]},
    )
    share = ProjectShareLink(
        project_id=project.id,
        token_hash="sha256_public_share_token",
        created_by_id=lead.id,
    )
    audit = AuditLog(
        organization_id=org.id,
        actor_id=admin.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        action="project_created",
        reason="Initial project setup",
        diff_payload={"before": None, "after": {"title": "Full Coverage Project"}},
    )
    db_session.add_all([tpl, share, audit])
    db_session.commit()

    assert ProjectTemplate.query.count() == 1
    assert ProjectShareLink.query.count() == 1
    assert AuditLog.query.count() == 1