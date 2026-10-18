"""Validated configuration loaded from environment variables and .env."""

import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from aiogram.utils.token import validate_token
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
        case_sensitive=False,
    )

    db_path: Path = Path("data/farm-unit-1.sqlite3")
    llm_api_key: SecretStr | None = None
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    llm_model: str = "gemini-3.8-flash"
    web_base_url: str | None = 'https://novelpulse-books.github.io/novelpulse-library/'
    google_site_verification: str = ''

    @field_validator('google_site_verification', mode='before')
    @classmethod
    def clean_google_site_verification(cls, value):
        return '' if value is None else str(value).strip()

    @field_validator("web_base_url", mode="before")
    @classmethod
    def valid_web_base(cls, value):
        if value is None or not str(value).strip():
            return None
        parsed = urlparse(str(value).strip())
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("WEB_BASE_URL must be an HTTP(S) site URL without credentials/query/fragment")
        return str(value).strip().rstrip('/') + '/'

    @field_validator("llm_base_url")
    @classmethod
    def valid_llm_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("LLM_BASE_URL must be an HTTP(S) API base URL without credentials")
        return value.rstrip("/")

    @field_validator("llm_model")
    @classmethod
    def nonempty_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("LLM_MODEL must not be empty")
        return value.strip()

    @field_validator("llm_api_key", mode="before")
    @classmethod
    def empty_key_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class Settings(PipelineSettings):
    bot_token: SecretStr
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    channel_id: int | str | None = None
    autopilot_interval_hours: float = Field(default=0, ge=0, le=8760, allow_inf_nan=False)
    node_mode: Literal['full', 'bot_24x7', 'youtube_factory'] = 'full'
    catalog_snapshot_path: Path = Path('data/catalog_snapshot.json')
    catalog_snapshot_url: str | None = None
    catalog_snapshot_token: SecretStr | None = None
    retention_interval_hours: float = Field(default=1, gt=0, le=8760, allow_inf_nan=False)
    cloud_failover_enabled: bool = False
    cloud_repo: str = 'mayonezka/novelpulse-night-watch-public'
    cloud_github_token: SecretStr | None = None
    cloud_state_key: SecretStr | None = None
    cloud_role: Literal['pc', 'cloud'] = 'pc'
    autopilot_worker: bool = False
    autopilot_timeout_seconds: int = Field(default=1800,ge=30,le=10800)

    @field_validator('cloud_github_token', 'cloud_state_key', mode='before')
    @classmethod
    def empty_cloud_secret(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator('cloud_repo')
    @classmethod
    def valid_cloud_repo(cls, value):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
            raise ValueError('CLOUD_REPO must be owner/repository')
        return value

    @field_validator('catalog_snapshot_url', mode='before')
    @classmethod
    def valid_snapshot_url(cls, value):
        if value is None or not str(value).strip():
            return None
        parsed = urlparse(str(value).strip())
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError('CATALOG_SNAPSHOT_URL must be a trusted HTTPS URL without embedded credentials')
        return str(value).strip()

    @field_validator("channel_id", mode="before")
    @classmethod
    def parse_channel_id(cls, value: object) -> int | str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        text = str(value).strip()
        if re.fullmatch(r"-100[0-9]+", text):
            return int(text)
        if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", text):
            return text
        raise ValueError("CHANNEL_ID must be @channel_username or a -100... channel ID")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return value

    @field_validator("admin_ids")
    @classmethod
    def positive_admin_ids(cls, value: list[int]) -> list[int]:
        if any(tg_id <= 0 for tg_id in value):
            raise ValueError("ADMIN_IDS must contain positive Telegram user IDs")
        return list(dict.fromkeys(value))

    @field_validator("bot_token")
    @classmethod
    def valid_bot_token(cls, value: SecretStr) -> SecretStr:
        try:
            validate_token(value.get_secret_value())
        except Exception:
            raise ValueError("BOT_TOKEN must be a valid BotFather token") from None
        return value

