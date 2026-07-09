import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import List

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
    
    # Database
    # Telethon Config
    db_path: str = Field(default="bot_database.sqlite")
    
    # App config
    telegram_channels: List[str] = Field(default_factory=list) # e.g. ["test_channel"]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
