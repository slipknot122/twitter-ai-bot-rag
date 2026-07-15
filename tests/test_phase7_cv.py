import ast
import inspect
import os
import subprocess
import sys
import traceback
from dataclasses import MISSING, FrozenInstanceError, dataclass, fields
from pathlib import Path
from typing import Any, NoReturn, get_args, get_origin, get_type_hints
from unittest.mock import patch

import pytest
import requests

import llm_provider
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

# --- A3.3a: LLMProvider Environment Mutability & Security Fixes ---



def _make_llm_config(
    *,
    model: str = "gemini/gemini-1.5-pro",
    temperature: float = 0.2,
    openai_api_key: str | None = "OPENAI_KEY_SENTINEL_91f2",
    gemini_api_key: str | None = "GEMINI_KEY_SENTINEL_91f2",
) -> LLMConfig:
    return LLMConfig(
        model=model,
        temperature=temperature,
        openai_api_key=openai_api_key,
        gemini=GeminiConfig(api_key=gemini_api_key),
    )


def _scan_security_source(source: str) -> None:
    tree = ast.parse(source)

    def _os_names(tree: ast.AST) -> set[str]:
        names = {"os"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os":
                        names.add(alias.asname or alias.name)
        return names

    os_aliases = _os_names(tree)

    def _is_os_environ_target(target: ast.AST) -> bool:
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute):
            if isinstance(target.value.value, ast.Name) and target.value.value.id in os_aliases:
                if target.value.attr == "environ":
                    return True
        if isinstance(target, ast.Attribute):
            if isinstance(target.value, ast.Name) and target.value.id in os_aliases:
                if target.attr == "environ":
                    return True
        return False

    def _is_os_putenv_call(node: ast.Call) -> bool:
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id in os_aliases:
                if node.func.attr in {"putenv", "unsetenv"}:
                    return True
        return False

    def _is_os_environ_call(node: ast.Call) -> bool:
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Attribute):
                if isinstance(node.func.value.value, ast.Name) and node.func.value.value.id in os_aliases:
                    if node.func.value.attr == "environ":
                        if node.func.attr in {"update", "setdefault", "pop", "clear", "popitem"}:
                            return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if _is_os_environ_target(target):
                    raise ValueError("os.environ assignment detected")

        if isinstance(node, ast.Call):
            if _is_os_putenv_call(node):
                raise ValueError("os.putenv/unsetenv detected")
            if _is_os_environ_call(node):
                raise ValueError("os.environ mutation method detected")

            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "logger":
                    tainted = {"api_key", "kwargs", "params", "raw_exception"}
                    def _is_tainted(val: ast.AST) -> bool:
                        if isinstance(val, ast.Name) and val.id in tainted:
                            return True
                        if isinstance(val, ast.Attribute) and isinstance(val.value, ast.Name) and val.value.id == "response" and val.attr == "text":
                            return True
                        return False
                    for kw in node.keywords:
                        if _is_tainted(kw.value):
                            raise ValueError("logger call with tainted kwargs detected")
                    for arg in node.args:
                        if _is_tainted(arg):
                            raise ValueError(f"logger with tainted arg detected")

            if isinstance(node.func, ast.Name) and node.func.id == "print":
                if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "api_key":
                    raise ValueError("print call detected")
                # allowed print("safe diagnostic") since 'secret' is removed

        if isinstance(node, ast.Delete):
            for target in node.targets:
                if _is_os_environ_target(target):
                    raise ValueError("del os.environ detected")

        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "GEMINI_EMBEDDING_ENDPOINT":
                    if not getattr(node, "value", None) or not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                        raise ValueError("Endpoint is not a string literal")


def test_llm_provider_ast_no_env_mutations() -> None:
    provider_path = Path(llm_provider.__file__)
    source = provider_path.read_text(encoding="utf-8")
    _scan_security_source(source)

