from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.blueprints.admin import admin_bp
from app.blueprints.admin.forms import GenerateInviteForm, WorkspaceSettingsForm
from app.models import InviteCode
from app.services.admin_service import (
    AdminServiceError,
    revoke_invite_code,
    update_workspace_settings,
)
from app.services.tenant_service import TenantServiceError, generate_invite_code
from app.utils.decorators import role_required


@admin_bp.route("/settings", methods=["GET", "POST"])
@role_required("super_admin")
def settings():
    org = current_user.organization
    org_settings = org.settings
    form = WorkspaceSettingsForm()
    invite_form = GenerateInviteForm()

    if request.method == "GET":
        form.brand_name.data = org_settings.brand_name
        form.theme_preset.data = org_settings.theme_preset
        form.custom_accent_hex.data = org_settings.custom_accent_hex
        form.acceptance_mode.data = org_settings.acceptance_mode
        form.pending_sla_hours.data = org_settings.pending_sla_hours
        form.require_review_gate.data = org_settings.require_review_gate
        form.allow_client_share_links.data = org_settings.allow_client_share_links
        form.invite_expiry_hours.data = org_settings.invite_expiry_hours
        form.daily_digest_mode.data = org_settings.daily_digest_mode

        cols = org_settings.column_labels or {}
        form.col_todo.data = cols.get("todo", "To Do")
        form.col_in_progress.data = cols.get("in_progress", "In Progress")
        form.col_review.data = cols.get("review", "In Review")
        form.col_done.data = cols.get("done", "Done")

        form.recognition_enabled.data = org_settings.recognition_enabled
        form.on_time_bonus_pct.data = org_settings.on_time_bonus_pct
        form.internal_ext_penalty_pct.data = org_settings.internal_ext_penalty_pct
        form.tier_reset_cadence.data = org_settings.tier_reset_cadence

        weights = org_settings.priority_weights or {}
        form.weight_low.data = weights.get("low", 10)
        form.weight_medium.data = weights.get("medium", 25)
        form.weight_high.data = weights.get("high", 50)
        form.weight_critical.data = weights.get("critical", 100)

        tiers = org_settings.tier_thresholds or {}
        form.tier_emerald.data = tiers.get("emerald", 250)
        form.tier_indigo.data = tiers.get("indigo", 750)
        form.tier_gold.data = tiers.get("gold", 2000)

    if form.validate_on_submit():
        try:
            update_workspace_settings(
                settings=org_settings,
                actor=current_user,
                brand_name=form.brand_name.data,
                theme_preset=form.theme_preset.data,
                custom_accent_hex=form.custom_accent_hex.data,
                remove_logo=form.remove_logo.data,
                logo_file=form.logo_file.data,
                acceptance_mode=form.acceptance_mode.data,
                pending_sla_hours=form.pending_sla_hours.data,
                require_review_gate=form.require_review_gate.data,
                allow_client_share_links=form.allow_client_share_links.data,
                invite_expiry_hours=form.invite_expiry_hours.data,
                daily_digest_mode=form.daily_digest_mode.data,
                recognition_enabled=form.recognition_enabled.data,
                on_time_bonus_pct=form.on_time_bonus_pct.data,
                internal_ext_penalty_pct=form.internal_ext_penalty_pct.data,
                tier_reset_cadence=form.tier_reset_cadence.data,
                column_labels={
                    "todo": form.col_todo.data,
                    "in_progress": form.col_in_progress.data,
                    "review": form.col_review.data,
                    "done": form.col_done.data,
                },
                priority_weights={
                    "low": form.weight_low.data,
                    "medium": form.weight_medium.data,
                    "high": form.weight_high.data,
                    "critical": form.weight_critical.data,
                },
                tier_thresholds={
                    "slate": 0,
                    "emerald": form.tier_emerald.data,
                    "indigo": form.tier_indigo.data,
                    "gold": form.tier_gold.data,
                },
            )
            flash("Workspace branding and governance settings updated.", "success")
            return redirect(url_for("admin.settings"))
        except AdminServiceError as exc:
            flash(str(exc), "danger")

    active_invites = (
        InviteCode.query.filter_by(organization_id=org.id, is_revoked=False)
        .order_by(InviteCode.created_at.desc())
        .all()
    )
    return render_template(
        "admin/settings.html",
        form=form,
        invite_form=invite_form,
        active_invites=active_invites,
    )


