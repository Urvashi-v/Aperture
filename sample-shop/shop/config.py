"""Application configuration, sourced from the environment / .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Runtime configuration for sample-shop.

    Values come from (in increasing precedence): defaults here, the .env file
    at the repository root, then real environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Application -------------------------------------------------------
    aperture_env: str = "local"
    shop_host: str = "0.0.0.0"
    shop_port: int = 8000
    shop_log_level: str = "INFO"
    shop_log_format: Literal["json", "console"] = "json"

    # ---- Database ----------------------------------------------------------
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "shop"
    postgres_user: str = "shop"
    postgres_password: str = "shop_local_dev_password"

    # When set, this wins over the individual POSTGRES_* fields.
    database_url: str | None = None

    # ---- Connection pool ---------------------------------------------------
    # Pathology P5 (pool saturation) is produced by shrinking this while the
    # load test raises concurrency. Left at a healthy value by default.
    db_pool_size: int = Field(default=20, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout_s: float = Field(default=30.0, gt=0)
    db_echo_sql: bool = False

    # ---- Seeding -----------------------------------------------------------
    seed_profile: str = "small"
    seed_random_seed: int = 1337

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        """The async SQLAlchemy URL the application connects with."""
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def asyncpg_dsn(self) -> str:
        """The same target as a plain libpq-style DSN.

        Used by the seeder, which drops to raw asyncpg to use COPY, and by
        Alembic. Deriving it from one place keeps the two from drifting apart.
        """
        return self.sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    def safe_summary(self) -> dict[str, object]:
        """Configuration snapshot with the password removed, for /health/info.

        The connection fields are read back out of the *effective* URL rather
        than off the individual POSTGRES_* settings. When DATABASE_URL is set
        it overrides those fields, and reporting the overridden values would
        tell an operator the process is talking to a database it is not
        actually talking to - which is the worst possible failure mode for a
        diagnostic endpoint.
        """
        url = make_url(self.sqlalchemy_url)
        return {
            "env": self.aperture_env,
            "log_level": self.shop_log_level,
            "log_format": self.shop_log_format,
            "postgres_host": url.host,
            "postgres_port": url.port,
            "postgres_db": url.database,
            "postgres_user": url.username,
            "db_pool_size": self.db_pool_size,
            "db_max_overflow": self.db_max_overflow,
            "db_pool_timeout_s": self.db_pool_timeout_s,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
