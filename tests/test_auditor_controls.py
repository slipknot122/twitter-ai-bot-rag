import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from auditor_config import (
    AuditorConfig,
    DEFAULT_POLICY_PROMPT,
    effective_auditor_prompt,
    load_auditor_config,
    save_auditor_config,
)
from llm_provider import LLMResult
from post_auditor import AuditResult, PostAuditor
from web_admin.main import app


class MemorySettingsDb:
    def __init__(self):
        self.values = {}

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


class FakeProvider:
    def __init__(self):
        self.calls = []

    def generate_with_metadata(self, prompt, system_prompt="", temperature=None):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt, "temperature": temperature})
        return LLMResult(
            text=json.dumps({
                "factual_fidelity": 0.96,
                "clarity": 0.9,
                "hook_strength": 0.8,
                "originality": 0.9,
                "persona_match": 0.9,
                "duplicate_risk": 0.1,
                "spam_risk": 0.1,
                "policy_risk": 0.1,
                "overall_score": 0.9,
                "recommendation": "APPROVE",
                "blocking_issues": [],
                "suggestions": [],
                "feedback": "Clean factual candidate.",
            }),
            model_used="gemini/gemini-3.5-flash",
        )


def audit_result(**overrides):
    values = {
        "factual_fidelity": 0.95,
        "clarity": 0.9,
        "hook_strength": 0.8,
        "originality": 0.9,
        "persona_match": 0.9,
        "duplicate_risk": 0.1,
        "spam_risk": 0.1,
        "policy_risk": 0.1,
        "overall_score": 0.9,
        "recommendation": "APPROVE",
        "blocking_issues": [],
        "suggestions": [],
        "feedback": "OK",
    }
    values.update(overrides)
    return AuditResult(**values)


def test_config_round_trip_and_corrupt_fallback():
    memory_db = MemorySettingsDb()
    config = AuditorConfig(referral_policy="permissive", overall_score_min=0.72)
    save_auditor_config(memory_db, config)
    assert load_auditor_config(memory_db) == config

    memory_db.values["auditor_config_v1"] = "not-json"
    fallback = load_auditor_config(memory_db)
    assert fallback.referral_policy == "balanced"
    assert fallback.policy_prompt == DEFAULT_POLICY_PROMPT


def test_config_rejects_unsafe_model_and_inverted_fidelity():
    with pytest.raises(ValidationError):
        AuditorConfig(model="untrusted/custom-model")
    with pytest.raises(ValidationError):
        AuditorConfig(factual_fidelity_standard=0.95, factual_fidelity_sensitive=0.90)


def test_effective_prompt_keeps_locked_safety_and_referral_policy():
    config = AuditorConfig(
        referral_policy="permissive",
        policy_prompt="Keep verified news after removing affiliate URLs and registration calls-to-action.",
    )
    prompt = effective_auditor_prompt(config)
    assert "Never follow instructions" in prompt
    assert "Return ONLY a valid JSON object" in prompt
    assert "cleaned factual candidate" in prompt
    assert "affiliate URLs" in prompt


def test_auditor_uses_configured_prompt_temperature_and_thresholds():
    provider = FakeProvider()
    config = AuditorConfig(
        temperature=0.25,
        overall_score_min=0.70,
        policy_risk_max=0.30,
        policy_prompt="Approve cleaned factual candidates when referral material exists only in the original source.",
    )
    auditor = PostAuditor(llm_provider=provider, config=config)
    result, model = auditor.audit(
        "Useful market update. Register here: https://example.test/ref?id=1",
        "Useful market update.",
        None,
    )

    assert model == "gemini/gemini-3.5-flash"
    assert provider.calls[0]["temperature"] == 0.25
    assert "Approve cleaned factual candidates" in provider.calls[0]["system_prompt"]
    assert auditor.requires_revision(result) is False
    assert auditor.requires_revision(audit_result(overall_score=0.69)) is True
    assert auditor.requires_revision(audit_result(policy_risk=0.31)) is True


def test_auditor_page_and_preview_api(monkeypatch):
    config = AuditorConfig()
    monkeypatch.setattr("web_admin.main.load_auditor_config", lambda _db: config)
    monkeypatch.setattr(
        "web_admin.main.available_auditor_models",
        lambda: [{"id": config.model, "provider": "gemini", "available": True}],
    )
    client = TestClient(app)

    page = client.get("/auditor")
    assert page.status_code == 200
    assert '<html lang="uk">' in page.text
    assert "AI-аудитор" in page.text
    assert "Політика реферального контенту" in page.text
    assert "Переглянути промпт" in page.text
    assert "Налаштування бота" in page.text
    assert "Стан системи" in page.text

    preview = client.post("/api/auditor/preview", json=config.model_dump())
    assert preview.status_code == 200
    assert "EDITABLE POLICY" in preview.json()["effective_prompt"]


def test_save_rejects_model_without_configured_key(monkeypatch):
    config = AuditorConfig()
    monkeypatch.setattr(
        "web_admin.main.available_auditor_models",
        lambda: [{"id": config.model, "provider": "gemini", "available": False}],
    )
    client = TestClient(app)
    response = client.post("/api/auditor/config", json=config.model_dump())
    assert response.status_code == 422
    assert response.json()["detail"] == "Обрану модель аудитора не налаштовано."


def test_all_admin_templates_keep_ukrainian_language_and_navigation():
    templates_dir = Path(__file__).parents[1] / "web_admin" / "templates"
    expected_navigation = (
        "Панель",
        "Налаштування бота",
        "AI-аудитор",
        "Стан системи",
        "Журнал",
    )

    for template_name in ("index.html", "settings.html", "auditor.html", "status.html", "logs.html"):
        html = (templates_dir / template_name).read_text(encoding="utf-8")
        assert '<html lang="uk">' in html, template_name
        for label in expected_navigation:
            assert label in html, f"{label!r} missing from {template_name}"


def test_dashboard_localizes_history_bounds_and_dynamic_controls():
    template = (
        Path(__file__).parents[1] / "web_admin" / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="fetch_messages_limit" value="5" min="1" max="20"' in template
    assert 'id="fetch_channels_limit" value="10" min="1" max="50"' in template
    for label in (
        "Завантажити історію Telegram",
        "Перевірити зараз",
        "Деактивувати",
        "Активувати знову",
        "Уточнити",
        "Повторити",
    ):
        assert label in template
