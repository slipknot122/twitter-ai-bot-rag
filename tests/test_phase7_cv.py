import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, MISSING, fields
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from unittest.mock import patch
import pytest

from config import (
    load_settings,
    ConfigStartupError,
    ConfigIssue,
    Settings,
    _sanitize_validation_errors,
)
from runtime_types import (
    CloudflareConfig,
    DatabaseConfig,
    GeminiConfig,
    LLMConfig,
    MediaConfig,
    PollinationsConfig,
    RuntimeDependencies,
    TelegramConfig,
    TwitterConfig,
)
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

def _get_dummy_resources() -> dict[str, Any]:
    class DummyDB:
        def __repr__(self):
            return "DANGEROUS_DB_REPR"

    class DummyLLM:
        def __repr__(self):
            return "DANGEROUS_LLM_REPR"

    class DummyPub:
        def __repr__(self):
            return "DANGEROUS_PUB_REPR"

    class DummyMedia:
        def __repr__(self):
            return "DANGEROUS_MEDIA_REPR"

    class DummyMem:
        def __repr__(self):
            return "DANGEROUS_MEM_REPR"

    return {
        "db": DummyDB(),
        "llm": DummyLLM(),
        "publisher": DummyPub(),
        "media": DummyMedia(),
        "semantic_memory": DummyMem(),
    }


def _get_config_instances() -> list[Any]:
    g = GeminiConfig(api_key="123")
    cf = CloudflareConfig(account_id="a", api_token="b", image_model="c")
    po = PollinationsConfig(api_key="a", image_model="b")
    return [
        TelegramConfig(api_id=1, api_hash="h", session_string="s"),
        g,
        cf,
        po,
        DatabaseConfig(db_path="test.db"),
        LLMConfig(
            openai_api_key="a",
            gemini=g,
            model="m",
            temperature=0.5,
        ),
        TwitterConfig(
            api_key="a",
            api_secret="s",
            access_token="t",
            access_token_secret="ts",
            bearer_token="b",
            dry_run=True,
        ),
        MediaConfig(
            media_dir=Path("media"),
            gemini=g,
            cloudflare=cf,
            pollinations=po,
        ),
        RuntimeDependencies(**_get_dummy_resources()),  # type: ignore[arg-type]
    ]


def id_fn(obj: Any) -> str:
    return type(obj).__name__


@pytest.mark.parametrize("obj", _get_config_instances(), ids=id_fn)
def test_config_slices_are_frozen(obj):
    with pytest.raises(FrozenInstanceError):
        field_name = fields(obj)[0].name
        setattr(obj, field_name, getattr(obj, field_name))

@pytest.mark.parametrize("obj", _get_config_instances(), ids=id_fn)
def test_config_slices_have_slots(obj):
    assert not hasattr(obj, "__dict__"), f"{type(obj).__name__} should use slots"

def test_deep_immutability():
    classes = [
        TelegramConfig, GeminiConfig, CloudflareConfig, PollinationsConfig,
        DatabaseConfig, LLMConfig, TwitterConfig, MediaConfig, RuntimeDependencies
    ]

    def _check_no_mutable_collections(type_hint):
        origin = get_origin(type_hint) or type_hint
        if origin in (list, dict, set):
            return False
        args = get_args(type_hint)
        for a in args:
            if not _check_no_mutable_collections(a):
                return False
        return True

    for cls in classes:
        # Create a local namespace for RuntimeDependencies to resolve forward references
        # without importing the actual resource modules
        local_ns = {}
        if cls is RuntimeDependencies:
            local_ns = {
                "Database": Any,
                "LLMProvider": Any,
                "TwitterPublisher": Any,
                "MediaBuilder": Any,
                "SemanticMemory": Any,
            }

        hints = get_type_hints(cls, localns=local_ns)

        for f in fields(cls):
            if f.default is not MISSING:
                assert not isinstance(f.default, (list, dict, set))
            if f.default_factory is not MISSING:
                assert f.default_factory not in (list, dict, set)

            resolved_type = hints[f.name]
            assert _check_no_mutable_collections(resolved_type)

            if cls is MediaConfig and f.name == "media_dir":
                assert resolved_type is Path

    # Control test: ensure our checker actually rejects lists
    assert _check_no_mutable_collections(list[str]) is False
    assert _check_no_mutable_collections(dict[str, Any]) is False
    assert _check_no_mutable_collections(set[int]) is False


def test_secret_annotations_are_exactly_str_none():
    EXPECTED_SECRETS = {
        TelegramConfig: ["api_hash", "session_string"],
        GeminiConfig: ["api_key"],
        CloudflareConfig: ["account_id", "api_token"], # account_id is considered sensitive context
        PollinationsConfig: ["api_key"],
        LLMConfig: ["openai_api_key"],
        TwitterConfig: ["api_key", "api_secret", "access_token", "access_token_secret", "bearer_token"],
    }

    for cls, secret_fields in EXPECTED_SECRETS.items():
        hints = get_type_hints(cls)
        for field_name in secret_fields:
            resolved_type = hints[field_name]
            # Ensure it is exactly `str | None`
            assert resolved_type == str | None, f"{cls.__name__}.{field_name} must be exactly str | None"

def test_shared_gemini_config_identity():
    g = GeminiConfig(api_key="secret")
    llm = LLMConfig(openai_api_key=None, gemini=g, model="m", temperature=0.5)
    cf = CloudflareConfig(account_id=None, api_token=None, image_model="m")
    po = PollinationsConfig(api_key=None, image_model="m")
    media = MediaConfig(media_dir=Path("media"), gemini=g, cloudflare=cf, pollinations=po)

    assert llm.gemini is media.gemini, "GeminiConfig must be shared by identity"

