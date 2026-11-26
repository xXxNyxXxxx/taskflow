import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me-in-production")

    # Deployment Mode: "private" (invite-code required) or "saas" (open org signup)
    APP_MODE = os.environ.get("APP_MODE", "private").strip().lower()
    if APP_MODE not in ("private", "saas"):
        raise ValueError("APP_MODE must be either 'private' or 'saas'")

    # Database
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
    }

    # Session & Cookie Security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # White-Label Logo Upload Hard Limits (1 MB max, raster only)
    MAX_CONTENT_LENGTH = 1 * 1024 * 1024
    ALLOWED_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
    CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")

    # Rate Limiting Storage (in-memory for dev; swap to Redis URI in prod)
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'taskflow_dev.db')}"
    )
    SESSION_COOKIE_SECURE = False


class TestingConfig(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    RATELIMIT_ENABLED = False


def _normalize_db_url(raw_url: str | None) -> str | None:
    if raw_url and raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql://", 1)
    return raw_url


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(os.environ.get("DATABASE_URL"))

    def __init__(self):
        if not os.environ.get("SECRET_KEY"):
            raise RuntimeError("SECRET_KEY must be set in ProductionConfig")
        if not self.SQLALCHEMY_DATABASE_URI:
            raise RuntimeError("DATABASE_URL must be set in ProductionConfig")

CONFIG_MAP = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}