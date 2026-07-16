from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from database import Database
    from llm_provider import LLMProvider
    from media_builder import MediaBuilder
    from semantic_memory import SemanticMemory
    from twitter_publisher import TwitterPublisher


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    api_id: int | None
    api_hash: str | None = field(repr=False)
    session_string: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class GeminiConfig:
    api_key: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class CloudflareConfig:
    account_id: str | None = field(repr=False)
    api_token: str | None = field(repr=False)
    image_model: str


@dataclass(frozen=True, slots=True)
class PollinationsConfig:
    api_key: str | None = field(repr=False)
    image_model: str


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    db_path: str


@dataclass(frozen=True, slots=True)
class LLMConfig:
    openai_api_key: str | None = field(repr=False)
    gemini: GeminiConfig
    model: str
    temperature: float


@dataclass(frozen=True, slots=True)
class TwitterConfig:
    api_key: str | None = field(repr=False)
    api_secret: str | None = field(repr=False)
    access_token: str | None = field(repr=False)
    access_token_secret: str | None = field(repr=False)
    bearer_token: str | None = field(repr=False)
    dry_run: bool


@dataclass(frozen=True, slots=True)
class MediaConfig:
    media_dir: Path
    gemini: GeminiConfig
    cloudflare: CloudflareConfig
    pollinations: PollinationsConfig


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    db: Database = field(repr=False)
    llm: LLMProvider = field(repr=False)
    publisher: TwitterPublisher = field(repr=False)
    media: MediaBuilder = field(repr=False)
    semantic_memory: SemanticMemory = field(repr=False)