@admin_bp.post("/invites/generate")
@role_required("super_admin")
def create_invite():
    invite_form = GenerateInviteForm()
    if invite_form.validate_on_submit():
        try:
            _, raw_token = generate_invite_code(
                organization_id=current_user.organization_id,
                created_by=current_user,
                assigned_role=invite_form.assigned_role.data,
                max_uses=invite_form.max_uses.data,
            )
            flash(
                f"Invite created ({invite_form.assigned_role.data}). Copy token now—it will not be shown again: {raw_token}",
                "success",
            )
        except TenantServiceError as exc:
            flash(str(exc), "danger")
    return redirect(url_for("admin.settings"))


@admin_bp.post("/invites/<int:invite_id>/revoke")
@role_required("super_admin")
def revoke_invite(invite_id: int):
    try:
        revoke_invite_code(invite_id=invite_id, actor=current_user)
        flash("Invite code revoked.", "info")
    except AdminServiceError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.settings"))


from app.models import AuditLog, InviteCode, Project, User
from app.services.governance_service import (
    GovernanceServiceError,
    archive_project,
    delete_empty_project,
    query_audit_logs,
    reject_project_deletion,
    soft_remove_member,
    transfer_project_lead,
    unarchive_project,
)


@admin_bp.get("/governance")
@role_required("super_admin")
def governance():
    org = current_user.organization
    members = (
        User.query.filter_by(organization_id=org.id)
        .order_by(User.is_active.desc(), User.full_name.asc())
        .all()
    )
    projects = (
        Project.query.filter_by(organization_id=org.id)
        .order_by(Project.created_at.desc())
        .all()
    )
    action_filter = request.args.get("action", "").strip() or None
    logs = query_audit_logs(organization_id=org.id, action=action_filter, limit=100)


    pending_deletions = (
    AuditLog.query.filter(
        AuditLog.organization_id == org.id,
        AuditLog.action == "project_delete_requested",
        AuditLog.project_id.isnot(None),
    )
    .order_by(AuditLog.created_at.desc())
    .all()
    )

    return render_template(
        "admin/governance.html",
        members=members,
        projects=projects,
        logs=logs,
        action_filter=action_filter,
    )


@admin_bp.post("/governance/transfer-lead")
@role_required("super_admin")
def handover_lead():
    project_id = request.form.get("project_id", type=int)
    new_lead_id = request.form.get("new_lead_id", type=int)
    reason = request.form.get("reason", "")

    try:
        project = transfer_project_lead(
            project_id=project_id,
            new_lead_id=new_lead_id,
            actor=current_user,
            reason=reason,
        )
        flash(f"Transferred '{project.title}' lead ownership to {project.lead.full_name}.", "success")
    except GovernanceServiceError as exc:
        flash(str(exc), "danger")

    return redirect(url_for("admin.governance"))


@admin_bp.post("/governance/members/<int:user_id>/role")
@role_required("super_admin")
def change_role(user_id: int):
    new_role = request.form.get("role", "").strip()
    try:
        user = update_member_role(
            target_user_id=user_id,
            new_role=new_role,
            actor=current_user,
        )
        flash(f"Updated {user.full_name}'s role to {user.role.replace('_', ' ')}.", "success")
    except GovernanceServiceError as exc:
        flash(str(exc), "danger")

    return redirect(url_for("admin.governance"))


@admin_bp.post("/governance/members/<int:user_id>/deactivate")
@role_required("super_admin")
def deactivate_member(user_id: int):
    reason = request.form.get("reason", "")
    try:
        user = soft_remove_member(
            target_user_id=user_id,
            actor=current_user,
            reason=reason,
        )
        flash(f"Deactivated {user.full_name} and returned open tasks to unassigned queue.", "info")
    except GovernanceServiceError as exc:
        flash(str(exc), "danger")

    return redirect(url_for("admin.governance"))


from app.services.governance_service import (
    GovernanceServiceError,
    archive_project,
    delete_empty_project,
    query_audit_logs,
    soft_remove_member,
    transfer_project_lead,
    update_member_role,
)


@admin_bp.post("/governance/projects/<int:project_id>/delete")
@role_required("super_admin")
def purge_project(project_id: int):
    reason = request.form.get("reason", "")
    try:
        title = delete_empty_project(
            project_id=project_id,
            actor=current_user,
            reason=reason,
        )
        flash(f"Permanently deleted unused project '{title}'.", "info")
    except GovernanceServiceError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.governance"))