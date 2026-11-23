from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.projects import projects_bp
from app.blueprints.projects.forms import ProjectCreateForm, SaveTemplateForm
from app.models import Project, ProjectTemplate, User
from app.services.task_service import (
    TaskServiceError,
    create_project,
    get_tenant_project,
    instantiate_project_template,
    is_pending_sla_expired,
    save_project_as_template,
)
from app.utils.decorators import role_required


def _populate_project_form_choices(form: ProjectCreateForm, org_id: int) -> None:
    leads = (
        User.query.filter(
            User.organization_id == org_id,
            User.is_active.is_(True),
            User.role.in_(["super_admin", "project_lead"]),
        )
        .order_by(User.full_name.asc())
        .all()
    )
    form.lead_id.choices = [(u.id, f"{u.full_name} ({u.role.replace('_', ' ').title()})") for u in leads]

    templates = (
        ProjectTemplate.query.filter_by(organization_id=org_id)
        .order_by(ProjectTemplate.name.asc())
        .all()
    )
    form.template_id.choices = [(0, "— Blank Project —")] + [(t.id, t.name) for t in templates]


@projects_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    org = current_user.organization
    form = ProjectCreateForm()
    _populate_project_form_choices(form, org.id)

    if request.method == "GET" and form.lead_id.choices:
        form.lead_id.data = current_user.id

    if form.validate_on_submit():
        try:
            tpl_id = form.template_id.data if form.template_id.data and form.template_id.data > 0 else None
            project = create_project(
                actor=current_user,
                title=form.title.data,
                description=form.description.data,
                est_completion_date=form.est_completion_date.data,
                lead_id=form.lead_id.data if current_user.is_super_admin else current_user.id,
                template_id=tpl_id,
            )
            flash(f"Project '{project.title}' created.", "success")
            return redirect(url_for("projects.board", project_id=project.id))
        except TaskServiceError as exc:
            flash(str(exc), "danger")

    projects = (
        Project.query.filter_by(organization_id=org.id)
        .order_by(Project.created_at.desc())
        .all()
    )
    templates = (
        ProjectTemplate.query.filter_by(organization_id=org.id)
        .order_by(ProjectTemplate.name.asc())
        .all()
    )
    return render_template(
        "projects/index.html",
        projects=projects,
        templates=templates,
        form=form,
    )


@projects_bp.route("/<int:project_id>/board")
@login_required
def board(project_id: int):
    org = current_user.organization
    try:
        project = get_tenant_project(project_id, org.id)
    except TaskServiceError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("projects.index"))

    settings = org.settings
    sla_hours = settings.pending_sla_hours if settings else 48

    # Group top-level parent tasks by Kanban column; subtasks nest inside their parent card
    columns = {"todo": [], "in_progress": [], "review": [], "done": []}
    expired_pending_ids = set()

    for task in project.tasks:
        if is_pending_sla_expired(task, sla_hours):
            expired_pending_ids.add(task.id)
        if task.parent_task_id is None:
            columns.setdefault(task.board_status, []).append(task)

    members = (
        User.query.filter_by(organization_id=org.id, is_active=True)
        .order_by(User.full_name.asc())
        .all()
    )
    templates = (
        ProjectTemplate.query.filter_by(organization_id=org.id)
        .order_by(ProjectTemplate.name.asc())
        .all()
    )
    template_form = SaveTemplateForm()

    return render_template(
        "projects/board.html",
        project=project,
        columns=columns,
        column_labels=settings.column_labels,
        members=members,
        templates=templates,
        template_form=template_form,
        expired_pending_ids=expired_pending_ids,
    )


@projects_bp.post("/<int:project_id>/save-template")
@role_required("super_admin", "project_lead")
def save_template(project_id: int):
    form = SaveTemplateForm()
    if form.validate_on_submit():
        try:
            tpl = save_project_as_template(
                project_id=project_id,
                actor=current_user,
                name=form.name.data,
                description=form.description.data,
            )
            flash(f"Saved reusable template '{tpl.name}'.", "success")
        except TaskServiceError as exc:
            flash(str(exc), "danger")
    return redirect(url_for("projects.board", project_id=project_id))


@projects_bp.post("/<int:project_id>/apply-template")
@role_required("super_admin", "project_lead")
def apply_template(project_id: int):
    template_id = request.form.get("template_id", type=int)
    if not template_id:
        flash("Please select a template to apply.", "warning")
        return redirect(url_for("projects.board", project_id=project_id))

    try:
        project = get_tenant_project(project_id, current_user.organization_id)
        if not current_user.is_super_admin and project.lead_id != current_user.id:
            raise TaskServiceError("Only the Project Lead or Super Admin can apply templates.")
        created = instantiate_project_template(
            project=project, template_id=template_id, actor=current_user, commit=True
        )
        flash(f"Added {len(created)} task(s) from template.", "success")
    except TaskServiceError as exc:
        flash(str(exc), "danger")

    return redirect(url_for("projects.board", project_id=project_id))