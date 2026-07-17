import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from database import Database, normalize_source_keywords, source_text_matches
from web_admin.main import app


@pytest.fixture
def database(tmp_path):
    return Database(str(tmp_path / "sources.db"))


def add_source(database, **overrides):
    values = {
        "source_type": "telegram",
        "external_id": "-1001234567890",
        "name": "Tech channel",
    }
    values.update(overrides)
    return database.add_source(**values)


def test_source_management_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "migration.db")
    Database(path)
    database = Database(path)
    with database._get_connection() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(sources)")}
    assert {"poll_interval_minutes", "include_keywords", "exclude_keywords", "archived_at"} <= columns


def test_connection_status_migration_and_persistence(tmp_path):
    path = str(tmp_path / "connection-status.db")
    database = Database(path)
    source = add_source(database)
    checking = database.mark_source_connection_checking(source["id"])
    assert checking["connection_status"] == "checking"
    completed = database.complete_source_connection_check(
        source["id"], ok=False, error_code="timeout", detail="Джерело не відповіло вчасно",
    )
    assert completed["connection_status"] == "failed"
    assert completed["connection_checked_at"]
    assert completed["connection_error_code"] == "timeout"
    reopened = Database(path).get_source(source["id"])
    assert reopened["connection_status"] == "failed"
    assert reopened["connection_error_detail"] == "Джерело не відповіло вчасно"


def test_connection_check_button_and_missing_url_result(database):
    source = add_source(database, source_type="x", external_id="news-account")
    client = TestClient(app)
    with patch("web_admin.main.db", database):
        page = client.get("/sources")
        assert 'data-check-connection' in page.text
        assert "Перевірити з’єднання" in page.text
        response = client.post(f"/api/sources/{source['id']}/check-connection")
        assert response.status_code == 202
        assert response.json()["status"] == "failed"
        stored = database.get_source(source["id"])
        assert stored["connection_status"] == "failed"
        assert stored["connection_error_code"] == "missing_url"
        assert stored["connection_checked_at"]


def test_connection_check_rejects_duplicate(database):
    source = add_source(database, source_type="website", external_id="site", canonical_url="https://example.com")
    client = TestClient(app)
    with patch("web_admin.main.db", database), patch(
        "web_admin.main._source_connection_checks", {source["id"]}
    ):
        response = client.post(f"/api/sources/{source['id']}/check-connection")
    assert response.status_code == 409


def test_keyword_normalization_and_matching():
    assert normalize_source_keywords([" AI ", "ai", "Технології"]) == ["ai", "технології"]
    assert source_text_matches("Новини AI", ["AI"], ["казино"])
    assert not source_text_matches("AI казино", ["AI"], ["казино"])
    assert not source_text_matches("Новини спорту", ["AI"], [])
    assert source_text_matches("Будь-який допис", [], [])


def test_filters_apply_before_draft_creation(database):
    add_source(database, include_keywords=["AI"], exclude_keywords=["реклама"])
    assert database.create_draft_from_active_source("telegram", "-1001234567890", "1", "Новини спорту") == "filtered"
    assert database.create_draft_from_active_source("telegram", "-1001234567890", "2", "AI реклама") == "filtered"
    assert database.create_draft_from_active_source("telegram", "-1001234567890", "3", "Новини AI") == "created"


def test_archive_restore_clears_poll_lease(database):
    source = add_source(database, source_type="rss", external_id="feed", canonical_url="https://example.com/feed")
    with database._get_connection() as connection:
        connection.execute("UPDATE source_poll_state SET lease_token='lease', lease_expires_at='2099-01-01', collector_status='polling' WHERE source_id=?", (source["id"],))
        connection.commit()
    archived = database.archive_source(source["id"])
    assert archived["archived_at"] and archived["is_active"] == 0
    state = database.get_poll_state(source["id"])
    assert state["lease_token"] is None and state["collector_status"] == "idle"
    restored = database.restore_source(source["id"])
    assert restored["archived_at"] is None and restored["is_active"] == 1


def test_delete_requires_detach_and_preserves_snapshots(database):
    source = add_source(database)
    assert database.create_draft_from_active_source("telegram", source["external_id"], "item", "Вміст") == "created"
    assert database.delete_source_permanently(source["id"]) == "conflict"
    assert database.delete_source_permanently(source["id"], detach_drafts=True) == "deleted"
    with database._get_connection() as connection:
        draft = connection.execute("SELECT source_id, source_name_snapshot FROM drafts WHERE source_item_id='item'").fetchone()
    assert draft["source_id"] is None
    assert draft["source_name_snapshot"] == "Tech channel"


def test_source_settings_round_trip(database):
    source = add_source(database, poll_interval_minutes=45, include_keywords=["AI"], exclude_keywords=["spam"])
    assert source["poll_interval_minutes"] == 45
    stored = database.get_source(source["id"])
    assert stored["include_keywords"] == ["ai"]
    assert stored["exclude_keywords"] == ["spam"]
    updated = database.update_source(source["id"], {"include_keywords": ["ML", "ml"], "poll_interval_minutes": 90})
    assert json.loads(updated["include_keywords"]) == ["ml"]
    assert updated["poll_interval_minutes"] == 90


def test_sources_page_and_api_lifecycle(database):
    source = add_source(database, include_keywords=["AI"], poll_interval_minutes=45)
    client = TestClient(app)
    with patch("web_admin.main.db", database):
        page = client.get("/sources")
        assert page.status_code == 200
        assert '<html lang="uk">' in page.text
        assert "Додати джерело" in page.text
        assert "Обов’язкові ключові слова" in page.text

        updated = client.patch(
            f"/api/sources/{source['id']}",
            json={"name": "AI News", "exclude_keywords": ["реклама"]},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "AI News"

        archived = client.post(f"/api/sources/{source['id']}/archive")
        assert archived.status_code == 200
        assert archived.json()["archived_at"]

        restored = client.post(f"/api/sources/{source['id']}/restore")
        assert restored.status_code == 200
        assert restored.json()["archived_at"] is None

        deleted = client.delete(f"/api/sources/{source['id']}?permanent=true")
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"


def test_source_api_requires_explicit_detach(database):
    source = add_source(database)
    database.create_draft_from_active_source("telegram", source["external_id"], "item", "Вміст")
    client = TestClient(app)
    with patch("web_admin.main.db", database):
        conflict = client.delete(f"/api/sources/{source['id']}?permanent=true")
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["actions"] == ["archive", "detach_and_delete"]
        detached = client.delete(
            f"/api/sources/{source['id']}?permanent=true&detach_drafts=true"
        )
        assert detached.status_code == 200
