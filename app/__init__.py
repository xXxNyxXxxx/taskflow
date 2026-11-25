import re
import sqlite3
from flask import Flask, flash, redirect, url_for
from flask_login import current_user, logout_user
from sqlalchemy import event
from sqlalchemy.engine import Engine


from config import CONFIG_MAP
from app.extensions import csrf, db, limiter, login_manager, migrate

# Curated low-arousal accent map (ADHD/OCD-safe: zero neon, WCAG-checked)
THEME_ACCENT_PRESETS = {
    "calm_sage": {"light": "#5B7C8D", "dark": "#7697A8"},
    "soft_terracotta": {"light": "#9E6B5B", "dark": "#B58474"},
    "dusty_lavender": {"light": "#756A86", "dark": "#9287A3"},
    "forest_moss": {"light": "#5A7765", "dark": "#769682"},
}

SAFE_HEX_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


@event.listens_for(Engine, "connect")
def enforce_sqlite_foreign_keys(dbapi_connection, connection_record):
    """Forces SQLite to honor ON DELETE CASCADE / RESTRICT like PostgreSQL."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()


def create_app(config_name: str = "development") -> Flask:
    app = Flask(__name__)

    config_cls = CONFIG_MAP.get(config_name, CONFIG_MAP["development"])
    app.config.from_object(config_cls() if config_name == "production" else config_cls)

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # Register models so Alembic/SQLAlchemy sees all 11 tables
    from app import models  # noqa: F401
    from app.models import User

    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to access your workspace."
    login_manager.login_message_category = "info"

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        """Rejects deactivated users or suspended tenants at session load."""
        if not user_id or not user_id.isdigit():
            return None
        user = db.session.get(User, int(user_id))
        if user is None or not user.is_active:
            return None
        if user.organization is None or not user.organization.is_active:
            return None
        return user

    @app.before_request
    def enforce_active_session_guard():
        """Double-checks mid-session deactivation so removed users are evicted immediately."""
        if current_user.is_authenticated:
            if not current_user.is_active or not current_user.organization.is_active:
                logout_user()
                flash("Your workspace access has been deactivated.", "warning")
                return redirect(url_for("auth.login"))

    @app.context_processor
    def inject_tenant_branding():
        from flask_login import current_user
        from app.models import AuditLog

        pending_delete_count = 0
        if getattr(current_user, "is_authenticated", False) and current_user.organization:
            org = current_user.organization
            settings = org.settings
            if current_user.is_super_admin:
                pending_delete_count = AuditLog.query.filter(
                    AuditLog.organization_id == org.id,
                    AuditLog.action == "project_delete_requested",
                    AuditLog.project_id.isnot(None),
                ).count()
            return {
                "current_org": org,
                "org_settings": settings,
                "brand_name": org.name,
                "brand_logo_url": settings.logo_url if settings else None,
                "accent_hex": settings.custom_accent_hex if settings else "#4A6B5D",
                "pending_delete_count": pending_delete_count,
            }
        return {
            "current_org": None,
            "org_settings": None,
            "brand_name": "TaskFlow",
            "brand_logo_url": None,
            "accent_hex": "#4A6B5D",
            "pending_delete_count": 0,
        }

    @app.after_request
    def set_security_headers(response):
        """Hardens responses against clickjacking, MIME sniffing, and referrer leaks."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    # Register Blueprints inside the Application Factory
    from app.blueprints.admin import admin_bp
    from app.blueprints.auth import auth_bp
    from app.blueprints.board import board_bp
    from app.blueprints.projects import projects_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(board_bp)

    @app.get("/health")
    def health_check():
        return {"status": "ok", "app_mode": app.config["APP_MODE"]}, 200

    # Register CLI commands
    from app.cli import register_cli_commands

    register_cli_commands(app)

    return app