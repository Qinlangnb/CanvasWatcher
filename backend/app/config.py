from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:////data/academic_watcher.db"
    canvas_base_url: str = ""
    canvas_credential_id: str = "canvas"
    canvas_access_token: SecretStr = SecretStr("")
    canvas_max_concurrency: int = Field(default=3, ge=1, le=10)
    llm_provider: str = "disabled"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    ntfy_url: str = ""
    ntfy_topic: str = ""
    download_root: Path = Path("/data/downloads")
    courses_config: Path = Path("/config/courses.yaml")
    file_rules_config: Path = Path("/config/file_rules.yaml")
    availability_config: Path = Path("/config/availability.yaml")
    daily_study_capacity_minutes: int = Field(default=180, ge=30, le=1440)
    log_level: str = "INFO"
    scheduler_enabled: bool = True
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    frontend_url: str = "http://localhost:8080"
    auth_broker_port: int = Field(default=8765, ge=1024, le=65535)
    canvas_oauth_client_id: str = ""
    canvas_oauth_client_secret: SecretStr = SecretStr("")
    canvas_oauth_redirect_uri: str = "http://127.0.0.1:8000/api/auth/canvas/oauth/callback"
    google_calendar_client_id: str = ""
    google_calendar_client_secret: SecretStr = SecretStr("")
    google_calendar_redirect_uri: str = (
        "http://localhost:8000/api/integrations/google-calendar/oauth/callback"
    )
    google_token_encryption_key: SecretStr = SecretStr("")
    calendar_sync_interval_minutes: int = Field(default=5, ge=1, le=1440)
    ics_max_file_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    ics_max_events: int = Field(default=10_000, ge=1, le=100_000)
    ics_expansion_past_days: int = Field(default=366, ge=0, le=3660)
    ics_expansion_future_days: int = Field(default=730, ge=30, le=3660)

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def legacy_canvas_token(self) -> str:
        """Compatibility path for the original CANVAS_ACCESS_TOKEN setting."""
        return self.canvas_access_token.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
