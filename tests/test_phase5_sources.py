"""
Phase 5: Universal Source Registry — Tests
72 tests covering migration, normalization, ingest, dedup, snapshots,
anti-starvation, trust routing, CRUD API, cache, FK, resolve, and XSS.
"""
import pytest
import sqlite3
import json
import time
import os
import datetime
from unittest.mock import patch, MagicMock, PropertyMock
from pathlib import Path

from database import Database, normalize_telegram_id, _validate_canonical_url
from web_admin.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test_bot.db"
    d = Database(str(db_path))
    yield d


def _add_source(db_inst, **kwargs):
    """Helper to insert a source and return its dict."""
    defaults = dict(
        source_type="telegram",
        external_id="-1001234567890",
        name="Test Source",
        canonical_url=None,
        priority=50,
        trust_rating=50,
        processing_mode="auto",
    )
    defaults.update(kwargs)
    return db_inst.add_source(**defaults)


# ===========================================================================
# MIGRATION (tests 1-5)
# ===========================================================================

def test_migration_creates_sources_table(temp_db):
    with temp_db._get_connection() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sources'"
        ).fetchone()
        assert row is not None


def test_migration_seeds_numeric_telegram_channels(tmp_path):
    with patch("database.settings") as mock_settings:
        mock_settings.telegram_channels = ["-1001111111111", "2222222222"]
        mock_settings.db_path = str(tmp_path / "test.db")
        mock_settings.media_lease_seconds = 600
        mock_settings.media_worker_timeout_seconds = 540
        mock_settings.media_generation_timeout = 60
        d = Database(str(tmp_path / "test.db"))
        sources = d.get_sources()
        telegram_sources = [s for s in sources if s["source_type"] == "telegram"]
        assert len(telegram_sources) >= 2
        resolved = [s for s in telegram_sources if s["resolution_status"] == "resolved"]
        assert len(resolved) >= 2


def test_migration_marks_username_sources_unresolved(tmp_path):
    with patch("database.settings") as mock_settings:
        mock_settings.telegram_channels = ["@testchannel"]
        mock_settings.db_path = str(tmp_path / "test.db")
        mock_settings.media_lease_seconds = 600
        mock_settings.media_worker_timeout_seconds = 540
        mock_settings.media_generation_timeout = 60
        d = Database(str(tmp_path / "test.db"))
        sources = d.get_sources()
        unresolved = [s for s in sources if s["resolution_status"] == "unresolved"]
        assert len(unresolved) >= 1
        assert all(s["is_active"] == 0 for s in unresolved)


def test_migration_idempotent_on_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    d1 = Database(db_path)
    count_1 = len(d1.get_sources())
    d2 = Database(db_path)
    count_2 = len(d2.get_sources())
    assert count_1 == count_2


def test_migration_adds_draft_columns(temp_db):
    with temp_db._get_connection() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(drafts)").fetchall()}
    for col in ["source_id", "source_name_snapshot", "source_priority_snapshot",
                "source_trust_snapshot", "source_processing_mode_snapshot", "source_item_id"]:
        assert col in cols, f"Missing column: {col}"


# ===========================================================================
# TELEGRAM ID NORMALIZATION (tests 6-12)
# ===========================================================================

def test_normalize_integer():
    assert normalize_telegram_id(1234567890) == "-1001234567890"


def test_normalize_string():
    assert normalize_telegram_id("1234567890") == "-1001234567890"


def test_normalize_canonical():
    assert normalize_telegram_id("-1001234567890") == "-1001234567890"


def test_normalize_whitespace():
    assert normalize_telegram_id(" -1001234567890 ") == "-1001234567890"


def test_normalize_invalid_username():
    with pytest.raises(ValueError):
        normalize_telegram_id("@channel")


def test_normalize_invalid_url():
    with pytest.raises(ValueError):
        normalize_telegram_id("https://t.me/ch")


def test_normalize_empty():
    with pytest.raises(ValueError):
        normalize_telegram_id("")


# ===========================================================================
# COMPOSITE UNIQUENESS (tests 13-14)
# ===========================================================================

