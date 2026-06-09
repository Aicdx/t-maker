from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MonitorRuntimeState(BaseModel):
    running: bool = False
    last_tick_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_notified_signal_key: str | None = None
    notification_count: int = Field(default=0, ge=0)
