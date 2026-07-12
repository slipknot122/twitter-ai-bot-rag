import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from utils import validate_post_text, ValidationError, is_telegram_configured
from ai_worker import ai_worker_loop
from media_builder import GoogleImagenProvider, CloudflareProvider, ContentRejectionError, TransientMediaError, ProviderAuthError

# --- Text Validation Tests ---

def test_text_validation_valid():
    text = "This is a valid tweet text that is long enough."
    assert validate_post_text(text) == text

def test_text_validation_empty_whitespace():
    with pytest.raises(ValidationError):
        validate_post_text("")
    with pytest.raises(ValidationError):
        validate_post_text("   \n  ")
    with pytest.raises(ValidationError):
        validate_post_text(None)

def test_text_validation_min_length():
    with pytest.raises(ValidationError):
        validate_post_text("Short")
        
def test_text_validation_max_length():
    long_text = "a" * 281
    with pytest.raises(ValidationError):
        validate_post_text(long_text, max_length=280)
        
def test_text_validation_control_chars():
    # \x00 is a control char
    with pytest.raises(ValidationError):
        validate_post_text("Valid text but with \x00 null byte")
    # Newlines should be valid
    assert validate_post_text("Valid text\nwith newline") == "Valid text\nwith newline"

# --- Telegram Status Detection Tests ---

@patch('utils.settings')
def test_telegram_configured_with_string_session(mock_settings):
    mock_settings.telegram_api_id = "123"
    mock_settings.telegram_api_hash = "abc"
    mock_settings.telegram_session_string = "session_string_data"
    assert is_telegram_configured() is True

@patch('utils.settings')
def test_telegram_configured_with_file(mock_settings, tmp_path):
    mock_settings.telegram_api_id = "123"
    mock_settings.telegram_api_hash = "abc"
    mock_settings.telegram_session_string = ""
    
    # Create a dummy session file in tmp_path
    session_file = tmp_path / "bot_session.session"
    session_file.write_text("dummy")
    
    # Assert using override
    assert is_telegram_configured(session_path_override=session_file) is True

@patch('utils.settings')
def test_telegram_configured_no_credentials(mock_settings, tmp_path):
    # No credentials at all
    mock_settings.telegram_api_id = ""
    mock_settings.telegram_api_hash = ""
    assert is_telegram_configured() is False
    
    # Missing session file
    mock_settings.telegram_api_id = "123"
    mock_settings.telegram_api_hash = "abc"
    mock_settings.telegram_session_string = ""
    
    session_file = tmp_path / "not_exist.session"
    assert is_telegram_configured(session_path_override=session_file) is False

# --- Shadow Mode Tests ---

@pytest.mark.anyio
@patch('ai_worker.db')
@patch('ai_worker.ai_engine')
async def test_shadow_mode_publish(mock_ai_engine, mock_db):
    mock_draft = {"id": 1, "original_text": "text", "status": "processing"}
    mock_db.fetch_next_new_draft.side_effect = [mock_draft, KeyboardInterrupt("Stop loop")]
    mock_ai_engine.process_text.return_value = {"action": "PUBLISH", "tweet_text": "valid valid valid valid"}
    
    # TEST 1: shadow_mode = True -> review
    mock_db.get_setting.return_value = True
    try:
        await ai_worker_loop()
    except KeyboardInterrupt:
        pass
    mock_db.update_draft_status.assert_not_called()
    # verify that the update execute is called with "review"
    args = mock_db._get_connection.return_value.__enter__.return_value.cursor.return_value.execute.call_args[0]
    assert args[1][3] == "review" # new_status
    
    # TEST 2: shadow_mode = False -> approved
    mock_db.reset_mock()
    mock_db.get_setting.return_value = False
    mock_db.fetch_next_new_draft.side_effect = [mock_draft, KeyboardInterrupt("Stop loop")]
    try:
        await ai_worker_loop()
    except KeyboardInterrupt:
        pass
    args = mock_db._get_connection.return_value.__enter__.return_value.cursor.return_value.execute.call_args[0]
    assert args[1][3] == "approved" # new_status

@pytest.mark.anyio
@patch('ai_worker.db')
@patch('ai_worker.ai_engine')
async def test_shadow_mode_other_actions(mock_ai_engine, mock_db):
    mock_draft = {"id": 1, "original_text": "text", "status": "processing"}
    mock_db.fetch_next_new_draft.side_effect = [mock_draft, KeyboardInterrupt("Stop loop")]
    mock_db.get_setting.return_value = False # shadow_mode is False
    
    # REVIEW action
    mock_ai_engine.process_text.return_value = {"action": "REVIEW", "tweet_text": "valid valid valid valid"}
    try:
        await ai_worker_loop()
    except KeyboardInterrupt:
        pass
    args = mock_db._get_connection.return_value.__enter__.return_value.cursor.return_value.execute.call_args[0]
    assert args[1][3] == "review"

    mock_db.reset_mock()
    mock_db.fetch_next_new_draft.side_effect = [mock_draft, KeyboardInterrupt("Stop loop")]
    mock_db.get_setting.return_value = False
    
    # IGNORE action
    mock_ai_engine.process_text.return_value = {"action": "IGNORE", "tweet_text": "valid valid valid valid"}
    try:
        await ai_worker_loop()
    except KeyboardInterrupt:
        pass
    args = mock_db._get_connection.return_value.__enter__.return_value.cursor.return_value.execute.call_args[0]
    assert args[1][3] == "ignored"

# --- Media Fallback HTTP 400 Tests ---

@patch('media_builder.requests.post')
def test_cloudflare_400_policy_violation(mock_post):
    provider = CloudflareProvider()
    provider.account_id = "123"
    provider.api_token = "abc"
    
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Error: content policy violation"
    mock_post.return_value = mock_resp
    
    with pytest.raises(ContentRejectionError):
        provider.generate("prompt", None, 512, 512, 10)

@patch('media_builder.requests.post')
def test_cloudflare_400_unsupported_content_type(mock_post):
    provider = CloudflareProvider()
    provider.account_id = "123"
    provider.api_token = "abc"
    
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Error: unsupported content-type"
    mock_post.return_value = mock_resp
    
    # Should raise TransientMediaError to allow fallback
    with pytest.raises(TransientMediaError):
        provider.generate("prompt", None, 512, 512, 10)
