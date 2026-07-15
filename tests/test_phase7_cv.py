import os
import pytest
from config import load_settings, ConfigStartupError, ConfigIssue, Settings, _sanitize_validation_errors

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
