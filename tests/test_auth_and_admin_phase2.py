import io
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash

from app.models import AuditLog, InviteCode, Organization, User
from app.services.tenant_service import generate_invite_code, hash_token


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def seed_org_with_roles(db_session, slug="phase2-org", name="Phase 2 Org"):
    org = Organization(name=name, slug=slug, is_active=True)
    db_session.add(org)
    db_session.flush()

    pw = generate_password_hash("StrongPass123!")
    admin = User(
        organization_id=org.id,
        email=f"admin@{slug}.dev",
        password_hash=pw,
        full_name="Super Admin",
        role="super_admin",
        is_active=True,
    )
    lead = User(
        organization_id=org.id,
        email=f"lead@{slug}.dev",
        password_hash=pw,
        full_name="Project Lead",
        role="project_lead",
        is_active=True,
    )
    member = User(
        organization_id=org.id,
        email=f"member@{slug}.dev",
        password_hash=pw,
        full_name="Team Member",
        role="member",
        is_active=True,
    )
    db_session.add_all([admin, lead, member])
    db_session.commit()
    return org, admin, lead, member


def login_as(client, email: str, password: str = "StrongPass123!"):
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def test_login_open_redirect_protection(client, db_session):
    _, admin, _, _ = seed_org_with_roles(db_session, slug="redirect-org")

    # External host redirect attempt must be ignored and routed to /
    res = client.post(
        "/login?next=https://evil-phishing.example.com/steal",
        data={"email": admin.email, "password": "StrongPass123!"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/")

    # Protocol-relative redirect attempt (//evil.com) must also be blocked
    client.post("/logout")
    res2 = client.post(
        "/login?next=//evil.example.com",
        data={"email": admin.email, "password": "StrongPass123!"},
        follow_redirects=False,
    )
    assert res2.status_code == 302
    assert res2.headers["Location"].endswith("/")


def test_private_mode_blocks_new_org_creation_and_requires_invite(app, client, db_session):
    app.config["APP_MODE"] = "private"
    org, admin, _, _ = seed_org_with_roles(db_session, slug="private-org")

    # Tampered POST attempting signup_mode="new_org" in private mode must fail without invite code
    res = client.post(
        "/signup",
        data={
            "signup_mode": "new_org",
            "org_name": "Rogue Tenant",
            "full_name": "Rogue User",
            "email": "rogue@private-org.dev",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
        },
        follow_redirects=True,
    )
    assert b"A valid workspace invite code is required." in res.data
    assert Organization.query.filter_by(name="Rogue Tenant").first() is None

    # Valid invite code redemption succeeds and assigns exact bound role
    _, raw_token = generate_invite_code(
        organization_id=org.id,
        created_by=admin,
        assigned_role="project_lead",
        max_uses=1,
    )
    res_ok = client.post(
        "/signup",
        data={
            "signup_mode": "invite",
            "invite_code": raw_token,
            "full_name": "Invited Lead",
            "email": "invited.lead@private-org.dev",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
        },
        follow_redirects=False,
    )
    assert res_ok.status_code == 302
    joined = User.query.filter_by(email="invited.lead@private-org.dev").one()
    assert joined.organization_id == org.id
    assert joined.role == "project_lead"

    # Reusing a single-use invite code must fail
    client.post("/logout")
    res_reuse = client.post(
        "/signup",
        data={
            "signup_mode": "invite",
            "invite_code": raw_token,
            "full_name": "Second User",
            "email": "second@private-org.dev",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
        },
        follow_redirects=True,
    )
    assert b"maximum uses" in res_reuse.data


def test_saas_mode_creates_new_tenant_and_super_admin(app, client, db_session):
    app.config["APP_MODE"] = "saas"

    res = client.post(
        "/signup",
        data={
            "signup_mode": "new_org",
            "org_name": "Mija Coffee Roasters",
            "full_name": "Mija Owner",
            "email": "owner@mijacoffee.dev",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
        },
        follow_redirects=False,
    )
    assert res.status_code == 302

    org = Organization.query.filter_by(name="Mija Coffee Roasters").one()
    owner = User.query.filter_by(email="owner@mijacoffee.dev").one()
    assert owner.organization_id == org.id
    assert owner.role == "super_admin"
    assert org.settings is not None


def test_deactivated_user_is_immediately_evicted_mid_session(client, db_session):
    _, _, _, member = seed_org_with_roles(db_session, slug="evict-org")

    res = login_as(client, member.email)
    assert res.status_code == 302

    # Confirm authenticated access works while active
    dash_ok = client.get("/")
    assert dash_ok.status_code == 200

    # Super Admin soft-removes the user (is_active = False)
    member.is_active = False
    db_session.commit()

    # Next request must immediately terminate session and redirect to /login
    dash_evicted = client.get("/", follow_redirects=False)
    assert dash_evicted.status_code == 302
    assert "/login" in dash_evicted.headers["Location"]


def test_rbac_blocks_non_super_admin_from_workspace_settings(client, db_session):
    _, _, lead, member = seed_org_with_roles(db_session, slug="rbac-org")

    # Project Lead blocked
    login_as(client, lead.email)
    res_lead = client.get("/admin/settings", follow_redirects=False)
    assert res_lead.status_code == 302
    assert res_lead.headers["Location"].endswith("/")
    client.post("/logout")

    # Member blocked
    login_as(client, member.email)
    res_member = client.get("/admin/settings", follow_redirects=False)
    assert res_member.status_code == 302
    assert res_member.headers["Location"].endswith("/")


def _base_settings_payload(overrides: dict | None = None) -> dict:
    payload = {
        "brand_name": "Aurelia White-Label",
        "theme_preset": "calm_sage",
        "custom_accent_hex": "",
        "acceptance_mode": "strict",
        "pending_sla_hours": 48,
        "require_review_gate": "y",
        "allow_client_share_links": "y",
        "invite_expiry_hours": 72,
        "daily_digest_mode": "banner",
        "col_todo": "To Do",
        "col_in_progress": "In Progress",
        "col_review": "Client QA",
        "col_done": "Done",
        "recognition_enabled": "y",
        "on_time_bonus_pct": 20,
        "internal_ext_penalty_pct": 50,
        "tier_reset_cadence": "lifetime",
        "weight_low": 10,
        "weight_medium": 25,
        "weight_high": 50,
        "weight_critical": 100,
        "tier_emerald": 250,
        "tier_indigo": 750,
        "tier_gold": 2000,
    }
    if overrides:
        payload.update(overrides)
    return payload


def test_admin_settings_blocks_svg_spoof_and_neon_hex(client, db_session):
    org, admin, _, _ = seed_org_with_roles(db_session, slug="branding-sec-org")
    login_as(client, admin.email)

    # 1. Spoofed SVG payload renamed as .png must be rejected by magic-byte inspection
    fake_png_svg = (
        io.BytesIO(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        "malicious_logo.png",
    )
    res_svg = client.post(
        "/admin/settings",
        data=_base_settings_payload({"logo_file": fake_png_svg}),
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Invalid image format. Only PNG, JPG, or WEBP raster files are allowed." in res_svg.data
    assert org.settings.logo_url is None

    # 2. High-saturation neon hex (#FF00FF) must be rejected to protect low-arousal UI
    res_neon = client.post(
        "/admin/settings",
        data=_base_settings_payload({"custom_accent_hex": "#FF00FF"}),
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"neon colors are blocked" in res_neon.data
    assert org.settings.custom_accent_hex is None

    # 3. Valid PNG magic bytes + low-arousal muted hex (#5A7A8C) succeeds & writes AuditLog
    valid_png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    res_valid = client.post(
        "/admin/settings",
        data=_base_settings_payload(
            {
                "custom_accent_hex": "#5A7A8C",
                "logo_file": (io.BytesIO(valid_png_header), "clean_logo.png"),
            }
        ),
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Workspace branding and governance settings updated." in res_valid.data
    db_session.refresh(org.settings)
    assert org.settings.brand_name == "Aurelia White-Label"
    assert org.settings.custom_accent_hex == "#5A7A8C"
    assert org.settings.column_labels["review"] == "Client QA"
    assert org.settings.logo_url is not None

    audit = AuditLog.query.filter_by(
        organization_id=org.id, action="settings_updated"
    ).first()
    assert audit is not None
    assert audit.diff_payload["after"]["brand_name"] == "Aurelia White-Label"


def test_cross_tenant_invite_revocation_blocked(client, db_session):
    org_a, admin_a, _, _ = seed_org_with_roles(db_session, slug="tenant-a", name="Tenant A")
    org_b, admin_b, _, _ = seed_org_with_roles(db_session, slug="tenant-b", name="Tenant B")

    invite_b = InviteCode(
        organization_id=org_b.id,
        code_hash=hash_token("TENANT-B-SECRET"),
        assigned_role="member",
        created_by_id=admin_b.id,
        expires_at=utc_now() + timedelta(hours=24),
    )
    db_session.add(invite_b)
    db_session.commit()

    # Admin A attempting to revoke Tenant B's invite ID must fail cleanly
    login_as(client, admin_a.email)
    res = client.post(f"/admin/invites/{invite_b.id}/revoke", follow_redirects=True)
    assert b"Invite code not found in your workspace." in res.data
    db_session.refresh(invite_b)
    assert invite_b.is_revoked is False