def test_secret_safe_repr():
    tc = TelegramConfig(
        api_id=1,
        api_hash="SENTINEL_TG_HASH",
        session_string="SENTINEL_TG_SESSION",
    )
    assert "SENTINEL_TG_HASH" not in repr(tc)
    assert "SENTINEL_TG_SESSION" not in repr(tc)

    g = GeminiConfig(api_key="SENTINEL_GEMINI_KEY_12345")
    assert "SENTINEL_GEMINI_KEY_12345" not in repr(g)

    llm = LLMConfig(
        openai_api_key="SENTINEL_OPENAI_KEY_67890",
        gemini=g,
        model="m",
        temperature=0.5,
    )
    assert "SENTINEL_OPENAI_KEY_67890" not in repr(llm)
    assert "SENTINEL_GEMINI_KEY_12345" not in repr(llm)

    tw = TwitterConfig(
        api_key="SENTINEL_TWITTER_API_KEY_abc",
        api_secret="SENTINEL_TWITTER_API_SECRET_def",
        access_token="SENTINEL_TWITTER_ACCESS_TOKEN_ghi",
        access_token_secret="SENTINEL_TWITTER_ACCESS_TOKEN_SECRET_jkl",
        bearer_token="SENTINEL_TWITTER_BEARER_TOKEN_mno",
        dry_run=True,
    )
    assert "SENTINEL_TWITTER_API_KEY_abc" not in repr(tw)
    assert "SENTINEL_TWITTER_API_SECRET_def" not in repr(tw)
    assert "SENTINEL_TWITTER_ACCESS_TOKEN_ghi" not in repr(tw)
    assert "SENTINEL_TWITTER_ACCESS_TOKEN_SECRET_jkl" not in repr(tw)
    assert "SENTINEL_TWITTER_BEARER_TOKEN_mno" not in repr(tw)

    cf = CloudflareConfig(
        account_id="SENTINEL_CF_ACCOUNT_pqr",
        api_token="SENTINEL_CF_TOKEN_stu",
        image_model="m",
    )
    assert "SENTINEL_CF_ACCOUNT_pqr" not in repr(cf)
    assert "SENTINEL_CF_TOKEN_stu" not in repr(cf)

    po = PollinationsConfig(
        api_key="SENTINEL_POLL_KEY_vwx",
        image_model="m",
    )
    assert "SENTINEL_POLL_KEY_vwx" not in repr(po)

    media = MediaConfig(
        media_dir=Path("media"),
        gemini=g,
        cloudflare=cf,
        pollinations=po,
    )
    assert "SENTINEL_GEMINI_KEY_12345" not in repr(media)
    assert "SENTINEL_CF_ACCOUNT_pqr" not in repr(media)
    assert "SENTINEL_CF_TOKEN_stu" not in repr(media)
    assert "SENTINEL_POLL_KEY_vwx" not in repr(media)

    deps = _get_dummy_resources()
    rd = RuntimeDependencies(**deps)  # type: ignore[arg-type]
    r = repr(rd)
    assert "DANGEROUS_DB_REPR" not in r
    assert "DANGEROUS_LLM_REPR" not in r
    assert "DANGEROUS_PUB_REPR" not in r
    assert "DANGEROUS_MEDIA_REPR" not in r
    assert "DANGEROUS_MEM_REPR" not in r

def test_runtime_dependencies_identity_and_methods():
    deps = _get_dummy_resources()
    rd = RuntimeDependencies(**deps)  # type: ignore[arg-type]

    assert rd.db is deps["db"]
    assert rd.llm is deps["llm"]
    assert rd.publisher is deps["publisher"]
    assert rd.media is deps["media"]
    assert rd.semantic_memory is deps["semantic_memory"]

    for method in ["close", "build", "initialize", "startup", "shutdown"]:
        assert not hasattr(RuntimeDependencies, method), f"DTO should not have {method}"

def test_import_safety_and_type_checking(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SENTINEL=1\n")

    script = tmp_path / "check_imports.py"
    script.write_text("""
import os
import sys
from pathlib import Path

expected_path = Path(os.environ["PROJECT_ROOT"]).resolve()
tmp_path_obj = Path(os.environ["TMP_PATH"]).resolve()

before_files = set(tmp_path_obj.rglob("*"))
before_modules = set(sys.modules.keys())

import runtime_types

after_modules = set(sys.modules.keys())
after_files = set(tmp_path_obj.rglob("*"))

actual_path = Path(runtime_types.__file__).resolve().parent
if actual_path != expected_path:
    sys.stderr.write(f"WRONG_RUNTIME_TYPES: expected {expected_path}, got {actual_path}\\n")
    sys.exit(1)

new_modules = after_modules - before_modules

forbidden = [
    "config", "pydantic_settings",
    "database", "llm_provider", "twitter_publisher", "media_builder", "semantic_memory"
]
found = [m for m in forbidden if m in new_modules or m in sys.modules]

if found:
    sys.stderr.write(f"FAILED_MODULES:{','.join(found)}\\n")
    sys.exit(1)

if before_files != after_files:
    sys.stderr.write(f"FILES_MUTATED: before={before_files} after={after_files}\\n")
    sys.exit(1)

print("OK")
""", encoding="utf-8", newline="\n")

    env = os.environ.copy()
    env.pop("SENTINEL", None)
    project_root = Path(__file__).parent.parent.absolute()
    env["PYTHONPATH"] = f"{project_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["PROJECT_ROOT"] = str(project_root)
    env["TMP_PATH"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0, f"Subprocess failed (exit {result.returncode}):\\nSTDOUT:\\n{result.stdout}\\nSTDERR:\\n{result.stderr}"
    assert result.stderr == ""
    assert result.stdout.strip() == "OK"
