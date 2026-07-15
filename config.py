import os
import types
from dataclasses import dataclass
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import List

@dataclass(frozen=True)
class ConfigIssue:
    field: str
    code: str

class ConfigStartupError(RuntimeError):
    def __init__(self, issues: tuple[ConfigIssue, ...]):
        if not issues:
            issues = (ConfigIssue("configuration", "configuration_error"),)
        self.issues = issues
        msg_parts = [f"- {i.field}: {i.code}" for i in issues]
        self._safe_msg = "Config validation failed with following issues:\n" + "\n".join(msg_parts)
        super().__init__(self._safe_msg)

    def __repr__(self):
        return f"ConfigStartupError({len(self.issues)} issues)"

    def __str__(self):
        return self._safe_msg

ERROR_TYPE_MAP = types.MappingProxyType({
    "missing": "missing",
    "int_parsing": "invalid_type",
    "float_parsing": "invalid_type",
    "string_type": "invalid_type",
    "bool_parsing": "invalid_type",
    "list_type": "invalid_type",
    "dict_type": "invalid_type",
    "value_error": "invalid_value",
})

def _sanitize_validation_errors(errors: list[dict], valid_fields: set[str]) -> tuple[ConfigIssue, ...]:
    safe_issues = []

    for err in errors:
        loc = err.get("loc", [])
        if loc and isinstance(loc[0], str) and loc[0] in valid_fields:
            field = loc[0]
        else:
            field = "configuration"

        raw_type = err.get("type", "unknown")
        code = ERROR_TYPE_MAP.get(raw_type, "configuration_error")

        issue = ConfigIssue(field=field, code=code)
        if issue not in safe_issues:
            safe_issues.append(issue)

    if not safe_issues:
        safe_issues.append(ConfigIssue(field="configuration", code="configuration_error"))

    safe_issues.sort(key=lambda i: (i.field, i.code))
    return tuple(safe_issues)

def load_settings(env_file: str | None = None) -> "Settings":
    from pydantic import ValidationError
    try:
        return Settings(_env_file=env_file)
    except ValidationError as e:
        valid_fields = set(Settings.model_fields.keys())
        issues = _sanitize_validation_errors(e.errors(), valid_fields)
        raise ConfigStartupError(issues) from None

class Settings(BaseSettings):
    # Telegram
    telegram_api_id: int = Field(default=0)
    telegram_api_hash: str = Field(default="")
    telegram_session_string: str = Field(default="")
    
    # Twitter
    twitter_api_key: str = Field(default="")
    twitter_api_secret: str = Field(default="")
    twitter_access_token: str = Field(default="")
    twitter_access_token_secret: str = Field(default="")
    twitter_bearer_token: str = Field(default="")
    twitter_dry_run: bool = Field(default=True)
    
    # AI / LLM
    openai_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")
    llm_model: str = Field(default="gemini/gemini-3.5-flash")
    llm_temperature: float = Field(default=0.7)
    
    # Database
    # Telethon Config
    db_path: str = Field(default="bot_database.sqlite")
    
    # App config
    telegram_channels: List[str] = Field(default_factory=list) # e.g. ["test_channel"]
    web_admin_host: str = Field(default="127.0.0.1")

    # --- Media Generation ---
    media_generation_enabled: bool = Field(default=False)
    media_provider_order: str = Field(default="google,cloudflare,pollinations")
    
    # Cloudflare Workers AI
    cloudflare_account_id: str = Field(default="")
    cloudflare_api_token: str = Field(default="")
    cloudflare_image_model: str = Field(default="@cf/black-forest-labs/flux-1-schnell")
    
    # Pollinations (fallback)
    pollinations_api_key: str = Field(default="")
    pollinations_image_model: str = Field(default="flux")
    
    # Hugging Face (prepared, not connected in V1)
    hf_token: str = Field(default="")
    hf_image_model: str = Field(default="black-forest-labs/FLUX.1-schnell")
    
    # Image settings
    media_image_width: int = Field(default=1200)
    media_image_height: int = Field(default=675)
    media_generation_timeout: int = Field(default=60)
    media_worker_timeout_seconds: int = Field(default=540)
    media_lease_seconds: int = Field(default=600)
    media_max_bytes: int = Field(default=10_000_000)

    from pydantic import model_validator

    @model_validator(mode='after')
    def validate_timeouts(self):
        if not (self.media_lease_seconds >= self.media_worker_timeout_seconds + 30 and self.media_worker_timeout_seconds >= self.media_generation_timeout + 60):
            raise ValueError("Invalid timeout settings: lease > worker > provider with margins")
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