@pytest.mark.parametrize(
    "source, match",
    [
        ("print(api_key)", "print call detected"),
        ('os.environ["KEY"] = "value"', "os.environ assignment detected"),
        ('os.environ: str = "value"', "os.environ assignment detected"),
        ('os.environ["KEY"] += "value"', "os.environ assignment detected"),
        ('del os.environ["KEY"]', "del os.environ detected"),
        ('os.environ.update({"KEY": "value"})', "os.environ mutation method detected"),
        ('os.environ.setdefault("KEY", "value")', "os.environ mutation method detected"),
        ('os.environ.pop("KEY")', "os.environ mutation method detected"),
        ('os.environ.popitem()', "os.environ mutation method detected"),
        ('os.environ.clear()', "os.environ mutation method detected"),
        ('os.putenv("KEY", "value")', "os.putenv/unsetenv detected"),
        ('os.unsetenv("KEY")', "os.putenv/unsetenv detected"),
        (
            'import os as operating_system\noperating_system.environ["KEY"] = "secret"',
            "os.environ assignment detected",
        ),
        (
            'import os as operating_system\noperating_system.putenv("KEY", "secret")',
            "os.putenv/unsetenv detected",
        ),
        ('GEMINI_EMBEDDING_ENDPOINT = f"https://api.com?key={key}"', "Endpoint is not a string literal"),
        ('GEMINI_EMBEDDING_ENDPOINT = "https://api.com?key=" + key', "Endpoint is not a string literal"),
        ('logger.info("message", key=api_key)', "logger call with tainted kwargs detected"),
        ('logger.info("message", key=kwargs)', "logger call with tainted kwargs detected"),
        ('logger.info("message", key=params)', "logger call with tainted kwargs detected"),
        ('logger.error("{}", raw_exception)', "logger with tainted arg detected"),
        ('logger.error(response.text)', "logger with tainted arg detected"),
    ],
)
def test_security_scanner_rejects_bad_patterns(source: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _scan_security_source(source)


def test_security_scanner_allows_safe_patterns() -> None:
    _scan_security_source('print("safe diagnostic")')
    _scan_security_source('logger.info("model {}", model)')
    _scan_security_source('logger.info("count", extra={"count": count})')
    _scan_security_source('os.environ.get("KEY")')


class ForbiddenSettings:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"unexpected settings access: {name}")


def test_explicit_constructor_does_not_read_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_llm_config()
    monkeypatch.setattr(
        llm_provider,
        "settings",
        ForbiddenSettings(),
    )

    provider = llm_provider.LLMProvider(config)

    assert provider._config is config


