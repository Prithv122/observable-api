"""Runtime configuration. Every value here is settable from the environment.

Defaults are the ones used for the load tests in README section 5 -- if you change a
default, the numbers in the README no longer describe the code that is running.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from the environment (or a local .env)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"

    rate_limit_requests: int = Field(default=60, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    rate_limit_enabled: bool = True

    cache_ttl_seconds: int = Field(default=30, ge=1)
    cache_enabled: bool = True

    log_level: str = "INFO"
    log_json: bool = True

    warehouse_path: Path = Path("data/warehouse.duckdb")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings are read once per process; the cache is cleared in tests."""
    return Settings()
