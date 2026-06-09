from __future__ import annotations

from pathlib import Path

from pydantic import Field
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
    monitor_auto_start: bool = False
    monitor_interval_seconds: float = Field(default=30, gt=0)
    monitor_min_ai_confidence: float = Field(default=0.6, ge=0, le=1)
    monitor_notify_hold: bool = False
    monitor_notify_suspected: bool = True
    monitor_dedup_window_minutes: int = Field(default=240, gt=0)
    codex_analysis_enabled: bool = True
    feishu_webhook_url: str = ""
    feishu_timeout_seconds: float = Field(default=8, gt=0)

    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env", ".env"),
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()
