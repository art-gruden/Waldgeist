"""Configuration: secrets from the environment (.env), game balance from config/game.yaml."""

from __future__ import annotations

import re
from datetime import time
from functools import cached_property
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_hhmm(value: str | time) -> time:
    if isinstance(value, time):
        return value
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not match:
        raise ValueError(f"expected HH:MM, got {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


class Settings(BaseSettings):
    """Runtime settings. Secrets never live in the code or in git — only in .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    database_url: str = Field(description="SQLAlchemy async URL, e.g. postgresql+asyncpg://ec:pass@db:5432/enchanted")
    group_chat_id: int = 0
    admin_tg_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    game_config_path: Path = Path("config/game.yaml")
    content_dir: Path = Path("content/uk")
    assets_dir: Path = Path("assets/images")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    heartbeat_file: Path = Path("/tmp/ec-heartbeat")

    @field_validator("group_chat_id", mode="before")
    @classmethod
    def _empty_group(cls, value: object) -> object:
        return 0 if value in ("", None) else value

    @field_validator("admin_tg_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in re.split(r"[,;\s]+", value.strip()) if part]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("admin_tg_ids")
    @classmethod
    def _positive_ids(cls, value: list[int]) -> list[int]:
        for tg_id in value:
            if tg_id <= 0:
                raise ValueError(f"admin Telegram IDs must be positive user IDs, got {tg_id}")
        return value


# ---------------------------------------------------------------- game.yaml


class MorningConfig(BaseModel):
    window_start: time
    window_end: time
    card_time: time
    summary_time: time
    energy: int = Field(gt=0)
    mode: str = "greeting_only"
    counted_reaction: str = "🥰"
    invite_reaction: str = "🕊"
    card_scene: str = "heart"
    greeting_patterns: list[str]

    @field_validator("window_start", "window_end", "card_time", "summary_time", mode="before")
    @classmethod
    def _times(cls, value: object) -> time:
        return _parse_hhmm(value)  # type: ignore[arg-type]

    @field_validator("mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        if value not in {"greeting_only", "any_message"}:
            raise ValueError("morning.mode must be greeting_only or any_message")
        return value

    @model_validator(mode="after")
    def _window(self) -> MorningConfig:
        if self.window_start >= self.window_end:
            raise ValueError("morning.window_start must be before window_end")
        for pattern in self.greeting_patterns:
            re.compile(pattern)
        return self


class LevelsConfig(BaseModel):
    thresholds: list[int]
    stage_starts: list[int]

    @model_validator(mode="after")
    def _check(self) -> LevelsConfig:
        if not self.thresholds or self.thresholds[0] != 0:
            raise ValueError("levels.thresholds must start with 0")
        if any(b <= a for a, b in zip(self.thresholds, self.thresholds[1:], strict=False)):
            raise ValueError("levels.thresholds must be strictly increasing")
        if not self.stage_starts or self.stage_starts[0] != 1:
            raise ValueError("levels.stage_starts must start with 1")
        if any(b <= a for a, b in zip(self.stage_starts, self.stage_starts[1:], strict=False)):
            raise ValueError("levels.stage_starts must be strictly increasing")
        if self.stage_starts[-1] > len(self.thresholds):
            raise ValueError("levels.stage_starts refers to a level that does not exist")
        return self


class SleepConfig(BaseModel):
    dozing_after_days: int = Field(gt=0)
    sleeping_after_days: int
    dreaming_after_days: int
    reminder_after_days: int = Field(gt=0)
    job_time: time

    @field_validator("job_time", mode="before")
    @classmethod
    def _times(cls, value: object) -> time:
        return _parse_hhmm(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def _order(self) -> SleepConfig:
        if not self.dozing_after_days < self.sleeping_after_days < self.dreaming_after_days:
            raise ValueError("sleep thresholds must increase: dozing < sleeping < dreaming")
        return self


class ClassesConfig(BaseModel):
    live_energy: int = Field(gt=0)
    recording_energy: int = Field(gt=0)
    recording_valid_days: int = Field(gt=0)
    announce_before_hours: list[int]
    thanks_time: time
    max_failed_attempts: int = Field(gt=0)
    lockout_minutes: int = Field(gt=0)

    @field_validator("thanks_time", mode="before")
    @classmethod
    def _times(cls, value: object) -> time:
        return _parse_hhmm(value)  # type: ignore[arg-type]


class VisitsConfig(BaseModel):
    per_day: int = Field(gt=0)
    host_energy: int = Field(ge=0)
    guest_energy: int = Field(ge=0)
    away_minutes: int = Field(ge=0)
    oracle_owner_tg_id: int = 0
    affirmations_file: Path = Path("content/uk/affirmations.yaml")


class RegistrationConfig(BaseModel):
    name_min_length: int = 2
    name_max_length: int = 24


class CommunityConfig(BaseModel):
    active_days: int = 7


class GameConfig(BaseModel):
    timezone: str
    morning: MorningConfig
    levels: LevelsConfig
    sleep: SleepConfig
    classes: ClassesConfig
    visits: VisitsConfig
    registration: RegistrationConfig = RegistrationConfig()
    community: CommunityConfig = CommunityConfig()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        ZoneInfo(value)  # raises for an unknown zone
        return value

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def load(cls, path: Path) -> GameConfig:
        with path.open(encoding="utf-8") as fh:
            return cls.model_validate(yaml.safe_load(fh))
