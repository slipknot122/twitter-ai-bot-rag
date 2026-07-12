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
    llm_temperature: float = Field(default=0.7)
    
    # Database
    # Telethon Config
    db_path: str = Field(default="bot_database.sqlite")
    
    # App config
    telegram_channels: List[str] = Field(default_factory=list) # e.g. ["test_channel"]

    # --- Media Generation ---
    media_generation_enabled: bool = Field(default=False)
    media_provider_order: str = Field(default="cloudflare,pollinations")
    
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
    media_max_bytes: int = Field(default=10_000_000)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
