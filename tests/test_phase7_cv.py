import os
from pathlib import Path
from unittest.mock import patch

import pytest

from config import (
    load_settings,
    ConfigStartupError,
    ConfigIssue,
    Settings,
    _sanitize_validation_errors,
)
from runtime_types import TelegramConfig
from utils import (
    is_telegram_configured_from,
    is_telegram_configured,
    _normalize_legacy_api_id,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate tests from existing environment variables."""
    monkeypatch.delenv("WEB_ADMIN_HOST", raising=False)
    monkeypatch.delenv("MEDIA_LEASE_SECONDS", raising=False)
    monkeypatch.delenv("MEDIA_WORKER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MEDIA_GENERATION_TIMEOUT", raising=False)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("UNKNOWN_RANDOM_FIELD", raising=False)

def test_cv_explicit_env_file_none_ignores_temp_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WEB_ADMIN_HOST=9.9.9.9\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    s = load_settings(env_file=None)
    assert s.web_admin_host == "127.0.0.1"

def test_cv_explicit_path_reads_env_file(tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("WEB_ADMIN_HOST=8.8.8.8\n", encoding="utf-8")

    s = load_settings(env_file=str(env_file))
    assert s.web_admin_host == "8.8.8.8"

def test_cv_process_env_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / "custom.env"
    env_file.write_text("WEB_ADMIN_HOST=1.1.1.1\n", encoding="utf-8")
    monkeypatch.setenv("WEB_ADMIN_HOST", "2.2.2.2")

    s = load_settings(env_file=str(env_file))
    assert s.web_admin_host == "2.2.2.2"

def test_cv_malformed_value_sanitized_code(monkeypatch):
    monkeypatch.setenv("MEDIA_LEASE_SECONDS", "not_a_number")
    with pytest.raises(ConfigStartupError) as exc:
        load_settings(env_file=None)

    issues = exc.value.issues
    assert len(issues) == 1
    assert issues[0] == ConfigIssue(field="media_lease_seconds", code="invalid_type")

def test_cv_cross_field_validator_sanitized(monkeypatch):
    # Violates lease >= worker + 30
    monkeypatch.setenv("MEDIA_LEASE_SECONDS", "10")
    monkeypatch.setenv("MEDIA_WORKER_TIMEOUT_SECONDS", "100")
    monkeypatch.setenv("MEDIA_GENERATION_TIMEOUT", "60")

    with pytest.raises(ConfigStartupError) as exc:
        load_settings(env_file=None)

    issues = exc.value.issues
    assert len(issues) == 1
    assert issues[0] == ConfigIssue(field="configuration", code="invalid_value")

def test_cv_unknown_field_extra_policy(tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("UNKNOWN_RANDOM_FIELD=123\n", encoding="utf-8")

    s = load_settings(env_file=str(env_file))
    assert getattr(s, "unknown_random_field", None) is None

def test_cv_secret_value_redaction(monkeypatch, capsys, caplog):
    secret = "MySuperSecretPassword123"
    monkeypatch.setenv("TELEGRAM_API_ID", secret)

    with pytest.raises(ConfigStartupError) as exc:
        load_settings(env_file=None)

    err = exc.value
    assert secret not in str(err)
    assert secret not in repr(err)
    assert secret not in repr(err.issues)
    assert not any(secret in str(arg) for arg in err.args)
    assert getattr(err, "__cause__", None) is None

    out, err_str = capsys.readouterr()
    assert secret not in out
    assert secret not in err_str
    assert secret not in caplog.text

def test_cv_independent_instances():
    s1 = load_settings(env_file=None)
    s2 = load_settings(env_file=None)
    assert s1 is not s2

def test_cv_loader_does_not_mutate_environ(tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("WEB_ADMIN_HOST=4.4.4.4\n", encoding="utf-8")

    before = dict(os.environ)
    load_settings(env_file=str(env_file))
    after = dict(os.environ)

    assert before == after

def test_cv_unknown_error_type_normalization():
    valid_fields = set(Settings.model_fields.keys())

    issues1 = _sanitize_validation_errors([{"loc": ("something_nested",), "type": "weird_error_999"}], valid_fields)
    assert len(issues1) == 1
    assert issues1[0] == ConfigIssue(field="configuration", code="configuration_error")

    issues2 = _sanitize_validation_errors([], valid_fields)
    assert len(issues2) == 1
    assert issues2[0] == ConfigIssue(field="configuration", code="configuration_error")

    import types
    from config import ERROR_TYPE_MAP
    assert isinstance(ERROR_TYPE_MAP, types.MappingProxyType)

def test_cv_empty_error_normalization():
    err = ConfigStartupError(())
    issues = err.issues
    assert len(issues) == 1
    assert issues[0] == ConfigIssue("configuration", "configuration_error")
    assert "configuration_error" in str(err)
    assert repr(err) == "ConfigStartupError(1 issues)"
    assert "configuration_error" in err.args[0]

def test_telegram_configured_from_valid_session_string():
    config = TelegramConfig(123, "abc", "session_data")
    assert is_telegram_configured_from(config) is True

def test_telegram_configured_from_whitespace_session_string():
    config = TelegramConfig(123, "abc", "   ")
    assert is_telegram_configured_from(config) is False

def test_telegram_configured_from_missing_api_id():
    assert is_telegram_configured_from(TelegramConfig(None, "abc", "session")) is False

def test_telegram_configured_from_zero_api_id():
    assert is_telegram_configured_from(TelegramConfig(0, "abc", "session")) is False

def test_telegram_configured_from_negative_api_id():
    assert is_telegram_configured_from(TelegramConfig(-5, "abc", "session")) is False

def test_telegram_configured_from_missing_api_hash():
    assert is_telegram_configured_from(TelegramConfig(123, None, "session")) is False
    assert is_telegram_configured_from(TelegramConfig(123, "", "session")) is False

def test_telegram_configured_from_whitespace_api_hash():
    assert is_telegram_configured_from(TelegramConfig(123, "   ", "session")) is False

def test_telegram_configured_from_path_none_without_session_string():
    config = TelegramConfig(123, "abc", "")
    assert is_telegram_configured_from(config, session_path_override=None) is False

def test_telegram_configured_from_missing_file(tmp_path):
    session_file = tmp_path / "test.session"
    config = TelegramConfig(123, "abc", "")
    assert is_telegram_configured_from(config, session_path_override=session_file) is False

def test_telegram_configured_from_existing_regular_file(tmp_path):
    session_file = tmp_path / "test.session"
    session_file.write_text("dummy")
    config = TelegramConfig(123, "abc", "")
    assert is_telegram_configured_from(config, session_path_override=session_file) is True

def test_telegram_configured_from_directory_path(tmp_path):
    dir_path = tmp_path / "somedir"
    dir_path.mkdir()
    config = TelegramConfig(123, "abc", "")
    assert is_telegram_configured_from(config, session_path_override=dir_path) is False

@patch("pathlib.Path.is_file")
def test_telegram_configured_from_session_string_skips_path(mock_is_file, tmp_path):
    config = TelegramConfig(123, "abc", "session")
    session_file = tmp_path / "test.session"
    assert is_telegram_configured_from(config, session_path_override=session_file) is True
    mock_is_file.assert_not_called()

@patch("utils.settings")
def test_legacy_wrapper_uses_patched_settings(mock_settings):
    mock_settings.telegram_api_id = 123
    mock_settings.telegram_api_hash = "abc"
    mock_settings.telegram_session_string = "session"

    assert is_telegram_configured() is True

    mock_settings.telegram_api_id = None
    assert is_telegram_configured() is False

@patch("utils.is_telegram_configured_from")
@patch("utils.settings")
def test_legacy_wrapper_default_path(mock_settings, mock_helper):
    mock_settings.telegram_api_id = 123
    mock_settings.telegram_api_hash = "abc"
    mock_settings.telegram_session_string = ""
    mock_helper.return_value = True

    is_telegram_configured()
    args, kwargs = mock_helper.call_args
    passed_path = kwargs.get("session_path_override")
    expected_path = Path(__file__).parent.parent.absolute() / "bot_session.session"
    assert passed_path == expected_path

@patch("utils.is_telegram_configured_from")
@patch("utils.settings")
def test_legacy_wrapper_relative_string_path(mock_settings, mock_helper):
    mock_settings.telegram_api_id = 123
    mock_settings.telegram_api_hash = "abc"
    mock_settings.telegram_session_string = ""
    mock_helper.return_value = True

    is_telegram_configured("custom.session")
    args, kwargs = mock_helper.call_args
    passed_path = kwargs.get("session_path_override")
    expected_path = Path(__file__).parent.parent.absolute() / "custom.session"
    assert passed_path == expected_path

@patch("utils.is_telegram_configured_from")
@patch("utils.settings")
def test_legacy_wrapper_relative_path_object(mock_settings, mock_helper):
    mock_settings.telegram_api_id = 123
    mock_settings.telegram_api_hash = "abc"
    mock_settings.telegram_session_string = ""
    mock_helper.return_value = True

    is_telegram_configured(Path("custom.session"))
    args, kwargs = mock_helper.call_args
    passed_path = kwargs.get("session_path_override")
    # Legacy semantics: Path objects are not made absolute to project root
    assert passed_path == Path("custom.session")

@patch("utils.is_telegram_configured_from")
@patch("utils.settings")
def test_legacy_wrapper_absolute_path(mock_settings, mock_helper, tmp_path):
    mock_settings.telegram_api_id = 123
    mock_settings.telegram_api_hash = "abc"
    mock_settings.telegram_session_string = ""
    mock_helper.return_value = True

    abs_path = tmp_path / "absolute.session"
    is_telegram_configured(abs_path)
    args, kwargs = mock_helper.call_args
    passed_path = kwargs.get("session_path_override")
    assert passed_path == abs_path

@patch("utils.settings")
def test_legacy_wrapper_filesystem_relative_path(mock_settings, tmp_path, monkeypatch):
    mock_settings.telegram_api_id = 123
    mock_settings.telegram_api_hash = "abc"
    mock_settings.telegram_session_string = ""

    # Change working directory to a temporary path
    monkeypatch.chdir(tmp_path)

    # Verify relative string path translates to project root
    # So if we write a file to project root, it works (but we don't want to touch project root)
    # Let's verify relative Path object instead, which stays relative to cwd
    session_file = Path("test_cwd.session")

    assert is_telegram_configured(session_file) is False

    session_file.write_text("dummy")
    assert is_telegram_configured(session_file) is True

def test_legacy_wrapper_normalizer_int():
    assert _normalize_legacy_api_id(123) == 123

def test_legacy_wrapper_normalizer_numeric_string():
    assert _normalize_legacy_api_id("123") == 123

def test_legacy_wrapper_normalizer_whitespace_numeric_string():
    assert _normalize_legacy_api_id("  123  ") == 123

def test_legacy_wrapper_normalizer_malformed_string():
    assert _normalize_legacy_api_id("123abc") is None
    assert _normalize_legacy_api_id("invalid") is None

def test_legacy_wrapper_normalizer_empty_string():
    assert _normalize_legacy_api_id("") is None
    assert _normalize_legacy_api_id("   ") is None

def test_legacy_wrapper_normalizer_bool():
    assert _normalize_legacy_api_id(True) is None
    assert _normalize_legacy_api_id(False) is None

def test_legacy_wrapper_normalizer_none():
    assert _normalize_legacy_api_id(None) is None

def test_legacy_wrapper_normalizer_arbitrary_object():
    assert _normalize_legacy_api_id(object()) is None
