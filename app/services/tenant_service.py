import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import AuditLog, InviteCode, Organization, User


class TenantServiceError(Exception):
    """Domain validation error surfaced cleanly to forms/routes."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_utc(dt: datetime) -> datetime:
    """Ensures SQLite naive datetimes compare safely with UTC-aware datetimes."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def slugify_org_name(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "workspace"
    slug = base[:120]
    counter = 1
    while Organization.query.filter_by(slug=slug).first() is not None:
        suffix = f"-{counter}"
        slug = f"{base[: 120 - len(suffix)]}{suffix}"
        counter += 1
    return slug


def authenticate_user(email: str, password: str) -> User:
    normalized_email = email.strip().lower()
    user = User.query.filter_by(email=normalized_email).first()

    # Constant-time fallback check prevents user enumeration timing leaks
    if user is None:
        check_password_hash(
            "pbkdf2:sha256:600000$dummy$00000000000000000000000000000000", password
        )
        raise TenantServiceError("Invalid email or password.")

    if not check_password_hash(user.password_hash, password):
        raise TenantServiceError("Invalid email or password.")

    if not user.is_active or not user.organization.is_active:
        raise TenantServiceError("This account or workspace has been deactivated.")

    return user


def validate_invite_code(raw_code: str) -> InviteCode:
    if not raw_code or not raw_code.strip():
        raise TenantServiceError("A valid workspace invite code is required.")

    code_hash = hash_token(raw_code)
    invite = InviteCode.query.filter_by(code_hash=code_hash).first()

    if invite is None or invite.is_revoked:
        raise TenantServiceError("Invalid or revoked invite code.")

    if not invite.organization.is_active:
        raise TenantServiceError("The target workspace is currently inactive.")

    if invite.use_count >= invite.max_uses:
        raise TenantServiceError("This invite code has reached its maximum uses.")

    if normalize_utc(invite.expires_at) <= utc_now():
        raise TenantServiceError("This invite code has expired.")

    return invite


def register_via_invite(
    *, full_name: str, email: str, password: str, raw_invite_code: str
) -> User:
    normalized_email = email.strip().lower()
    if User.query.filter_by(email=normalized_email).first():
        raise TenantServiceError("An account with this email already exists.")

    invite = validate_invite_code(raw_invite_code)

    user = User(
        organization_id=invite.organization_id,
        email=normalized_email,
        password_hash=generate_password_hash(password),
        full_name=full_name.strip(),
        role=invite.assigned_role,
        is_active=True,
    )
    db.session.add(user)
    db.session.flush()

    invite.use_count += 1
    if invite.max_uses == 1:
        invite.used_by_id = user.id

    db.session.add(
        AuditLog(
            organization_id=invite.organization_id,
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
            action="user_joined_via_invite",
            reason=f"Joined workspace with role '{invite.assigned_role}'.",
        )
    )
    db.session.commit()
    return user


def register_new_organization(
    *, org_name: str, full_name: str, email: str, password: str
) -> User:
    """Allowed only when APP_MODE == 'saas'."""
    normalized_email = email.strip().lower()
    if User.query.filter_by(email=normalized_email).first():
        raise TenantServiceError("An account with this email already exists.")

    clean_org_name = org_name.strip()
    if len(clean_org_name) < 2:
        raise TenantServiceError("Workspace name must be at least 2 characters.")

    org = Organization(
        name=clean_org_name,
        slug=slugify_org_name(clean_org_name),
        is_active=True,
    )
    db.session.add(org)
    db.session.flush()  # Triggers auto-creation of OrganizationSettings

    admin = User(
        organization_id=org.id,
        email=normalized_email,
        password_hash=generate_password_hash(password),
        full_name=full_name.strip(),
        role="super_admin",
        is_active=True,
    )
    db.session.add(admin)
    db.session.flush()

    db.session.add(
        AuditLog(
            organization_id=org.id,
            actor_id=admin.id,
            target_type="organization",
            target_id=org.id,
            action="organization_created",
            reason="Self-service SaaS workspace registration.",
        )
    )
    db.session.commit()
    return admin


def generate_invite_code(
    *,
    organization_id: int,
    created_by: User,
    assigned_role: str = "member",
    max_uses: int = 1,
    expiry_hours: int | None = None,
) -> tuple[InviteCode, str]:
    if created_by.organization_id != organization_id or created_by.role not in (
        "super_admin",
        "project_lead",
    ):
        raise TenantServiceError("Not authorized to generate workspace invites.")

    if assigned_role not in ("project_lead", "member"):
        raise TenantServiceError("Invite codes can only assign 'project_lead' or 'member'.")

    # Only Super Admins can issue project_lead invites
    if assigned_role == "project_lead" and created_by.role != "super_admin":
        raise TenantServiceError("Only a Super Admin can invite a Project Lead.")

    org = db.session.get(Organization, organization_id)
    hours = expiry_hours or (org.settings.invite_expiry_hours if org and org.settings else 72)

    raw_token = f"TF-{secrets.token_urlsafe(12)}"
    invite = InviteCode(
        organization_id=organization_id,
        code_hash=hash_token(raw_token),
        assigned_role=assigned_role,
        created_by_id=created_by.id,
        max_uses=max(1, min(max_uses, 100)),
        expires_at=utc_now() + timedelta(hours=hours),
    )
    db.session.add(invite)
    db.session.flush()
    db.session.add(
        AuditLog(
            organization_id=organization_id,
            actor_id=created_by.id,
            target_type="invite_code",
            target_id=0,
            action="invite_generated",
            reason=f"Generated {assigned_role} invite (max_uses={invite.max_uses}).",
        )
    )
    db.session.commit()
    return invite, raw_token