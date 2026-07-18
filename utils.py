import os
from pathlib import Path
from config import settings
from runtime_types import TelegramConfig

class ValidationError(Exception):
    pass

def validate_post_text(text: str, min_length: int = 10, max_length: int = 280) -> str:
    """
    Validates post text for empty/whitespace, min length, max length, and control characters.
    Raises ValidationError if invalid. Returns the stripped text if valid.
    """
    if not text or not text.strip():
        raise ValidationError("Text is empty or contains only whitespace")
        
    cleaned_text = text.strip()
    
    if len(cleaned_text) < min_length:
        raise ValidationError(f"Text is too short (min {min_length} chars)")
        
    if len(cleaned_text) > max_length:
        raise ValidationError(f"Text is too long (max {max_length} chars)")
        
    # Check for invalid control characters (keep newlines and tabs)
    for char in cleaned_text:
        if ord(char) < 32 and char not in ('\n', '\r', '\t'):
            raise ValidationError("Text contains invalid control characters")
            
    return cleaned_text


def truncate_post_text(text: str, max_length: int = 280) -> str:
    """Return stripped text within max_length, preferring a word boundary."""
    cleaned_text = text.strip()
    if len(cleaned_text) <= max_length:
        return cleaned_text

    candidate = cleaned_text[:max_length].rstrip()
    boundary = candidate.rfind(" ")
    if boundary > 0:
        candidate = candidate[:boundary].rstrip()
    return candidate


def _normalize_legacy_api_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None

def is_telegram_configured_from(config: TelegramConfig, *, session_path_override: str | Path | None = None) -> bool:
    """
    Explicit-input helper without environment or network access.
    Uses only explicit inputs; may inspect an explicitly supplied path.
    """
    api_id = config.api_id
    if api_id is None or api_id <= 0:
        return False
        
    api_hash = config.api_hash.strip() if config.api_hash else None
    if not api_hash:
        return False
        
    session_string = config.session_string.strip() if config.session_string else None
    if session_string:
        return True
        
    if session_path_override is not None:
        session_file = Path(session_path_override)
        return session_file.is_file()
        
    return False

def is_telegram_configured(session_path_override: str | Path | None = None) -> bool:
    """
    Checks if Telegram is configured (API ID, API Hash, and session).
    Legacy wrapper around is_telegram_configured_from.
    """
    config = TelegramConfig(
        api_id=_normalize_legacy_api_id(settings.telegram_api_id),
        api_hash=settings.telegram_api_hash,
        session_string=settings.telegram_session_string,
    )
        
    # Check session file using absolute path from project root
    # or override for testing
    project_root = Path(__file__).parent.absolute()
    session_file = session_path_override or (project_root / "bot_session.session")
    
    if isinstance(session_file, str):
        session_file = Path(session_file)
        if not session_file.is_absolute():
            session_file = project_root / session_file
            
    return is_telegram_configured_from(config, session_path_override=session_file)

def classify_safe_error(error: Exception) -> str:
    """
    Classifies an exception into a safe, generic error code without leaking secrets or source text.
    Allowed codes: llm_timeout, llm_provider_error, audit_invalid_json, audit_schema_validation,
    revision_failed, candidate_validation, state_conflict, database_error, unknown_error.
    """
    import sqlite3
    import asyncio
    import json
    from httpx import TimeoutException as HttpxTimeout
    from litellm.exceptions import Timeout as LitellmTimeout
    from litellm.exceptions import APIError, AuthenticationError, APIConnectionError, RateLimitError
    from pydantic import ValidationError as PydanticValidationError
    
    if isinstance(error, (asyncio.TimeoutError, HttpxTimeout, LitellmTimeout)):
        return "llm_timeout"
    
    if isinstance(error, ValidationError):
        return "candidate_validation"
        
    if isinstance(error, PydanticValidationError):
        return "audit_schema_validation"
        
    if isinstance(error, json.JSONDecodeError):
        return "audit_invalid_json"

    # From our post_auditor.py
    if type(error).__name__ == "AuditFailure":
        # Check if the code is known
        if hasattr(error, 'code') and error.code:
            return f"audit_{error.code}"
        return "audit_failure"
        
    if isinstance(error, (APIError, AuthenticationError, APIConnectionError, RateLimitError)):
        return "llm_provider_error"
        
    if isinstance(error, sqlite3.Error):
        # We can narrow this down if needed
        if "UNIQUE constraint" in str(error) or "FOREIGN KEY" in str(error):
             return "state_conflict"
        return "database_error"
        
    return "unknown_error"
