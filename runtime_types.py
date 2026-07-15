from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    api_id: int | None
    api_hash: str | None
    session_string: str | None
