import colorsys
import os
import re
from datetime import datetime, timezone
from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import AuditLog, InviteCode, OrganizationSettings, Project, Task, User


class AdminServiceError(Exception):
    """Domain validation error surfaced cleanly to Super Admin forms."""


ALLOWED_PRESETS = {"calm_sage", "soft_terracotta", "dusty_lavender", "forest_moss"}
HEX_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def detect_raster_magic_bytes(header: bytes) -> str | None:
    """Verifies actual binary file signature—blocks SVG, HTML, and spoofed extensions."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    return None


def validate_low_arousal_hex(hex_color: str | None) -> str | None:
    """Enforces 6-digit hex format and rejects harsh neon or unreadable lightness."""
    if not hex_color or not hex_color.strip():
        return None

    cleaned = hex_color.strip().upper()
    if not HEX_PATTERN.match(cleaned):
        raise AdminServiceError("Custom accent must be a valid 6-digit hex color (e.g., #5B7C8D).")

    r = int(cleaned[1:3], 16) / 255.0
    g = int(cleaned[3:5], 16) / 255.0
    b = int(cleaned[5:7], 16) / 255.0
    _, lightness, saturation = colorsys.rgb_to_hls(r, g, b)

    if saturation > 0.65:
        raise AdminServiceError(
            "Accent saturation is too high (neon colors are blocked to preserve low-arousal ergonomics)."
        )
    if not (0.28 <= lightness <= 0.68):
        raise AdminServiceError(
            "Accent lightness must fall between 28% and 68% for readable contrast in Light and Dark themes."
        )

    return cleaned


def process_logo_upload(file_storage: FileStorage, org_id: int, old_public_id: str | None) -> tuple[str, str]:
    """Validates raster magic bytes and uploads to Cloudinary (or local fallback in dev)."""
    raw_bytes = file_storage.read(1024 * 1024 + 1)
    file_storage.seek(0)

    if len(raw_bytes) == 0:
        raise AdminServiceError("Uploaded logo file is empty.")
    if len(raw_bytes) > 1024 * 1024:
        raise AdminServiceError("Logo file exceeds the 1 MB maximum limit.")

    detected_ext = detect_raster_magic_bytes(raw_bytes[:16])
    if detected_ext is None:
        raise AdminServiceError("Invalid image format. Only PNG, JPG, or WEBP raster files are allowed.")

    cloudinary_url = current_app.config.get("CLOUDINARY_URL")
    if cloudinary_url:
        import cloudinary
        import cloudinary.uploader

        if old_public_id:
            try:
                cloudinary.uploader.destroy(old_public_id)
            except Exception:
                pass

        result = cloudinary.uploader.upload(
            file_storage,
            folder=f"taskflow/org_{org_id}",
            resource_type="image",
            format="webp",
            transformation=[{"width": 256, "height": 256, "crop": "limit"}],
        )
        return result["secure_url"], result["public_id"]

    # Local static fallback when CLOUDINARY_URL is unset
    upload_dir = os.path.join(current_app.static_folder, "uploads", "logos")
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = secure_filename(f"org_{org_id}_logo.{detected_ext}")
    dest_path = os.path.join(upload_dir, safe_name)
    file_storage.save(dest_path)
    return f"/static/uploads/logos/{safe_name}", f"local:{safe_name}"


def update_workspace_settings(
    *,
    settings: OrganizationSettings,
    actor: User,
    brand_name: str | None,
    theme_preset: str,
    custom_accent_hex: str | None,
    remove_logo: bool,
    logo_file: FileStorage | None,
    acceptance_mode: str,
    pending_sla_hours: int,
    require_review_gate: bool,
    allow_client_share_links: bool,
    invite_expiry_hours: int,
    daily_digest_mode: str,
    recognition_enabled: bool,
    on_time_bonus_pct: int,
    internal_ext_penalty_pct: int,
    tier_reset_cadence: str,
    column_labels: dict[str, str],
    priority_weights: dict[str, int],
    tier_thresholds: dict[str, int],
) -> OrganizationSettings:
    if actor.role != "super_admin" or actor.organization_id != settings.organization_id:
        raise AdminServiceError("Only a workspace Super Admin can modify organization settings.")

    if theme_preset not in ALLOWED_PRESETS:
        raise AdminServiceError("Invalid theme preset selected.")

    validated_hex = validate_low_arousal_hex(custom_accent_hex)

    # Validate Column Labels
    expected_cols = {"todo", "in_progress", "review", "done"}
    if set(column_labels.keys()) != expected_cols:
        raise AdminServiceError("All four Kanban column labels are required.")
    cleaned_cols = {}
    seen_lower = set()
    for key, val in column_labels.items():
        clean_val = (val or "").strip()
        if not (2 <= len(clean_val) <= 30):
            raise AdminServiceError(f"Column '{key}' label must be between 2 and 30 characters.")
        if clean_val.lower() in seen_lower:
            raise AdminServiceError("Kanban column labels must be unique.")
        seen_lower.add(clean_val.lower())
        cleaned_cols[key] = clean_val

    # Validate Priority Weights (monotonic ascending)
    expected_priorities = ["low", "medium", "high", "critical"]
    if set(priority_weights.keys()) != set(expected_priorities):
        raise AdminServiceError("All four priority weights are required.")
    for p_key, p_val in priority_weights.items():
        if not (0 <= p_val <= 1000):
            raise AdminServiceError(f"Priority weight '{p_key}' must be between 0 and 1000.")
    if not (
        priority_weights["low"]
        <= priority_weights["medium"]
        <= priority_weights["high"]
        <= priority_weights["critical"]
    ):
        raise AdminServiceError("Priority weights must increase from Low to Critical.")

    # Validate Tier Thresholds (strictly ascending from slate=0)
    expected_tiers = {"slate", "emerald", "indigo", "gold"}
    if set(tier_thresholds.keys()) != expected_tiers:
        raise AdminServiceError("All four performance tier thresholds are required.")
    if tier_thresholds["slate"] != 0:
        raise AdminServiceError("Slate tier threshold must start at 0.")
    if not (
        0
        < tier_thresholds["emerald"]
        < tier_thresholds["indigo"]
        < tier_thresholds["gold"]
        <= 1_000_000
    ):
        raise AdminServiceError("Tier thresholds must strictly increase: Slate (0) < Emerald < Indigo < Gold.")

    before_snapshot = {
        "brand_name": settings.brand_name,
        "theme_preset": settings.theme_preset,
        "custom_accent_hex": settings.custom_accent_hex,
        "acceptance_mode": settings.acceptance_mode,
        "require_review_gate": settings.require_review_gate,
        "recognition_enabled": settings.recognition_enabled,
        "tier_reset_cadence": settings.tier_reset_cadence,
        "daily_digest_mode": settings.daily_digest_mode,
    }

    # Prevent orphaned "pending" tasks when switching Strict -> Direct
    auto_confirmed_count = 0
    if settings.acceptance_mode == "strict" and acceptance_mode == "direct":
        pending_tasks = (
            Task.query.join(Project, Task.project_id == Project.id)
            .filter(
                Project.organization_id == settings.organization_id,
                Task.assignment_status == "pending",
                Task.board_status != "done",
            )
            .all()
        )
        for task in pending_tasks:
            task.assignment_status = "confirmed"
            auto_confirmed_count += 1

    # Handle Logo Replacement / Removal
    if remove_logo:
        settings.logo_url = None
        settings.logo_public_id = None
    elif logo_file and logo_file.filename:
        new_url, new_public_id = process_logo_upload(
            logo_file, settings.organization_id, settings.logo_public_id
        )
        settings.logo_url = new_url
        settings.logo_public_id = new_public_id

    clean_brand = (brand_name or "").strip()
    if clean_brand and len(clean_brand) < 2:
        raise AdminServiceError("Custom brand name must be at least 2 characters.")

    settings.brand_name = clean_brand or None
    settings.theme_preset = theme_preset
    settings.custom_accent_hex = validated_hex
    settings.acceptance_mode = acceptance_mode
    settings.pending_sla_hours = pending_sla_hours
    settings.require_review_gate = require_review_gate
    settings.allow_client_share_links = allow_client_share_links
    settings.invite_expiry_hours = invite_expiry_hours
    settings.daily_digest_mode = daily_digest_mode
    settings.recognition_enabled = recognition_enabled
    settings.on_time_bonus_pct = on_time_bonus_pct
    settings.internal_ext_penalty_pct = internal_ext_penalty_pct
    settings.tier_reset_cadence = tier_reset_cadence
    settings.column_labels = cleaned_cols
    settings.priority_weights = priority_weights
    settings.tier_thresholds = tier_thresholds
    settings.updated_by_id = actor.id

    after_snapshot = {
        "brand_name": settings.brand_name,
        "theme_preset": settings.theme_preset,
        "custom_accent_hex": settings.custom_accent_hex,
        "acceptance_mode": settings.acceptance_mode,
        "require_review_gate": settings.require_review_gate,
        "recognition_enabled": settings.recognition_enabled,
        "tier_reset_cadence": settings.tier_reset_cadence,
        "daily_digest_mode": settings.daily_digest_mode,
    }

    audit_reason = "Updated workspace branding & governance configuration."
    if auto_confirmed_count > 0:
        audit_reason += f" Auto-confirmed {auto_confirmed_count} in-flight pending task(s) upon switching to direct mode."

    db.session.add(
        AuditLog(
            organization_id=settings.organization_id,
            actor_id=actor.id,
            target_type="org_settings",
            target_id=settings.id,
            action="settings_updated",
            reason=audit_reason,
            diff_payload={"before": before_snapshot, "after": after_snapshot},
        )
    )
    db.session.commit()
    return settings


def revoke_invite_code(*, invite_id: int, actor: User) -> InviteCode:
    invite = InviteCode.query.filter_by(
        id=invite_id, organization_id=actor.organization_id
    ).first()
    if invite is None:
        raise AdminServiceError("Invite code not found in your workspace.")

    invite.is_revoked = True
    db.session.add(
        AuditLog(
            organization_id=actor.organization_id,
            actor_id=actor.id,
            target_type="invite_code",
            target_id=invite.id,
            action="invite_revoked",
            reason="Manually revoked workspace invite code.",
        )
    )
    db.session.commit()
    return invite