"""Runtime configuration, read from environment variables.

Every value has a sensible default for local development; production values
come from Cloud Run env vars and Secret Manager (see scripts/bootstrap.sh).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    # Public base URL of the deployed service, e.g. https://calsync-xyz-uc.a.run.app
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8080"))

    # Storage. "gcs" in production, "memory" for tests / local hacking.
    storage_backend: str = field(default_factory=lambda: _env("STORAGE_BACKEND", "memory"))
    gcs_bucket: str = field(default_factory=lambda: _env("GCS_BUCKET", ""))
    gcs_prefix: str = field(default_factory=lambda: _env("GCS_PREFIX", "calsync"))

    # Secrets
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET", "dev-session-secret-change-me"))
    token_encryption_key: str = field(default_factory=lambda: _env("TOKEN_ENCRYPTION_KEY", ""))
    cron_secret: str = field(default_factory=lambda: _env("CRON_SECRET", "dev-cron-secret"))

    # OAuth clients
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    ms_client_id: str = field(default_factory=lambda: _env("MS_CLIENT_ID"))
    ms_client_secret: str = field(default_factory=lambda: _env("MS_CLIENT_SECRET"))
    # "common" allows personal + work/school accounts. Use a tenant id to restrict.
    ms_tenant: str = field(default_factory=lambda: _env("MS_TENANT", "common"))

    # Sync window: how far back / forward we mirror events (days).
    window_past_days: int = field(default_factory=lambda: _env_int("WINDOW_PAST_DAYS", 30))
    window_future_days: int = field(default_factory=lambda: _env_int("WINDOW_FUTURE_DAYS", 365))

    # Local error log file (also mirrored to GCS). Cloud Run's filesystem is
    # ephemeral, which is exactly why GCS is the system of record.
    error_log_path: str = field(default_factory=lambda: _env("ERROR_LOG_PATH", "/tmp/calsync-errors.log"))

    # Allow-list of emails that may log in. Empty = anyone with a valid OAuth
    # login can use the instance. Set this on a personal deployment.
    allowed_emails: tuple[str, ...] = field(
        default_factory=lambda: tuple(e.strip().lower() for e in _env("ALLOWED_EMAILS").split(",") if e.strip())
    )

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.base_url}/auth/google/callback"

    @property
    def ms_redirect_uri(self) -> str:
        return f"{self.base_url}/auth/microsoft/callback"

    @property
    def webhook_url(self) -> str:
        return f"{self.base_url}/webhooks"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Testing hook."""
    global _settings
    _settings = None
