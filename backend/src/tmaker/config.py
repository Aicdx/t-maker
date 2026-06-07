from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:191362688@127.0.0.1:15432/t_maker"
    openai_base_url: str = "https://api.openai.com"
    openai_api_key: str = ""
    openai_model: str = ""
    openai_wire_api: str = "responses"
    openai_reasoning_effort: str | None = None
    openai_disable_response_storage: bool = True
    openai_timeout_seconds: float = 18

    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env", ".env"),
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()
