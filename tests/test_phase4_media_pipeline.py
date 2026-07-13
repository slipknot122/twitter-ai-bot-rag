import pytest
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from database import Database

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test_bot.db"
    db = Database(str(db_path))
    yield db
    # cleanup handled by tmp_path

def test_migration_adds_media_columns(temp_db):
    with temp_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(drafts)")
        columns = {row['name'] for row in cursor.fetchall()}
        
    assert "media_status" in columns
    assert "media_error_code" in columns
    assert "media_generation_token" in columns
    assert "media_attempt_count" in columns
    assert "media_lease_expires_at" in columns

def test_ai_completion_media_request(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft = temp_db.fetch_next_new_draft()
    draft_id = draft["id"]
    
    # Complete with media request
    success = temp_db.complete_ai_processing(
        draft_id, 
        "review", 
        {"rewritten_text": "Rewritten", "image_prompt": "A cool image"},
        media_request=True
    )
    
    assert success
    
    with temp_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, media_status, media_generation_token FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        
    assert row["status"] == "review"
    assert row["media_status"] == "pending"
    assert row["media_generation_token"] is not None

def test_claim_next_pending_media(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    
    temp_db.complete_ai_processing(
        draft_id, "review", {"rewritten_text": "text", "image_prompt": "prompt"}, media_request=True
    )
    
    job = temp_db.claim_next_pending_media(timeout_seconds=600)
    assert job is not None
    assert job["id"] == draft_id
    assert job["media_status"] == "generating"
    assert job["media_attempt_count"] == 1
    assert job["media_lease_expires_at"] is not None
    
    # Second claim should return None because it's generating
    job2 = temp_db.claim_next_pending_media()
    assert job2 is None

def test_fail_media_transient_retry(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    
    job = temp_db.claim_next_pending_media()
    token = job["media_generation_token"]
    
    success = temp_db.fail_media_generation(draft_id, token, "ERR_429", "Rate limit", is_transient=True)
    assert success
    
    with temp_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT media_status, media_attempt_count, media_next_attempt_at FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        
    assert row["media_status"] == "pending"
    assert row["media_attempt_count"] == 1
    assert row["media_next_attempt_at"] is not None
    
    # Because of next_attempt_at in the future, it shouldn't be claimed yet
    assert temp_db.claim_next_pending_media() is None

def test_fail_media_permanent(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    
    job = temp_db.claim_next_pending_media()
    token = job["media_generation_token"]
    
    success = temp_db.fail_media_generation(draft_id, token, "MODERATION", "Blocked", is_transient=False)
    assert success
    
    with temp_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT media_status FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        
    assert row["media_status"] == "failed"

def test_cancel_media_on_publish(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    
    temp_db.approve_draft(draft_id)
    
    # Try to publish text only while media is pending
    pub_draft = temp_db.fetch_next_approved_draft_for_publish()
    assert pub_draft["id"] == draft_id
    
    temp_db.record_publish_success(draft_id, "12345")
    
    with temp_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT media_status, media_generation_token FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        
    # It should have been cancelled
    assert row["media_status"] == "cancelled"
    assert row["media_generation_token"] is None

def test_stale_token_rejection(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    
    job = temp_db.claim_next_pending_media()
    stale_token = job["media_generation_token"]
    
    # Suppose lease expired and it was recovered
    with temp_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE drafts SET media_status = 'pending', media_generation_token = NULL WHERE id = ?", (draft_id,))
        conn.commit()
        
    # Another worker claims it
    job2 = temp_db.claim_next_pending_media()
    new_token = job2["media_generation_token"]
    
    assert stale_token != new_token
    
    # Old worker tries to save
    success = temp_db.complete_media_generation(draft_id, stale_token, {"media_path": "path/1.jpg"})
    assert not success # Should be rejected due to token mismatch
    
    # DB state remains generating with new_token
    with temp_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT media_status, media_generation_token FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        assert row["media_generation_token"] == new_token
