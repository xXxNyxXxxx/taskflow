from datetime import date
from flask import flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from app.blueprints.board import board_bp
from app.services.task_service import (
    ConcurrencyConflictError,
    TaskServiceError,
    add_task_comment,
    create_task,
    extend_task_deadline,
    get_tenant_task,
    move_task_status,
    reassign_task,
    respond_to_assignment,
    toggle_task_blocker,
)


def _wants_json() -> bool:
    return (
        request.is_json
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )


def _parse_iso_date(raw: str | None) -> date:
    if not raw:
        raise TaskServiceError("Due date is required.")
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise TaskServiceError("Invalid date format (expected YYYY-MM-DD).") from exc


@board_bp.post("/projects/<int:project_id>/create")
@login_required
def create(project_id: int):
    title = request.form.get("title", "")
    description = request.form.get("description", "")
    priority = request.form.get("priority", "medium")
    assignee_id = request.form.get("assignee_id", type=int) or None
    parent_task_id = request.form.get("parent_task_id", type=int) or None

    try:
        due_date = _parse_iso_date(request.form.get("due_date"))
        task = create_task(
            project_id=project_id,
            actor=current_user,
            title=title,
            due_date=due_date,
            priority=priority,
            description=description,
            assignee_id=assignee_id,
            parent_task_id=parent_task_id,
        )
        flash(f"Created {'subtask' if task.is_subtask else 'task'} '{task.title}'.", "success")
    except TaskServiceError as exc:
        flash(str(exc), "danger")

    return redirect(url_for("projects.board", project_id=project_id))


@board_bp.post("/<int:task_id>/move")
@login_required
def move(task_id: int):
    data = request.get_json(silent=True) if request.is_json else request.form
    new_status = (data.get("board_status") or "").strip()
    expected_status = (data.get("expected_status") or "").strip() or None

    try:
        task = move_task_status(
            task_id=task_id,
            actor=current_user,
            new_status=new_status,
            expected_current_status=expected_status,
        )
        if _wants_json():
            return jsonify({"ok": True, "task_id": task.id, "board_status": task.board_status}), 200
        flash("Task status updated.", "success")
        return redirect(url_for("projects.board", project_id=task.project_id))

    except ConcurrencyConflictError as exc:
        if _wants_json():
            return jsonify({"ok": False, "error": str(exc), "conflict": True}), 409
        flash(str(exc), "warning")
        task = get_tenant_task(task_id, current_user.organization_id)
        return redirect(url_for("projects.board", project_id=task.project_id))

    except TaskServiceError as exc:
        if _wants_json():
            return jsonify({"ok": False, "error": str(exc)}), 400
        flash(str(exc), "danger")
        try:
            task = get_tenant_task(task_id, current_user.organization_id)
            return redirect(url_for("projects.board", project_id=task.project_id))
        except TaskServiceError:
            return redirect(url_for("projects.index"))


@board_bp.post("/<int:task_id>/respond")
@login_required
def respond(task_id: int):
    decision = (request.form.get("decision") or "").strip()
    decline_reason = request.form.get("decline_reason", "")

    try:
        task = respond_to_assignment(
            task_id=task_id,
            actor=current_user,
            decision=decision,
            decline_reason=decline_reason,
        )
        flash(
            "Task accepted and confirmed." if decision == "confirm" else "Task assignment declined.",
            "info",
        )
        return redirect(url_for("projects.board", project_id=task.project_id))
    except TaskServiceError as exc:
        flash(str(exc), "danger")
        try:
            task = get_tenant_task(task_id, current_user.organization_id)
            return redirect(url_for("projects.board", project_id=task.project_id))
        except TaskServiceError:
            return redirect(url_for("projects.index"))


@board_bp.post("/<int:task_id>/reassign")
@login_required
def reassign(task_id: int):
    new_assignee_id = request.form.get("assignee_id", type=int) or None
    try:
        task = reassign_task(
            task_id=task_id,
            actor=current_user,
            new_assignee_id=new_assignee_id,
        )
        flash("Task assignee updated.", "success")
        return redirect(url_for("projects.board", project_id=task.project_id))
    except TaskServiceError as exc:
        flash(str(exc), "danger")
        try:
            task = get_tenant_task(task_id, current_user.organization_id)
            return redirect(url_for("projects.board", project_id=task.project_id))
        except TaskServiceError:
            return redirect(url_for("projects.index"))


@board_bp.post("/<int:task_id>/blocker")
@login_required
def blocker(task_id: int):
    action = request.form.get("action", "set")
    is_blocked = action == "set"
    blocker_type = request.form.get("blocker_type")
    blocker_reason = request.form.get("blocker_reason", "")

    try:
        task = toggle_task_blocker(
            task_id=task_id,
            actor=current_user,
            is_blocked=is_blocked,
            blocker_type=blocker_type,
            blocker_reason=blocker_reason,
        )
        flash("Blocker flag updated." if is_blocked else "Blocker cleared.", "info")
        return redirect(url_for("projects.board", project_id=task.project_id))
    except TaskServiceError as exc:
        flash(str(exc), "danger")
        try:
            task = get_tenant_task(task_id, current_user.organization_id)
            return redirect(url_for("projects.board", project_id=task.project_id))
        except TaskServiceError:
            return redirect(url_for("projects.index"))


@board_bp.post("/<int:task_id>/extend")
@login_required
def extend(task_id: int):
    extension_type = (request.form.get("extension_type") or "").strip()
    reason = request.form.get("reason", "")

    try:
        new_due = _parse_iso_date(request.form.get("new_due_date"))
        ext = extend_task_deadline(
            task_id=task_id,
            actor=current_user,
            new_due_date=new_due,
            extension_type=extension_type,
            reason=reason,
        )
        flash(f"Deadline extended to {ext.new_due_date} ({ext.extension_type.replace('_', ' ')}).", "success")
        return redirect(url_for("projects.board", project_id=ext.task.project_id))
    except TaskServiceError as exc:
        flash(str(exc), "danger")
        try:
            task = get_tenant_task(task_id, current_user.organization_id)
            return redirect(url_for("projects.board", project_id=task.project_id))
        except TaskServiceError:
            return redirect(url_for("projects.index"))


@board_bp.post("/<int:task_id>/comment")
@login_required
def comment(task_id: int):
    body = request.form.get("body", "")
    try:
        cmt = add_task_comment(task_id=task_id, actor=current_user, body=body)
        flash("Comment added.", "success")
        return redirect(url_for("projects.board", project_id=cmt.task.project_id))
    except TaskServiceError as exc:
        flash(str(exc), "danger")
        try:
            task = get_tenant_task(task_id, current_user.organization_id)
            return redirect(url_for("projects.board", project_id=task.project_id))
        except TaskServiceError:
            return redirect(url_for("projects.index"))