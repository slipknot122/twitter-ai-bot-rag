import json

import pytest

from database import Database, normalize_source_keywords, source_text_matches


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