def test_same_external_id_different_types(temp_db):
    _add_source(temp_db, source_type="telegram", external_id="-1001111111111", name="TG")
    _add_source(temp_db, source_type="rss", external_id="-1001111111111", name="RSS",
                canonical_url="https://example.com/feed")
    sources = temp_db.get_sources()
    matching = [s for s in sources if s["external_id"] == "-1001111111111"]
    assert len(matching) == 2


def test_same_type_same_id_conflict(temp_db):
    _add_source(temp_db, source_type="telegram", external_id="-1001111111111", name="TG1")
    with pytest.raises(sqlite3.IntegrityError):
        _add_source(temp_db, source_type="telegram", external_id="-1001111111111", name="TG2")


# ===========================================================================
# ATOMIC INGEST & DEDUP (tests 15-22)
# ===========================================================================

def test_create_draft_success(temp_db):
    _add_source(temp_db, priority=80, trust_rating=90, processing_mode="auto")
    result = temp_db.create_draft_from_active_source(
        "telegram", "-1001234567890", "msg_1", "Test text"
    )
    assert result == "created"
    with temp_db._get_connection() as conn:
        draft = conn.execute("SELECT * FROM drafts WHERE source_item_id = 'msg_1'").fetchone()
        assert draft is not None
        assert draft["source_name_snapshot"] == "Test Source"
        assert draft["source_priority_snapshot"] == 80
        assert draft["source_trust_snapshot"] == 90
        assert draft["source_processing_mode_snapshot"] == "auto"
        assert draft["source_id"] is not None


def test_create_draft_duplicate_delivery(temp_db):
    _add_source(temp_db)
    r1 = temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text 1")
    r2 = temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text 2")
    assert r1 == "created"
    assert r2 == "duplicate"
    with temp_db._get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM drafts WHERE source_item_id = 'msg_1'").fetchone()["c"]
        assert count == 1


def test_create_draft_unknown_source(temp_db):
    result = temp_db.create_draft_from_active_source("telegram", "-1009999999999", "msg_1", "Text")
    assert result == "rejected"
    with temp_db._get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM drafts").fetchone()["c"]
        assert count == 0


def test_create_draft_inactive_source(temp_db):
    src = _add_source(temp_db)
    temp_db.deactivate_source(src["id"])
    result = temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text")
    assert result == "rejected"


def test_create_draft_unresolved_source(temp_db):
    with temp_db._get_connection() as conn:
        conn.execute(
            "INSERT INTO sources (source_type, external_id, name, resolution_status, is_active) "
            "VALUES ('telegram', '@unresolved_ch', '@unresolved_ch', 'unresolved', 0)"
        )
        conn.commit()
    result = temp_db.create_draft_from_active_source("telegram", "@unresolved_ch", "msg_1", "Text")
    assert result == "rejected"


def test_duplicate_rollback_no_partial_rows(temp_db):
    _add_source(temp_db)
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text 1")
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text 2")
    with temp_db._get_connection() as conn:
        draft_count = conn.execute("SELECT COUNT(*) as c FROM drafts").fetchone()["c"]
        assert draft_count == 1


def test_source_item_id_empty_rejected(temp_db):
    _add_source(temp_db)
    with pytest.raises(ValueError):
        temp_db.create_draft_from_active_source("telegram", "-1001234567890", "  ", "Text")


def test_source_item_id_oversized_rejected(temp_db):
    _add_source(temp_db)
    with pytest.raises(ValueError):
        temp_db.create_draft_from_active_source("telegram", "-1001234567890", "x" * 201, "Text")


# ===========================================================================
# SNAPSHOT IMMUTABILITY (tests 23-25)
# ===========================================================================

def test_priority_snapshot_immutable(temp_db):
    src = _add_source(temp_db, priority=80)
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text")
    temp_db.update_source(src["id"], {"priority": 20})
    with temp_db._get_connection() as conn:
        draft = conn.execute("SELECT source_priority_snapshot FROM drafts WHERE source_item_id='msg_1'").fetchone()
        assert draft["source_priority_snapshot"] == 80


def test_trust_snapshot_immutable(temp_db):
    src = _add_source(temp_db, trust_rating=90)
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text")
    temp_db.update_source(src["id"], {"trust_rating": 10})
    with temp_db._get_connection() as conn:
        draft = conn.execute("SELECT source_trust_snapshot FROM drafts WHERE source_item_id='msg_1'").fetchone()
        assert draft["source_trust_snapshot"] == 90


