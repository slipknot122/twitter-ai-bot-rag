import os
from pathlib import Path
from config import settings

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

def is_telegram_configured(session_path_override: str = None) -> bool:
    """
    Checks if Telegram is configured (API ID, API Hash, and session).
    """
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        return False
        
    if settings.telegram_session_string:
        return True
        
    # Check session file using absolute path from project root
    # or override for testing
    project_root = Path(__file__).parent.absolute()
    session_file = session_path_override or (project_root / "bot_session.session")
    
    if isinstance(session_file, str):
        session_file = Path(session_file)
        if not session_file.is_absolute():
            session_file = project_root / session_file
            
    return session_file.exists()

def classify_safe_error(error: Exception) -> str:
    """
    Classifies an exception into a safe, generic error code without leaking secrets or source text.
    Allowed codes: llm_timeout, llm_provider_error, audit_invalid_json, audit_schema_validation,
    revision_failed, candidate_validation, state_conflict, database_error, unknown_error.
    """
    import sqlite3
    import asyncio
    from httpx import TimeoutException as HttpxTimeout
    from litellm.exceptions import Timeout as LitellmTimeout
    from pydantic import ValidationError as PydanticValidationError
    
    if isinstance(error, (asyncio.TimeoutError, HttpxTimeout, LitellmTimeout)):
        return "llm_timeout"
    
    if isinstance(error, ValidationError):
        return "candidate_validation"
        
    if isinstance(error, PydanticValidationError):
        return "audit_schema_validation"
        
    if isinstance(error, sqlite3.Error):
        # We can narrow this down later if needed
        if "UNIQUE constraint" in str(error) or "FOREIGN KEY" in str(error):
             return "state_conflict"
        return "database_error"
        
    error_str = str(error).lower()
    
    # Very coarse mapping for llm provider errors. It's safer to catch explicit types,
    # but litellm wraps many API errors. We just check if it's from LiteLLM.
    if "litellm" in str(type(error)).lower() or "llm" in error_str or "api error" in error_str:
        return "llm_provider_error"
        
    if "json" in error_str and "decode" in error_str:
        return "audit_invalid_json"
        
    return "unknown_error"
