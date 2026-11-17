"""Zentrale Anwendungskonfiguration (aus Umgebungsvariablen / .env)."""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="TRADING_", extra="ignore"
    )

    db_host: str = "127.0.0.1"
    db_port: int = 5433
    db_user: str = "trading"
    db_password: str = "trading_dev"
    db_name: str = "trading_experiment"

    gateway_api_key: str = ""
    agent_gateway_api_key: str = ""
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8000

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


class AgentRuntimeSettings(BaseSettings):
    """Agent secrets are injected separately; the shared application .env is ignored."""

    model_config = SettingsConfigDict(
        env_prefix="TRADING_AGENT_", env_file=None, extra="ignore"
    )

    agent_id: str = "agent-a"
    model: str = "openai/gpt-4o-mini"
    openrouter_api_key: SecretStr
    data_api_base_url: str = "http://127.0.0.1:8001"
    data_api_key: SecretStr
    gateway_base_url: str = "http://127.0.0.1:8000"
    gateway_api_key: SecretStr
    monthly_cost_cap_eur: float = Field(default=20.0, gt=0, le=20)
    max_output_tokens: int = Field(default=800, ge=64, le=4096)
    request_timeout_seconds: int = Field(default=30, gt=0, le=120)

    @field_validator("data_api_base_url", "gateway_base_url")
    @classmethod
    def require_loopback_service_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Agent-Dienste müssen auf einer lokalen Loopback-URL liegen")
        return value.rstrip("/")


class DataServiceSettings(BaseSettings):
    """Credentials for the read-only market-data service, not for the agent."""

    model_config = SettingsConfigDict(
        env_prefix="TRADING_DATA_", env_file=None, extra="ignore"
    )

    db_host: str = "127.0.0.1"
    db_port: int = 5433
    db_name: str = "trading_experiment"
    db_user: str = "trading_data_reader"
    db_password: SecretStr
    api_key: SecretStr
    host: str = "127.0.0.1"
    port: int = 8001
    market_data_max_age_seconds: int = 120

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