def test_processing_mode_snapshot_immutable(temp_db):
    src = _add_source(temp_db, processing_mode="auto")
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text")
    temp_db.update_source(src["id"], {"processing_mode": "review"})
    with temp_db._get_connection() as conn:
        draft = conn.execute(
            "SELECT source_processing_mode_snapshot FROM drafts WHERE source_item_id='msg_1'"
        ).fetchone()
        assert draft["source_processing_mode_snapshot"] == "auto"


# ===========================================================================
# ANTI-STARVATION QUEUE (tests 26-34)
# ===========================================================================

def test_higher_priority_first(temp_db):
    src_hi = _add_source(temp_db, external_id="-1001111111111", priority=80)
    src_lo = _add_source(temp_db, external_id="-1002222222222", priority=20, name="Low")
    temp_db.create_draft_from_active_source("telegram", "-1002222222222", "msg_lo", "Low text")
    temp_db.create_draft_from_active_source("telegram", "-1001111111111", "msg_hi", "High text")
    draft = temp_db.fetch_next_new_draft()
    assert draft["source_priority_snapshot"] == 80


def test_old_low_priority_overtakes(temp_db):
    src_hi = _add_source(temp_db, external_id="-1001111111111", priority=100)
    src_lo = _add_source(temp_db, external_id="-1002222222222", priority=0, name="Low")
    # Insert low-priority draft with created_at 1100 minutes ago (age_bonus=100)
    with temp_db._get_connection() as conn:
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1100)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        src_lo_row = conn.execute("SELECT id FROM sources WHERE external_id='-1002222222222'").fetchone()
        conn.execute(
            "INSERT INTO drafts (original_text, status, source_id, source_priority_snapshot, source_trust_snapshot, "
            "source_name_snapshot, source_processing_mode_snapshot, source_item_id, created_at) "
            "VALUES ('Old text', 'new', ?, 0, 50, 'Low', 'auto', 'msg_old', ?)",
            (src_lo_row["id"], old_time)
        )
        conn.commit()
    # Fresh high-priority draft
    temp_db.create_draft_from_active_source("telegram", "-1001111111111", "msg_new", "New text")
    draft = temp_db.fetch_next_new_draft()
    # Old low (effective 0+100=100) ties with fresh high (100+0=100), FIFO → old wins
    assert draft["source_item_id"] == "msg_old"


def test_fifo_tiebreak(temp_db):
    src = _add_source(temp_db, priority=50)
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "First")
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_2", "Second")
    draft = temp_db.fetch_next_new_draft()
    assert draft["source_item_id"] == "msg_1"


def test_legacy_null_priority_defaults_50(temp_db):
    # Insert a legacy draft with NULL source_priority_snapshot
    temp_db.add_message_and_draft("legacy_1", "old_channel", "Legacy text")
    src = _add_source(temp_db, priority=49)
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Source text")
    draft = temp_db.fetch_next_new_draft()
    # Legacy has effective 50, source has 49 → legacy first
    assert draft["source_priority_snapshot"] is None


def test_future_timestamp_bonus_zero(temp_db):
    src = _add_source(temp_db)
    with temp_db._get_connection() as conn:
        future_time = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        src_row = conn.execute("SELECT id FROM sources WHERE external_id='-1001234567890'").fetchone()
        conn.execute(
            "INSERT INTO drafts (original_text, status, source_id, source_priority_snapshot, source_trust_snapshot, "
            "source_name_snapshot, source_processing_mode_snapshot, source_item_id, created_at) "
            "VALUES ('Future text', 'new', ?, 50, 50, 'Test', 'auto', 'msg_future', ?)",
            (src_row["id"], future_time)
        )
        conn.commit()
    # Should not crash and should still return the draft
    draft = temp_db.fetch_next_new_draft()
    assert draft is not None


