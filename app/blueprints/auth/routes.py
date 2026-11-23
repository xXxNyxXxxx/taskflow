from urllib.parse import urlsplit
from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.blueprints.auth import auth_bp
from app.blueprints.auth.forms import LoginForm, SignupForm
from app.extensions import limiter
from app.services.tenant_service import (
    TenantServiceError,
    authenticate_user,
    register_new_organization,
    register_via_invite,
)


def is_safe_redirect(target: str | None) -> bool:
    if not target:
        return False
    parsed = urlsplit(target)
    return parsed.scheme == "" and parsed.netloc == "" and target.startswith("/")


@auth_bp.route("/")
@login_required
def dashboard():
    """Tenant-scoped landing dashboard (expanded with live metrics in Phases 3-5)."""
    org = current_user.organization
    return render_template("auth/dashboard.html", org=org)


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("auth.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        try:
            user = authenticate_user(form.email.data, form.password.data)
            login_user(user, remember=form.remember_me.data)
            next_page = request.args.get("next")
            if is_safe_redirect(next_page):
                return redirect(next_page)
            return redirect(url_for("auth.dashboard"))
        except TenantServiceError as exc:
            flash(str(exc), "danger")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/signup", methods=["GET", "POST"])
@limiter.limit("6 per minute", methods=["POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("auth.dashboard"))

    app_mode = current_app.config["APP_MODE"]
    form = SignupForm()

    # In private mode, lock registration strictly to invite codes
    if app_mode == "private":
        form.signup_mode.choices = [("invite", "Join with Invite Code")]
        form.signup_mode.data = "invite"

    if form.validate_on_submit():
        try:
            if app_mode == "saas" and form.signup_mode.data == "new_org":
                if not form.org_name.data:
                    raise TenantServiceError("Workspace name is required to create an organization.")
                user = register_new_organization(
                    org_name=form.org_name.data,
                    full_name=form.full_name.data,
                    email=form.email.data,
                    password=form.password.data,
                )
                flash("Workspace created! You are signed in as Super Admin.", "success")
            else:
                user = register_via_invite(
                    full_name=form.full_name.data,
                    email=form.email.data,
                    password=form.password.data,
                    raw_invite_code=form.invite_code.data or "",
                )
                flash(f"Welcome to {user.organization.settings.effective_brand_name}!", "success")

            login_user(user)
            return redirect(url_for("auth.dashboard"))
        except TenantServiceError as exc:
            flash(str(exc), "danger")

    return render_template("auth/signup.html", form=form, app_mode=app_mode)


@auth_bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))