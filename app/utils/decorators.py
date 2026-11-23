from functools import wraps
from flask import abort, flash, redirect, url_for
from flask_login import current_user, login_required


def role_required(*allowed_roles: str):
    """Enforces authenticated, active workspace membership with an allowed role."""
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapped_view(*args, **kwargs):
            if not current_user.is_active or not current_user.organization.is_active:
                abort(403)
            if current_user.role not in allowed_roles:
                flash("You do not have permission to access that resource.", "warning")
                return redirect(url_for("auth.dashboard"))
            return view_func(*args, **kwargs)

        return wrapped_view

    return decorator