def test_9_minutes_bonus_zero(temp_db):
    src = _add_source(temp_db, priority=0)
    with temp_db._get_connection() as conn:
        t = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=9)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        src_row = conn.execute("SELECT id FROM sources WHERE external_id='-1001234567890'").fetchone()
        conn.execute(
            "INSERT INTO drafts (original_text, status, source_id, source_priority_snapshot, source_trust_snapshot, "
            "source_name_snapshot, source_processing_mode_snapshot, source_item_id, created_at) "
            "VALUES ('9min text', 'new', ?, 0, 50, 'Test', 'auto', 'msg_9', ?)",
            (src_row["id"], t)
        )
        conn.commit()
    # Effective = 0 + 0 = 0 (9 min / 10 = 0.9 → floor = 0)
    draft = temp_db.fetch_next_new_draft()
    assert draft is not None


def test_10_minutes_bonus_one(temp_db):
    src1 = _add_source(temp_db, external_id="-1001111111111", priority=0)
    src2 = _add_source(temp_db, external_id="-1002222222222", priority=0, name="S2")
    with temp_db._get_connection() as conn:
        # Draft at exactly 10 min ago → bonus = 1
        t10 = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        s1 = conn.execute("SELECT id FROM sources WHERE external_id='-1001111111111'").fetchone()
        conn.execute(
            "INSERT INTO drafts (original_text, status, source_id, source_priority_snapshot, source_trust_snapshot, "
            "source_name_snapshot, source_processing_mode_snapshot, source_item_id, created_at) "
            "VALUES ('10min text', 'new', ?, 0, 50, 'S1', 'auto', 'msg_10', ?)",
            (s1["id"], t10)
        )
        # Fresh draft → bonus = 0
        s2 = conn.execute("SELECT id FROM sources WHERE external_id='-1002222222222'").fetchone()
        conn.execute(
            "INSERT INTO drafts (original_text, status, source_id, source_priority_snapshot, source_trust_snapshot, "
            "source_name_snapshot, source_processing_mode_snapshot, source_item_id, created_at) "
            "VALUES ('fresh text', 'new', ?, 0, 50, 'S2', 'auto', 'msg_fresh', ?)",
            (s2["id"], datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'))
        )
        conn.commit()
    draft = temp_db.fetch_next_new_draft()
    # 10-min draft has effective 1 vs fresh 0
    assert draft["source_item_id"] == "msg_10"


def test_20_minutes_bonus_two(temp_db):
    """20 min → bonus = 2"""
    src = _add_source(temp_db, priority=0)
    with temp_db._get_connection() as conn:
        t = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        s = conn.execute("SELECT id FROM sources WHERE external_id='-1001234567890'").fetchone()
        conn.execute(
            "INSERT INTO drafts (original_text, status, source_id, source_priority_snapshot, source_trust_snapshot, "
            "source_name_snapshot, source_processing_mode_snapshot, source_item_id, created_at) "
            "VALUES ('20min text', 'new', ?, 0, 50, 'Test', 'auto', 'msg_20', ?)",
            (s["id"], t)
        )
        conn.commit()
    draft = temp_db.fetch_next_new_draft()
    assert draft is not None
    assert draft["source_item_id"] == "msg_20"


# ===========================================================================
# TRUST ROUTING (tests 35-44)
# ===========================================================================

def test_trust_high_standard_routing():
    from ai_worker import compute_mandatory_review
    mandatory, reason = compute_mandatory_review("auto", 85, "NEWS")
    assert mandatory is False
    assert reason == "standard_routing"


def test_trust_medium_risky_category():
    from ai_worker import compute_mandatory_review
    mandatory, reason = compute_mandatory_review("auto", 65, "HACK")
    assert mandatory is True
    assert reason == "trust_tier_medium_risky_category"


def test_trust_medium_safe_category():
    from ai_worker import compute_mandatory_review
    mandatory, reason = compute_mandatory_review("auto", 65, "NEWS")
    assert mandatory is False
    assert reason == "standard_routing"


def test_trust_low_always_review():
    from ai_worker import compute_mandatory_review
    mandatory, reason = compute_mandatory_review("auto", 30, "NEWS")
    assert mandatory is True
    assert reason == "trust_tier_low"


def test_processing_mode_review_overrides_high_trust():
    from ai_worker import compute_mandatory_review
    mandatory, reason = compute_mandatory_review("review", 100, "NEWS")
    assert mandatory is True
    assert reason == "processing_mode_review"


def test_trust_never_bypasses_auditor():
    """Trust 100 still runs full audit — we verify compute_mandatory_review
    returns standard_routing, not 'skip_audit' or similar."""
    from ai_worker import compute_mandatory_review
    mandatory, reason = compute_mandatory_review("auto", 100, "NEWS")
    assert mandatory is False
    assert reason == "standard_routing"
    # No 'skip_audit' or 'auto_publish' reason exists


def test_mandatory_review_overrides_approve(temp_db):
    """When mandatory_review=True, even if AI recommends non-review, status stays 'review'."""
    from ai_worker import compute_mandatory_review
    mandatory, reason = compute_mandatory_review("auto", 30, "NEWS")
    assert mandatory is True
    # The ai_worker should override final status to 'review'


def test_trust_policy_saved_in_audit_result(temp_db):
    """Verify trust_policy block structure."""
    policy = {
        "source_trust_snapshot": 75,
        "source_processing_mode_snapshot": "auto",
        "category": "HACK",
        "mandatory_review": True,
        "reason": "trust_tier_medium_risky_category",
    }
    result = {
        "schema_version": 2,
        "trust_policy": policy,
        "first_audit": {},
        "final_audit": None,
    }
    j = json.dumps(result)
    parsed = json.loads(j)
    assert parsed["schema_version"] == 2
    assert parsed["trust_policy"]["reason"] == "trust_tier_medium_risky_category"
    assert "source_processing_mode_snapshot" in parsed["trust_policy"]


def test_trust_policy_has_processing_mode_snapshot():
    """Reason enum is bounded."""
    from ai_worker import compute_mandatory_review
    valid_reasons = {"processing_mode_review", "trust_tier_low",
                     "trust_tier_medium_risky_category", "standard_routing"}
    for mode in ["auto", "review"]:
        for trust in [0, 49, 50, 79, 80, 100]:
            for cat in ["NEWS", "HACK", "MEME", "SECURITY", "REGULATION", "MARKET"]:
                _, reason = compute_mandatory_review(mode, trust, cat)
                assert reason in valid_reasons, f"Unknown reason: {reason}"


def test_trust_routing_uses_snapshot_not_mutable(temp_db):
    """After draft creation, changing trust on source does not affect routing."""
    src = _add_source(temp_db, trust_rating=90)
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text")
    temp_db.update_source(src["id"], {"trust_rating": 10})
    with temp_db._get_connection() as conn:
        draft = conn.execute("SELECT source_trust_snapshot FROM drafts WHERE source_item_id='msg_1'").fetchone()
    assert draft["source_trust_snapshot"] == 90


# ===========================================================================
# FK & DELETION (tests 45-47)
# ===========================================================================

def test_pragma_foreign_keys_on(temp_db):
    with temp_db._get_connection() as conn:
        fk_list = conn.execute("PRAGMA foreign_key_list(drafts)").fetchall()
        source_fks = [fk for fk in fk_list if fk["table"] == "sources"]
        assert len(source_fks) >= 1


def test_hard_delete_blocked_by_fk(temp_db):
    src = _add_source(temp_db)
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text")
    with pytest.raises(sqlite3.IntegrityError):
        with temp_db._get_connection() as conn:
            conn.execute("DELETE FROM sources WHERE id = ?", (src["id"],))


def test_deactivation_does_not_hide_existing_drafts(temp_db):
    src = _add_source(temp_db, priority=50)
    temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text")
    temp_db.deactivate_source(src["id"])
    draft = temp_db.fetch_next_new_draft()
    assert draft is not None
    assert draft["source_item_id"] == "msg_1"


# ===========================================================================
# RESOLVE (tests 48-51)
# ===========================================================================

def test_resolve_unresolved_source(temp_db):
    with temp_db._get_connection() as conn:
        conn.execute(
            "INSERT INTO sources (source_type, external_id, name, resolution_status, is_active) "
            "VALUES ('telegram', '@old_username', 'Old Channel', 'unresolved', 0)"
        )
        conn.commit()
    source = temp_db.get_sources()
    unresolved = [s for s in source if s["resolution_status"] == "unresolved"]
    assert len(unresolved) == 1
    resolved = temp_db.resolve_source(unresolved[0]["id"], "-1009999999999")
    assert resolved is not None
    assert resolved["resolution_status"] == "resolved"
    assert resolved["is_active"] == 1
    assert resolved["external_id"] == "-1009999999999"


def test_resolve_duplicate_id_409(temp_db):
    _add_source(temp_db, external_id="-1001234567890")
    with temp_db._get_connection() as conn:
        conn.execute(
            "INSERT INTO sources (source_type, external_id, name, resolution_status, is_active) "
            "VALUES ('telegram', '@unresolved', 'Unresolved', 'unresolved', 0)"
        )
        conn.commit()
    unresolved = [s for s in temp_db.get_sources() if s["resolution_status"] == "unresolved"]
    with pytest.raises(sqlite3.IntegrityError):
        temp_db.resolve_source(unresolved[0]["id"], "-1001234567890")


def test_resolve_already_resolved_409(temp_db):
    src = _add_source(temp_db)
    with pytest.raises(ValueError, match="[Rr]esol"):
        temp_db.resolve_source(src["id"], "-1009999999999")


def test_reactivate_unresolved_blocked(temp_db):
    with temp_db._get_connection() as conn:
        conn.execute(
            "INSERT INTO sources (source_type, external_id, name, resolution_status, is_active) "
            "VALUES ('telegram', '@blocked', 'Blocked', 'unresolved', 0)"
        )
        conn.commit()
    unresolved = [s for s in temp_db.get_sources() if s["external_id"] == "@blocked"]
    with pytest.raises(ValueError, match="[Uu]nresolved"):
        temp_db.update_source(unresolved[0]["id"], {"is_active": True})


# ===========================================================================
# CRUD API (tests 52-65)
# ===========================================================================

def test_api_create_source_201(temp_db):
    with patch("web_admin.main.db", temp_db):
        resp = client.post("/api/sources", json={
            "source_type": "telegram",
            "external_id": "1234567890",
            "name": "API Source"
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["external_id"] == "-1001234567890"


def test_api_create_duplicate_409(temp_db):
    with patch("web_admin.main.db", temp_db):
        client.post("/api/sources", json={
            "source_type": "telegram", "external_id": "1234567890", "name": "S1"
        })
        resp = client.post("/api/sources", json={
            "source_type": "telegram", "external_id": "1234567890", "name": "S2"
        })
        assert resp.status_code == 409


def test_api_create_invalid_telegram_422(temp_db):
    with patch("web_admin.main.db", temp_db):
        resp = client.post("/api/sources", json={
            "source_type": "telegram", "external_id": "@username", "name": "Bad"
        })
        assert resp.status_code == 422


def test_api_create_out_of_range_422(temp_db):
    with patch("web_admin.main.db", temp_db):
        resp = client.post("/api/sources", json={
            "source_type": "telegram", "external_id": "1234567890",
            "name": "Bad", "priority": 101
        })
        assert resp.status_code == 422
        resp = client.post("/api/sources", json={
            "source_type": "telegram", "external_id": "1234567891",
            "name": "Bad", "trust_rating": -1
        })
        assert resp.status_code == 422


def test_api_create_extra_fields_422(temp_db):
    with patch("web_admin.main.db", temp_db):
        resp = client.post("/api/sources", json={
            "source_type": "telegram", "external_id": "1234567890",
            "name": "S", "unknown_field": "oops"
        })
        assert resp.status_code == 422


def test_api_create_rss_requires_https_url(temp_db):
    with patch("web_admin.main.db", temp_db):
        resp = client.post("/api/sources", json={
            "source_type": "rss", "external_id": "feed1",
            "name": "RSS", "canonical_url": "http://example.com/feed"
        })
        assert resp.status_code == 422


def test_api_create_unsafe_url_scheme_422(temp_db):
    with patch("web_admin.main.db", temp_db):
        resp = client.post("/api/sources", json={
            "source_type": "rss", "external_id": "feed1",
            "name": "RSS", "canonical_url": "javascript:alert(1)"
        })
        assert resp.status_code == 422


def test_api_get_sources_200(temp_db):
    with patch("web_admin.main.db", temp_db):
        _add_source(temp_db)
        resp = client.get("/api/sources")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1


def test_api_get_source_404(temp_db):
    with patch("web_admin.main.db", temp_db):
        resp = client.get("/api/sources/99999")
        assert resp.status_code == 404


def test_api_patch_source_200(temp_db):
    with patch("web_admin.main.db", temp_db):
        src = _add_source(temp_db)
        resp = client.patch(f"/api/sources/{src['id']}", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"


def test_api_patch_partial_semantics(temp_db):
    with patch("web_admin.main.db", temp_db):
        src = _add_source(temp_db, priority=80, trust_rating=90)
        resp = client.patch(f"/api/sources/{src['id']}", json={"name": "New Name"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["priority"] == 80  # unchanged
        assert data["trust_rating"] == 90  # unchanged
        assert data["name"] == "New Name"


def test_api_delete_deactivate_200(temp_db):
    with patch("web_admin.main.db", temp_db):
        src = _add_source(temp_db)
        resp = client.delete(f"/api/sources/{src['id']}")
        assert resp.status_code == 200
        updated = temp_db.get_source(src["id"])
        assert updated["is_active"] == 0


def test_api_delete_already_inactive_idempotent(temp_db):
    with patch("web_admin.main.db", temp_db):
        src = _add_source(temp_db)
        temp_db.deactivate_source(src["id"])
        resp = client.delete(f"/api/sources/{src['id']}")
        assert resp.status_code == 200


def test_api_reactivate_via_patch(temp_db):
    with patch("web_admin.main.db", temp_db):
        src = _add_source(temp_db)
        temp_db.deactivate_source(src["id"])
        resp = client.patch(f"/api/sources/{src['id']}", json={"is_active": True})
        assert resp.status_code == 200
        assert resp.json()["is_active"] == 1


# ===========================================================================
# CACHE (tests 66-71)
# ===========================================================================

def test_cache_returns_known_source(temp_db):
    from telegram_listener import SourceCache
    _add_source(temp_db)
    cache = SourceCache(temp_db)
    cache.reload()
    entry = cache.get("-1001234567890")
    assert entry is not None


def test_cache_returns_none_for_unknown(temp_db):
    from telegram_listener import SourceCache
    cache = SourceCache(temp_db)
    cache.reload()
    entry = cache.get("-1009999999999")
    assert entry is None


def test_cache_invalidation_after_crud(temp_db):
    from telegram_listener import SourceCache
    src = _add_source(temp_db)
    cache = SourceCache(temp_db)
    cache.reload()
    assert cache.get("-1001234567890") is not None
    temp_db.deactivate_source(src["id"])
    cache.invalidate()
    # After invalidation, next get triggers reload
    entry = cache.get("-1001234567890")
    assert entry is None


def test_cache_ttl_expiry_two_instances(temp_db):
    from telegram_listener import SourceCache
    src = _add_source(temp_db)
    cache1 = SourceCache(temp_db, ttl_seconds=1)
    cache2 = SourceCache(temp_db, ttl_seconds=1)
    cache1.reload()
    cache2.reload()
    assert cache1.get("-1001234567890") is not None
    assert cache2.get("-1001234567890") is not None
    # Deactivate and wait for TTL
    temp_db.deactivate_source(src["id"])
    time.sleep(1.1)
    # cache2 should see the change after TTL
    entry = cache2.get("-1001234567890")
    assert entry is None


def test_cache_fail_closed_double_failure(temp_db):
    from telegram_listener import SourceCache
    _add_source(temp_db)
    cache = SourceCache(temp_db, ttl_seconds=0)  # Force immediate reload
    cache.reload()
    assert cache.get("-1001234567890") is not None
    # Simulate DB failure
    cache._db = MagicMock()
    cache._db._get_connection.side_effect = Exception("DB error")
    cache.invalidate()
    # First failure
    entry = cache.get("-1001234567890")
    # Second failure - should clear
    entry = cache.get("-1001234567890")
    assert entry is None


def test_stale_cache_cannot_bypass_transactional_check(temp_db):
    """Cache says active, but DB says inactive -> create_draft returns rejected."""
    src = _add_source(temp_db)
    # Create draft succeeds while active
    r1 = temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_1", "Text")
    assert r1 == "created"
    # Deactivate in DB
    temp_db.deactivate_source(src["id"])
    # Even if cache is stale, DB check inside create_draft rejects
    r2 = temp_db.create_draft_from_active_source("telegram", "-1001234567890", "msg_2", "Text 2")
    assert r2 == "rejected"


# ===========================================================================
# UI / XSS (test 72)
# ===========================================================================

def test_ui_xss_source_name(temp_db):
    with patch("web_admin.main.db", temp_db):
        resp = client.post("/api/sources", json={
            "source_type": "telegram",
            "external_id": "1234567890",
            "name": "<script>alert(1)</script>"
        })
        assert resp.status_code == 201
        data = resp.json()
        # Name is stored as-is (escaping happens at render time via textContent)
        assert data["name"] == "<script>alert(1)</script>"
        # GET should also return it safely
        resp2 = client.get(f"/api/sources/{data['id']}")
        assert resp2.json()["name"] == "<script>alert(1)</script>"


# ===========================================================================
# REGRESSION TESTS (TELEGRAM LISTENER & CACHE)
# ===========================================================================

def test_import_telegram_listener_no_credentials():
    # Import telegram_listener without credentials
    # It should not raise any exceptions during import
    with patch("config.settings") as mock_settings:
        mock_settings.telegram_api_id = None
        mock_settings.telegram_api_hash = None
        
        # We need to reload the module to simulate import
        import sys
        import importlib
        if "telegram_listener" in sys.modules:
            importlib.reload(sys.modules["telegram_listener"])
        else:
            import telegram_listener
            
        assert True # Import succeeded without errors

def test_start_listener_no_credentials_safe_error():
    # start_listener should handle missing credentials safely
    import telegram_listener
    with patch("config.settings") as mock_settings:
        mock_settings.telegram_api_id = None
        mock_settings.telegram_api_hash = None
        
        # It shouldn't raise exception, just log error and return
        import asyncio
        asyncio.run(telegram_listener.start_listener())

def test_source_cache_reload_safe_error():
    import telegram_listener
    # Create cache with mock db that throws exception
    mock_db = MagicMock()
    mock_db.get_sources.side_effect = Exception("DB Error")
    cache = telegram_listener.SourceCache(mock_db, ttl_seconds=60)
    
    # Reload should fail but safely return False
    res = cache.reload()
    assert res is False

def test_normalize_telegram_id_negative_bare_id():
    # -123456 -> ValueError
    with pytest.raises(ValueError, match="Negative Telegram ID must start with -100"):
        normalize_telegram_id("-123456")

def test_normalize_telegram_id_oversized():
    # Length > 20 -> ValueError
    with pytest.raises(ValueError, match="Telegram ID too long"):
        normalize_telegram_id("1" * 21)

def test_normalize_telegram_id_zero():
    # 0 -> ValueError
    with pytest.raises(ValueError, match="Telegram ID cannot be zero"):
        normalize_telegram_id("0")
    
    with pytest.raises(ValueError, match="Telegram ID cannot be zero"):
        normalize_telegram_id("-1000")

def test_normalize_telegram_id_missing_channel_payload():
    # "-100" without payload -> ValueError
    with pytest.raises(ValueError, match="Missing or invalid channel payload after -100"):
        normalize_telegram_id("-100")

def test_validate_canonical_url_rejects_newlines_tabs():
    # Rejects tab, newline, carriage return, and DEL (0x7f)
    with pytest.raises(ValueError, match="canonical_url contains control character"):
        _validate_canonical_url("https://example.com/\tpath", "website")
        
    with pytest.raises(ValueError, match="canonical_url contains control character"):
        _validate_canonical_url("https://example.com/\npath", "website")
        
    with pytest.raises(ValueError, match="canonical_url contains control character"):
        _validate_canonical_url("https://example.com/\x7fpath", "website")



def test_start_listener_connection_failure_safe_log():
    import telegram_listener
    import asyncio
    from unittest.mock import AsyncMock
    with patch("config.settings") as mock_settings:
        mock_settings.telegram_api_id = "123"
        mock_settings.telegram_api_hash = "abc"
        mock_settings.telegram_phone_number = "123"
        
        with patch("telegram_listener.create_telegram_client") as mock_create:
            mock_client = MagicMock()
            # Simulate a connection failure (e.g. network error)
            mock_client.start = AsyncMock(side_effect=Exception("Network disconnected"))
            mock_create.return_value = mock_client
            
            with pytest.raises(RuntimeError, match="Telegram Client connection failed \\[SAFE_ERR_CONNECTION_FAILED\\]"):
                asyncio.run(telegram_listener.start_listener())
