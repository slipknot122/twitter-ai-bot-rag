import pytest
import asyncio
import json
from unittest.mock import patch, MagicMock, AsyncMock
from utils import truncate_post_text, validate_post_text, ValidationError, is_telegram_configured
from ai_engine import AIEngine
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


def test_truncate_post_text_prefers_word_boundary():
    text = ("market update " * 30).strip()
    result = truncate_post_text(text, 280)
    assert len(result) <= 280
    assert not result.endswith(" ")
    assert text.startswith(result)
    assert result.endswith("update") or result.endswith("market")


def test_truncate_post_text_without_boundary_uses_exact_limit():
    result = truncate_post_text("я" * 400, 280)
    assert result == "я" * 280


def test_rewriter_runtime_prompt_overrides_legacy_long_form_setting():
    response = json.dumps({
        "action": "PUBLISH",
        "confidence": 0.95,
        "reason": "news",
        "tweet_text": "A concise market update.",
        "image_prompt": "Crypto market screens in a newsroom",
        "sentiment": "Neutral",
        "category": "NEWS",
    })
    with patch("ai_engine.db.get_setting") as get_setting, patch(
        "ai_engine.context_builder.build_context", return_value=""
    ), patch("ai_engine.llm.generate", return_value=response) as generate:
        get_setting.side_effect = lambda key, default: {
            "system_prompt": "X Premium has no limit. Always write a long-form post.",
            "llm_temperature": 0.7,
            "allowed_categories": "NEWS",
        }.get(key, default)
        AIEngine().process_text("Source " * 1000)

    system_prompt = generate.call_args.kwargs["system_prompt"]
    assert "OVERRIDES EARLIER LENGTH INSTRUCTIONS" in system_prompt
    assert "no more than 280 characters total" in system_prompt
    payload = json.loads(generate.call_args.kwargs["prompt"])
    assert payload["original_news"] == "Source " * 1000


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
@patch('ai_worker.auditor')
@patch('ai_worker.db')
@patch('ai_worker.ai_engine')
async def test_ai_worker_publish_goes_to_review(mock_ai_engine, mock_db, mock_auditor):
    # Phase 3 policy: Even if it generates successfully, it goes to review (audit) and stays in review.
    # We test the end state after auditor.
    mock_draft = {"id": 1, "original_text": "text", "status": "processing"}
    mock_db.fetch_next_new_draft.side_effect = [mock_draft, KeyboardInterrupt("Stop loop")]
    mock_db.get_draft.return_value = mock_draft
    
    mock_ai_engine.process_text.return_value = {"action": "GENERATE", "tweet_text": "valid valid valid valid text that passes min length constraints"}
    
    # Mock auditor to pass
    from post_auditor import AuditResult
    mock_audit = AuditResult(
        recommendation="APPROVE",
        overall_score=0.9,
        factual_fidelity=0.9,
        clarity=0.9,
        hook_strength=0.9,
        originality=0.9,
        persona_match=0.9,
        duplicate_risk=0.1,
        spam_risk=0.1,
        policy_risk=0.1,
        blocking_issues=[],
        suggestions=[],
        feedback="Looks good"
    )
    mock_auditor.audit.return_value = (mock_audit, "mock_model")
    mock_auditor.requires_revision.return_value = False
    
    try:
        await ai_worker_loop()
    except KeyboardInterrupt:
        pass
        
    mock_db.complete_ai_processing.assert_called_once()
    assert mock_db.complete_ai_processing.call_args[0][1] == "review"
    kwargs = mock_db.complete_ai_processing.call_args[0][2]
    assert kwargs["audit_status"] == "passed"
    assert kwargs["audit_model"] == "mock_model"

@pytest.mark.anyio
@patch('ai_worker.db')
@patch('ai_worker.ai_engine')
async def test_ai_worker_other_actions(mock_ai_engine, mock_db):
    mock_draft = {"id": 1, "original_text": "text", "status": "processing"}
    mock_db.fetch_next_new_draft.side_effect = [mock_draft, KeyboardInterrupt("Stop loop")]
    mock_db.get_draft.return_value = mock_draft
    
    # FAILED action
    mock_ai_engine.process_text.return_value = {"action": "FAILED", "tweet_text": ""}
    try:
        await ai_worker_loop()
    except KeyboardInterrupt:
        pass
    mock_db.complete_ai_processing.assert_called_once()
    assert mock_db.complete_ai_processing.call_args[0][1] == "failed"

    mock_db.reset_mock()
    mock_db.fetch_next_new_draft.side_effect = [mock_draft, KeyboardInterrupt("Stop loop")]
    mock_db.get_draft.return_value = mock_draft
    
    # IGNORE action
    mock_ai_engine.process_text.return_value = {"action": "IGNORE", "tweet_text": "valid valid valid valid"}
    try:
        await ai_worker_loop()
    except KeyboardInterrupt:
        pass
    mock_db.complete_ai_processing.assert_called_once()
    assert mock_db.complete_ai_processing.call_args[0][1] == "ignored"

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
