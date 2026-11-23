import hashlib
import secrets
from datetime import date, datetime, timedelta, timezone
import click
from flask import Flask
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import (
    AuditLog,
    DeadlineExtension,
    InviteCode,
    Organization,
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


def sha256_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def register_cli_commands(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command():
        """Create all database tables directly (for quick local dev)."""
        db.create_all()
        click.echo("Initialized all 11 TaskFlow database tables.")

    @app.cli.command("seed-dev")
    @click.option("--reset", is_flag=True, help="Drop and recreate all tables before seeding.")
    def seed_dev_command(reset: bool):
        """Seed deterministic multi-tenant edge-case fixtures for development & UAT."""
        if reset:
            db.drop_all()
            db.create_all()
            click.echo("Database dropped and recreated.")
        else:
            db.create_all()

        if Organization.query.first():
            click.echo("Database already contains data. Run `flask seed-dev --reset` to rebuild.")
            return

        now = utc_now()
        today = date.today()
        default_pw = generate_password_hash("TaskFlowDev123!")

        # ------------------------------------------------------------------
        # 1. Tenant A: Jewelry Store Studio (Primary White-Labeled Workspace)
        # ------------------------------------------------------------------
        org_a = Organization(name="Atelier Aurelia", slug="atelier-aurelia", is_active=True)
        db.session.add(org_a)
        db.session.flush()  # Triggers auto-creation of OrganizationSettings

        # Customize Tenant A White-Label Branding
        org_a.settings.brand_name = "Aurelia Studio"
        org_a.settings.theme_preset = "soft_terracotta"
        org_a.settings.pending_sla_hours = 24

        admin_a = User(
            organization_id=org_a.id,
            email="admin@aurelia.dev",
            password_hash=default_pw,
            full_name="Nyx Admin",
            role="super_admin",
            current_tier="gold",
            ics_token_hash=sha256_token("dev-ics-token-admin-a"),
        )
        lead_a = User(
            organization_id=org_a.id,
            email="lead@aurelia.dev",
            password_hash=default_pw,
            full_name="Camille Lead",
            role="project_lead",
            current_tier="indigo",
        )
        member_a1 = User(
            organization_id=org_a.id,
            email="joey@aurelia.dev",
            password_hash=default_pw,
            full_name="Joey Designer",
            role="member",
            current_tier="emerald",
        )
        member_a2 = User(
            organization_id=org_a.id,
            email="iris@aurelia.dev",
            password_hash=default_pw,
            full_name="Iris Developer",
            role="member",
            current_tier="slate",
        )
        deactivated_a = User(
            organization_id=org_a.id,
            email="former@aurelia.dev",
            password_hash=default_pw,
            full_name="Former Contractor",
            role="member",
            is_active=False,
            current_tier="slate",
        )
        db.session.add_all([admin_a, lead_a, member_a1, member_a2, deactivated_a])
        db.session.flush()

        # Active Invite Code for Tenant A (raw token printed to console for testing)
        raw_invite_a = "AURELIA-JOIN-2026"
        invite_a = InviteCode(
            organization_id=org_a.id,
            code_hash=sha256_token(raw_invite_a),
            assigned_role="member",
            created_by_id=admin_a.id,
            max_uses=5,
            expires_at=now + timedelta(days=7),
        )
        db.session.add(invite_a)

        # Reusable Project Task Template
        template_a = ProjectTemplate(
            organization_id=org_a.id,
            created_by_id=lead_a.id,
            name="E-Commerce Collection Launch",
            description="Standard 2-tier launch workflow with relative due-date offsets.",
            template_payload={
                "tasks": [
                    {
                        "title": "Product Photography & Retouching",
                        "priority": "high",
                        "offset_days": 5,
                        "subtasks": [
                            {"title": "Macro ring shots", "priority": "medium", "offset_days": 3},
                            {"title": "Color-grade & export WebP", "priority": "medium", "offset_days": 5},
                        ],
                    },
                    {
                        "title": "Checkout & Inventory QA",
                        "priority": "critical",
                        "offset_days": 10,
                        "subtasks": [],
                    },
                ]
            },
        )
        db.session.add(template_a)

        # Active Project in Tenant A
        project_a = Project(
            organization_id=org_a.id,
            lead_id=lead_a.id,
            title="Jewelry Store Storefront",
            description="Custom catalog, ring sizing guide, and checkout launch.",
            status="active",
            est_completion_date=today + timedelta(days=21),
        )
        db.session.add(project_a)
        db.session.flush()

        # Edge Case 1: Completed Task with Gold-Tier PointLedger entry
        task_done = Task(
            project_id=project_a.id,
            created_by_id=lead_a.id,
            assignee_id=admin_a.id,
            title="Configure Multi-Tenant Database Architecture",
            priority="critical",
            board_status="done",
            assignment_status="confirmed",
            assigned_at=now - timedelta(days=5),
            due_date=today + timedelta(days=2),
            points_awarded=True,
            completed_at=now - timedelta(days=1),
        )
        db.session.add(task_done)
        db.session.flush()

        ledger_entry = PointLedger(
            organization_id=org_a.id,
            user_id=admin_a.id,
            task_id=task_done.id,
            base_points=100,
            bonus_points=20,
            total_points=120,
            period_key=today.strftime("%Y-%m"),
        )
        db.session.add(ledger_entry)

        # Edge Case 2: Client-Blocked Parent Task + Verified Client-Blocked Extension + Subtask
        task_blocked = Task(
            project_id=project_a.id,
            created_by_id=lead_a.id,
            assignee_id=member_a1.id,
            title="Design Bespoke Ring Customizer UI",
            priority="high",
            board_status="in_progress",
            assignment_status="confirmed",
            assigned_at=now - timedelta(days=4),
            due_date=today + timedelta(days=10),
            is_blocked=True,
            blocker_type="client",
            blocker_reason="Waiting on client to supply final hallmark engraving SVGs.",
            has_internal_extension=False,
        )
        db.session.add(task_blocked)
        db.session.flush()

        subtask_1 = Task(
            project_id=project_a.id,
            parent_task_id=task_blocked.id,
            created_by_id=member_a1.id,
            assignee_id=member_a2.id,
            title="Build interactive metal-finish selector",
            priority="medium",
            board_status="in_progress",
            assignment_status="confirmed",
            assigned_at=now - timedelta(days=2),
            due_date=today + timedelta(days=7),
        )
        ext_client = DeadlineExtension(
            task_id=task_blocked.id,
            requested_by_id=member_a1.id,
            approved_by_id=lead_a.id,
            old_due_date=today + timedelta(days=5),
            new_due_date=today + timedelta(days=10),
            extension_type="client_blocked",
            reason="Client delayed hallmark asset delivery by 5 business days.",
        )
        comment_1 = TaskComment(
            task_id=task_blocked.id,
            author_id=lead_a.id,
            body="Verified client blocker—on-time bonus preserved upon delivery.",
        )
        db.session.add_all([subtask_1, ext_client, comment_1])

        # Edge Case 3: Expired Pending SLA Task (assigned 72h ago vs 24h SLA)
        task_stale_pending = Task(
            project_id=project_a.id,
            created_by_id=lead_a.id,
            assignee_id=member_a2.id,
            title="Integrate Payment Webhook Signature Check",
            priority="high",
            board_status="todo",
            assignment_status="pending",
            assigned_at=now - timedelta(hours=72),
            due_date=today + timedelta(days=12),
        )
        db.session.add(task_stale_pending)

        # Read-Only Client Share Link
        raw_share_token = "aurelia-client-preview-token"
        share_link = ProjectShareLink(
            project_id=project_a.id,
            token_hash=sha256_token(raw_share_token),
            created_by_id=lead_a.id,
            expires_at=now + timedelta(days=14),
            is_active=True,
        )
        db.session.add(share_link)

        # Initial Audit Log Entry
        audit_seed = AuditLog(
            organization_id=org_a.id,
            actor_id=admin_a.id,
            project_id=project_a.id,
            target_type="org_settings",
            target_id=org_a.settings.id,
            action="settings_updated",
            reason="Initial white-label workspace setup.",
            diff_payload={"before": {"brand_name": None}, "after": {"brand_name": "Aurelia Studio"}},
        )
        db.session.add(audit_seed)

        # ------------------------------------------------------------------
        # 2. Tenant B: Isolated Rival Org (For Multi-Tenant IDOR Verification)
        # ------------------------------------------------------------------
        org_b = Organization(name="Northstar Logistics", slug="northstar-logistics", is_active=True)
        db.session.add(org_b)
        db.session.flush()

        admin_b = User(
            organization_id=org_b.id,
            email="admin@northstar.dev",
            password_hash=default_pw,
            full_name="Evan Northstar",
            role="super_admin",
            current_tier="slate",
        )
        db.session.add(admin_b)
        db.session.flush()

        project_b = Project(
            organization_id=org_b.id,
            lead_id=admin_b.id,
            title="Fleet Dispatch Portal",
            status="active",
            est_completion_date=today + timedelta(days=30),
        )
        db.session.add(project_b)

        db.session.commit()

        click.echo("Seeded TaskFlow v2.2 development fixtures successfully:")
        click.echo("  Tenant A (Aurelia Studio): admin@aurelia.dev | lead@aurelia.dev | joey@aurelia.dev | iris@aurelia.dev")
        click.echo("  Tenant B (Northstar):      admin@northstar.dev")
        click.echo("  Password for all users:    TaskFlowDev123!")
        click.echo(f"  Tenant A Invite Code:      {raw_invite_a}")
        click.echo(f"  Tenant A Share Token:      {raw_share_token}")