def test_explicit_constructor_does_not_call_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_call(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("network call during construction")

    monkeypatch.setattr(llm_provider, "completion", forbidden_call)
    monkeypatch.setattr(llm_provider, "embedding", forbidden_call)
    monkeypatch.setattr(llm_provider.requests, "post", forbidden_call)

    provider = llm_provider.LLMProvider(_make_llm_config())

    assert provider.model == "gemini/gemini-1.5-pro"


def test_llm_provider_legacy_no_arg_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummySettings:
        openai_api_key = "old_openai"
        gemini_api_key = "old_gemini"
        llm_model = "old_model"
        llm_temperature = 0.9

    monkeypatch.setattr(llm_provider, "settings", DummySettings())

    provider = llm_provider.LLMProvider()
    assert provider._config.openai_api_key == "old_openai"
    assert provider._config.gemini.api_key == "old_gemini"
    assert provider._config.model == "old_model"


def test_llm_provider_constructor_no_env_mutate() -> None:
    before = dict(os.environ)
    cfg = _make_llm_config()
    provider = llm_provider.LLMProvider(cfg)
    assert dict(os.environ) == before


def test_configured_primary_model_is_first() -> None:
    cfg = _make_llm_config(model="gemini/gemini-custom")
    provider = llm_provider.LLMProvider(cfg)
    models = provider._models_to_try()
    assert models[0] == "gemini/gemini-custom"


def test_duplicate_fallback_removed_order_kept() -> None:
    cfg = _make_llm_config(model="gemini/gemini-2.5-flash")
    provider = llm_provider.LLMProvider(cfg)
    models = provider._models_to_try()
    assert models[0] == "gemini/gemini-2.5-flash"
    assert models == ("gemini/gemini-2.5-flash", "gemini/gemini-3.5-flash", "gemini/gemini-3.1-flash-lite")


def test_non_gemini_no_gemini_fallbacks() -> None:
    cfg = _make_llm_config(model="openai/gpt-4o")
    provider = llm_provider.LLMProvider(cfg)
    models = provider._models_to_try()
    assert models == ("openai/gpt-4o",)


def test_each_attempt_gets_new_kwargs() -> None:
    cfg = _make_llm_config()
    provider = llm_provider.LLMProvider(cfg)
    kw1 = provider._completion_kwargs(model="gemini/1", temperature=0.5)
    kw2 = provider._completion_kwargs(model="gemini/2", temperature=0.5)
    assert kw1 is not kw2
    kw1["mutated"] = True
    assert "mutated" not in kw2

@pytest.mark.parametrize(
    "model_id, expected_key",
    [
        ("openai/gpt-4o", "OPENAI_KEY_SENTINEL_91f2"),
        ("gemini/gemini-1.5-pro", "GEMINI_KEY_SENTINEL_91f2"),
        ("custom/not-gemini", None),
        ("gpt-4o", None),
    ],
)
def test_api_key_resolver_cases(model_id: str, expected_key: str | None) -> None:
    cfg = _make_llm_config()
    provider = llm_provider.LLMProvider(cfg)
    kw = provider._completion_kwargs(model=model_id, temperature=0.5)
    assert kw.get("api_key") == expected_key


def test_provider_resolver_cases() -> None:
    assert llm_provider._provider_for_model("gemini/model") == "gemini"
    assert llm_provider._provider_for_model("openai/model") == "openai"
    assert llm_provider._provider_for_model("GEMINI/model") == "gemini"
    assert llm_provider._provider_for_model("custom/not-gemini") is None
    assert llm_provider._provider_for_model("company/gpt-wrapper") is None
    assert llm_provider._provider_for_model("gpt-4o") is None
    assert llm_provider._provider_for_model("") is None


def test_primary_and_fallback_get_same_gemini_key() -> None:
    cfg = _make_llm_config(model="gemini/gemini-3.5-flash")
    provider = llm_provider.LLMProvider(cfg)
    models = provider._models_to_try()
    keys = [provider._completion_kwargs(model=m, temperature=0.5).get("api_key") for m in models]
    assert all(k == "GEMINI_KEY_SENTINEL_91f2" for k in keys)

@dataclass
class FakeMessage:
    content: str = "ok"

@dataclass
class FakeChoice:
    message: FakeMessage

@dataclass
class FakeUsage:
    total_tokens: int = 3
    prompt_tokens: int = 2
    completion_tokens: int = 1

@dataclass
class FakeCompletionResponse:
    choices: list[FakeChoice]
    usage: FakeUsage


def test_temperature_override_no_config_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config(temperature=0.5)
    provider = llm_provider.LLMProvider(cfg)

    called_kwargs: dict[str, object] = {}

    def fake_completion(*args: object, **kwargs: object) -> FakeCompletionResponse:
        called_kwargs.update(kwargs)
        return FakeCompletionResponse(
            choices=[FakeChoice(message=FakeMessage(content="ok"))],
            usage=FakeUsage(),
        )

    monkeypatch.setattr(llm_provider, "completion", fake_completion)

    provider.generate_with_metadata("prompt", temperature=0.1)
    assert provider._config.temperature == 0.5
    assert called_kwargs.get("temperature") == 0.1


def test_failure_goes_to_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config(model="gemini/gemini-3.5-flash")
    provider = llm_provider.LLMProvider(cfg)

    call_count = 0

    def fake_completion(*args: object, **kwargs: object) -> FakeCompletionResponse:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise llm_provider.LLMProviderError("fake")
        return FakeCompletionResponse(
            choices=[FakeChoice(message=FakeMessage(content="ok"))],
            usage=FakeUsage(),
        )

    monkeypatch.setattr(llm_provider, "completion", fake_completion)

    res = provider.generate_with_metadata("prompt")
    assert call_count == 3
    assert res.model_used == "gemini/gemini-2.5-flash"


def test_success_stops_fallback_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config(model="gemini/gemini-3.5-flash")
    provider = llm_provider.LLMProvider(cfg)

    call_count = 0

    def fake_completion(*args: object, **kwargs: object) -> FakeCompletionResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise llm_provider.LLMProviderError("fake")
        return FakeCompletionResponse(
            choices=[FakeChoice(message=FakeMessage(content="ok"))],
            usage=FakeUsage(),
        )

    monkeypatch.setattr(llm_provider, "completion", fake_completion)

    res = provider.generate_with_metadata("prompt")
    assert call_count == 2
    assert res.model_used == "gemini/gemini-3.1-flash-lite"


def test_completion_success_no_env_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config()
    provider = llm_provider.LLMProvider(cfg)
    before = dict(os.environ)

    def fake_completion(*args: object, **kwargs: object) -> FakeCompletionResponse:
        return FakeCompletionResponse(
            choices=[FakeChoice(message=FakeMessage(content="ok"))],
            usage=FakeUsage(),
        )

    monkeypatch.setattr(llm_provider, "completion", fake_completion)

    provider.generate_with_metadata("prompt")
    assert dict(os.environ) == before


def test_completion_exception_no_env_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config()
    provider = llm_provider.LLMProvider(cfg)
    before = dict(os.environ)

    def fake_completion(*args: object, **kwargs: object) -> NoReturn:
        raise llm_provider.LLMProviderError("fake")

    monkeypatch.setattr(llm_provider, "completion", fake_completion)
    monkeypatch.setattr(provider.generate_with_metadata.retry, "sleep", lambda _: None)

    with pytest.raises(llm_provider.LLMProviderError):
        provider.generate_with_metadata("prompt")
    assert dict(os.environ) == before


def test_embedding_success_no_env_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config(model="openai/gpt")
    provider = llm_provider.LLMProvider(cfg)
    before = dict(os.environ)

    class FakeEmbeddingResponse:
        data = [{"embedding": [0.1]}]

    def fake_embedding(*args: object, **kwargs: object) -> FakeEmbeddingResponse:
        return FakeEmbeddingResponse()

    monkeypatch.setattr(llm_provider, "embedding", fake_embedding)

    provider.get_embedding("text")
    assert dict(os.environ) == before


def test_embedding_custom_fallback_does_not_call_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = llm_provider.LLMProvider(_make_llm_config(model="custom/not-gemini", openai_api_key=None))
    called_gemini = False

    def fake_post(*args: object, **kwargs: object) -> NoReturn:
        nonlocal called_gemini
        called_gemini = True
        raise AssertionError("Should not be called")

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    monkeypatch.setattr(provider.get_embedding.retry, "sleep", lambda _: None)
    monkeypatch.setattr(provider.get_embedding.retry, "sleep", lambda _: None)

    import tenacity
    with pytest.raises(tenacity.RetryError) as exc:
        provider.get_embedding("text")

    assert not called_gemini
    inner = exc.value.last_attempt.exception()
    assert isinstance(inner, llm_provider.LLMProviderError)
    assert inner.code == "openai_api_key_missing"


def test_embedding_transport_failure_no_env_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config(model="gemini/model")
    provider = llm_provider.LLMProvider(cfg)
    before = dict(os.environ)

    def fake_post(*args: object, **kwargs: object) -> NoReturn:
        raise requests.RequestException("fail")

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    monkeypatch.setattr(provider.get_embedding.retry, "sleep", lambda _: None)
    monkeypatch.setattr(provider.get_embedding.retry, "sleep", lambda _: None)

    import tenacity
    with pytest.raises(tenacity.RetryError) as exc:
        provider.get_embedding("text")

    inner = exc.value.last_attempt.exception()
    assert isinstance(inner, llm_provider.LLMProviderError)
    assert inner.code == "gemini_embedding_transport_failed"
    assert dict(os.environ) == before


def test_embedding_endpoint_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config(model="gemini/model")
    provider = llm_provider.LLMProvider(cfg)

    captured_url = ""
    captured_params: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        def json(self) -> dict[str, Any]:
            return {"embedding": {"values": [0.1]}}

    def fake_post(url: str, *args: object, **kwargs: object) -> FakeResponse:
        nonlocal captured_url, captured_params
        captured_url = url
        captured_params = kwargs.get("params", {})  # type: ignore
        return FakeResponse()

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    monkeypatch.setattr(provider.get_embedding.retry, "sleep", lambda _: None)
    monkeypatch.setattr(provider.get_embedding.retry, "sleep", lambda _: None)

    provider.get_embedding("text")
    assert captured_url == llm_provider.GEMINI_EMBEDDING_ENDPOINT
    assert "?" not in captured_url
    assert "GEMINI_KEY_SENTINEL_91f2" not in captured_url
    assert captured_params == {"key": "GEMINI_KEY_SENTINEL_91f2"}


def test_http_status_json_errors_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config(model="gemini/model")
    provider = llm_provider.LLMProvider(cfg)

    class FakeResponse500:
        status_code = 500
        def json(self) -> dict[str, Any]:
            return {}

    class FakeResponse200Bad:
        status_code = 200
        def json(self) -> dict[str, Any]:
            return {}

    def fake_post_500(*args: object, **kwargs: object) -> FakeResponse500:
        return FakeResponse500()

    def fake_post_200_bad(*args: object, **kwargs: object) -> FakeResponse200Bad:
        return FakeResponse200Bad()

    monkeypatch.setattr(llm_provider.requests, "post", fake_post_500)
    monkeypatch.setattr(provider.get_embedding.retry, "sleep", lambda _: None)
    import tenacity
    with pytest.raises(tenacity.RetryError) as exc:
        provider.get_embedding("text")
    inner = exc.value.last_attempt.exception()
    assert inner.code == "gemini_embedding_failed"

    monkeypatch.setattr(llm_provider.requests, "post", fake_post_200_bad)
    with pytest.raises(tenacity.RetryError) as exc:
        provider.get_embedding("text")
    inner = exc.value.last_attempt.exception()
    assert inner.code == "gemini_embedding_invalid_response"


def test_secrets_absent_in_logs_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _make_llm_config(model="gemini/model")
    provider = llm_provider.LLMProvider(cfg)

    def fake_completion(*args: object, **kwargs: object) -> NoReturn:
        raise llm_provider.LLMProviderError("fake")

    monkeypatch.setattr(llm_provider, "completion", fake_completion)
    monkeypatch.setattr(provider.generate_with_metadata.retry, "sleep", lambda _: None)

    with pytest.raises(llm_provider.LLMProviderError):
        provider.generate_with_metadata("prompt")

    out, err = capsys.readouterr()
    assert "GEMINI_KEY_SENTINEL_91f2" not in out
    assert "GEMINI_KEY_SENTINEL_91f2" not in err
    assert "GEMINI_KEY_SENTINEL_91f2" not in caplog.text

    assert "OPENAI_KEY_SENTINEL_91f2" not in out
    assert "OPENAI_KEY_SENTINEL_91f2" not in err
    assert "OPENAI_KEY_SENTINEL_91f2" not in caplog.text


def test_global_llm_legacy_export_contract() -> None:
    assert type(llm_provider.llm) is llm_provider.LLMProvider
    assert getattr(llm_provider.llm, "generate_with_metadata")
    assert getattr(llm_provider.llm, "get_embedding")


def test_import_contract_consumers_unchanged() -> None:
    sig_gen = inspect.signature(llm_provider.LLMProvider.generate)
    assert "prompt" in sig_gen.parameters
    assert "system_prompt" in sig_gen.parameters
    assert "temperature" in sig_gen.parameters
    assert sig_gen.return_annotation is str

    sig_emb = inspect.signature(llm_provider.LLMProvider.get_embedding)
    assert "text" in sig_emb.parameters


def test_llm_provider_error() -> None:
    err = llm_provider.LLMProviderError("completion_failed")
    assert err.code == "completion_failed"
    assert err.args == ("completion_failed",)
    assert not hasattr(err, "gemini_api_key")
    assert not hasattr(err, "openai_api_key")
    assert "secret" not in dir(err)


def test_retry_cardinality_generate_with_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config(model="gemini/gemini-1.5-pro")
    provider = llm_provider.LLMProvider(cfg)

    call_count = 0

    def fake_completion(*args: object, **kwargs: object) -> NoReturn:
        nonlocal call_count
        call_count += 1
        raise llm_provider.LLMProviderError("fake")

    monkeypatch.setattr(llm_provider, "completion", fake_completion)
    monkeypatch.setattr(provider.generate_with_metadata.retry, "sleep", lambda _: None)

    with pytest.raises(llm_provider.LLMProviderError):
        provider.generate_with_metadata("prompt")

    # Tenacity tries 3 times. We have 3 models in fallback loop.
    # 3 (tenacity) * 3 (models loop) = 9
    assert call_count == 12


def test_retry_cardinality_generate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_llm_config(model="gemini/gemini-1.5-pro")
    provider = llm_provider.LLMProvider(cfg)

    call_count = 0

    def fake_completion(*args: object, **kwargs: object) -> NoReturn:
        nonlocal call_count
        call_count += 1
        raise llm_provider.LLMProviderError("fake")

    monkeypatch.setattr(llm_provider, "completion", fake_completion)
    monkeypatch.setattr(provider.generate_with_metadata.retry, "sleep", lambda _: None)
    monkeypatch.setattr(provider.generate.retry, "sleep", lambda _: None)

    with pytest.raises(llm_provider.LLMProviderError):
        provider.generate("prompt")

    # generate() wraps generate_with_metadata(), both have 3x retry
    # 3 (generate) * 3 (generate_with_metadata) * 3 (models loop) = 27
    assert call_count == 36
