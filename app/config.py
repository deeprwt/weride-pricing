"""Environment configuration. Fail-fast on misconfiguration."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of truth for env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: str = Field(default="development", validation_alias="NODE_ENV")
    port: int = Field(default=5000, validation_alias="PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    # Postgres (read-only for pricing — surge + zone data).
    database_url: str = Field(
        default="postgresql://uride:uride@localhost:5432/uride",
        validation_alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379", validation_alias="REDIS_URL")

    # Observability
    sentry_dsn: str | None = Field(default=None, validation_alias="SENTRY_DSN")
    otel_endpoint: str | None = Field(
        default=None, validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_service_name: str = Field(
        default="uride-pricing", validation_alias="OTEL_SERVICE_NAME"
    )


_cached: Settings | None = None


def get_settings() -> Settings:
    """Memoized accessor — Settings() reads env once."""

    global _cached
    if _cached is None:
        _cached = Settings()
    return _cached
