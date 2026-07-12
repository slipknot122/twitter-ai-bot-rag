import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
import os
from pydantic import ValidationError

from ai_worker import ai_worker_loop
from llm_provider import LLMProvider
from media_builder import MediaBuilder, ProviderAuthError, ContentRejectionError, TransientMediaError
from web_admin.main import SettingsRequest

def test_settings_validation():
    # Valid
    req = SettingsRequest(
        system_prompt="Test",
        shadow_mode=True,
        publish_delay_minutes=60,
        publish_jitter_percent=10,
        max_retries=3,
        scheduler_check_interval_seconds=60,
        image_overlay="none",
        allowed_categories="NEWS"
    )
    assert req.publish_delay_minutes == 60

    # Invalid (negative delay)
    with pytest.raises(ValidationError):
        SettingsRequest(
            system_prompt="Test",
            shadow_mode=True,
            publish_delay_minutes=-5,
            publish_jitter_percent=10,
            max_retries=3,
            scheduler_check_interval_seconds=60,
            image_overlay="none",
            allowed_categories="NEWS"
        )

    # Invalid (jitter > 100)
    with pytest.raises(ValidationError):
        SettingsRequest(
            system_prompt="Test",
            shadow_mode=True,
            publish_delay_minutes=60,
            publish_jitter_percent=150,
            max_retries=3,
            scheduler_check_interval_seconds=60,
            image_overlay="none",
            allowed_categories="NEWS"
        )

def test_media_builder_error_handling():
    builder = MediaBuilder()
    
    mock_provider1 = MagicMock()
    mock_provider1.name = "provider1"
    mock_provider1.is_configured.return_value = True
    
    mock_provider2 = MagicMock()
    mock_provider2.name = "provider2"
    mock_provider2.is_configured.return_value = True
    mock_provider2.generate.return_value = b"image_data"
    
    # Test 1: ContentRejectionError should break cascade
    mock_provider1.generate.side_effect = ContentRejectionError("Moderation failed")
    builder._providers = {"provider1": mock_provider1, "provider2": mock_provider2}
    
    with patch('media_builder.settings') as mock_settings:
        mock_settings.media_generation_enabled = True
        mock_settings.media_provider_order = "provider1, provider2"
        mock_settings.media_width = 1024
        mock_settings.media_height = 1024
        mock_settings.media_timeout_seconds = 30
        
        result1 = builder.generate(1, "test prompt")
        assert result1 is None
        assert mock_provider1.generate.called
        assert not mock_provider2.generate.called

        # Test 2: ProviderAuthError should continue cascade
        mock_provider1.generate.reset_mock()
        mock_provider2.generate.reset_mock()
        mock_provider1.generate.side_effect = ProviderAuthError("Auth failed")
        
        with patch('media_builder.db.get_setting') as mock_db, \
             patch('media_builder._validate_and_save_image') as mock_val:
            mock_db.return_value = "none"
            mock_val.return_value = {"width": 1024, "height": 1024, "size_bytes": 100, "mime_type": "image/jpeg"}
            result2 = builder.generate(2, "test prompt")
        
        assert result2 is not None
        assert mock_provider1.generate.called
        assert mock_provider2.generate.called
    


@patch('llm_provider.settings')
def test_temperature_passed_to_llm(mock_settings):
    mock_settings.llm_temperature = 0.5
    mock_settings.llm_model = "gemini/gemini-1.5-flash"
    mock_settings.openai_api_key = "fake_key"
    mock_settings.gemini_api_key = "fake_key"
    
    with patch.dict('os.environ', {}, clear=True):
        provider = LLMProvider()
        
        # Normally this calls litellm.completion. Let's patch it.
        with patch('llm_provider.completion') as mock_completion:
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = "response"
            mock_completion.return_value = mock_resp
            
            # Pass custom temperature
            provider.generate("prompt", temperature=0.9)
            mock_completion.assert_called_once()
            _, kwargs = mock_completion.call_args
            assert kwargs['temperature'] == 0.9
            
            # Do not pass custom temperature
            mock_completion.reset_mock()
            provider.generate("prompt")
            mock_completion.assert_called_once()
            _, kwargs = mock_completion.call_args
            assert kwargs['temperature'] == 0.5

