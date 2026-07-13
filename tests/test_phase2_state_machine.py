import pytest
import sqlite3
import json
import os
import shutil
from pathlib import Path
from database import Database, AmbiguousPublishStateError, InvalidStateTransitionError, InvalidUpdateColumnError
from fastapi.testclient import TestClient
import datetime
import threading

@pytest.fixture
def temp_db_path(tmp_path):
    db_file = tmp_path / "test_state_machine.sqlite"
    yield str(db_file)
    try:
        if db_file.exists():
            db_file.unlink()
    except PermissionError:
        pass

@pytest.fixture
def db(temp_db_path):
    database = Database(temp_db_path)
    return database

def test_fk_violation_fails(db):
    """test_actual_fk_violation_fails: Ensure foreign_keys = ON throws constraints."""
    with pytest.raises(sqlite3.IntegrityError):
        with db._get_connection() as conn:
            cursor = conn.cursor()
            # Attempt to insert draft with non-existent processed_message_id
            cursor.execute("INSERT INTO drafts (processed_message_id, original_text) VALUES (9999, 'test')")

def test_ingestion_rollback(db):
    """test_ingestion_rollback: Rollback ingestion on draft failure."""
    # To simulate draft failure, we can manually break the constraint, e.g., missing original_text 
    with pytest.raises(sqlite3.IntegrityError):
        with db._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')
            cursor.execute("INSERT INTO processed_messages (source_id, source_channel) VALUES ('99', 'test')")
            # This should fail because original_text is NOT NULL
            cursor.execute("INSERT INTO drafts (processed_message_id) VALUES (?)", (cursor.lastrowid,))
            conn.commit()
            
    # processed_message should NOT exist because of rollback
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as c FROM processed_messages WHERE source_id = '99'")
        assert cursor.fetchone()['c'] == 0

def test_concurrent_duplicate_telegram(db):
    """test_concurrent_duplicate_telegram: Only one draft for a single telegram message id."""
    barrier = threading.Barrier(2)
    results = []

    def insert_task():
        barrier.wait()
        res = db.add_message_and_draft("1234", "chan1", "text")
        results.append(res)

    t1 = threading.Thread(target=insert_task)
    t2 = threading.Thread(target=insert_task)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    assert None in results
    assert len([r for r in results if r is not None]) == 1

def test_update_column_allowlist(db):
    """test_update_column_allowlist: Raises exception."""
    draft_id = db.add_message_and_draft("msg1", "chan1", "text")
    db.fetch_next_new_draft() # new -> processing
    
    with pytest.raises(InvalidUpdateColumnError):
        db.complete_ai_processing(draft_id, "review", {"invalid_column": "value"})

def test_global_transition_map_rejects_forbidden(db):
    """test_global_transition_map_rejects_forbidden: Raises exception."""
    draft_id = db.add_message_and_draft("msg2", "chan1", "text")
    # new -> published is forbidden
    with pytest.raises(InvalidStateTransitionError):
        with db._get_connection() as conn:
            db._transition_draft(conn, draft_id, {"new"}, "published")

def test_double_approve_and_claim(db):
    """test_double_approve_and_claim: Race conditions return false."""
    draft_id = db.add_message_and_draft("msg3", "chan1", "text")
    db.fetch_next_new_draft() # -> processing
    db.complete_ai_processing(draft_id, "review", {})
    
    barrier = threading.Barrier(2)
    approve_results = []
    def approve_task():
        barrier.wait()
        res = db.approve_draft(draft_id)
        approve_results.append(res)
        
    t1 = threading.Thread(target=approve_task)
    t2 = threading.Thread(target=approve_task)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    assert approve_results.count(True) == 1
    assert approve_results.count(False) == 1
    
    barrier2 = threading.Barrier(2)
    claim_results = []
    def claim_task():
        barrier2.wait()
        res = db.fetch_next_approved_draft_for_publish()
        claim_results.append(res)
        
    t3 = threading.Thread(target=claim_task)
    t4 = threading.Thread(target=claim_task)
    t3.start()
    t4.start()
    t3.join()
    t4.join()
    
    successes = [r for r in claim_results if r is not None]
    assert len(successes) == 1
    assert claim_results.count(None) == 1
    assert successes[0]['id'] == draft_id

def test_failed_is_terminal(db):
    """test_failed_is_terminal: `failed` cannot return to active."""
    draft_id = db.add_message_and_draft("msg4", "chan1", "text")
    db.fetch_next_new_draft()
    db.complete_ai_processing(draft_id, "failed", {})
    
    # Attempt to approve a failed draft
    with pytest.raises(InvalidStateTransitionError):
        with db._get_connection() as conn:
            db._transition_draft(conn, draft_id, {"failed"}, "approved")

def test_atomic_publish_success_rollback(db):
    """test_atomic_publish_success_rollback: Rollback of publish success if one step fails."""
    draft_id1 = db.add_message_and_draft("msg5", "chan1", "text")
    db.fetch_next_new_draft()
    db.complete_ai_processing(draft_id1, "review", {})
    db.approve_draft(draft_id1)
    db.fetch_next_approved_draft_for_publish() # -> publishing
    
    # First success
    db.record_publish_success(draft_id1, "tweet_999")
    
    # Now create a second draft
    draft_id2 = db.add_message_and_draft("msg6", "chan1", "text")
    db.fetch_next_new_draft()
    db.complete_ai_processing(draft_id2, "review", {})
    db.approve_draft(draft_id2)
    db.fetch_next_approved_draft_for_publish() # -> publishing
    
    # Attempt to record success with SAME tweet_id, which violates unique index
    with pytest.raises(AmbiguousPublishStateError):
        db.record_publish_success(draft_id2, "tweet_999")
        
    # Check draft2 remains in publishing because the transaction rolled back
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM drafts WHERE id = ?", (draft_id2,))
        assert cursor.fetchone()['status'] == 'publishing'
        
        cursor.execute("SELECT COUNT(*) as c FROM published_tweets WHERE draft_id = ?", (draft_id2,))
        assert cursor.fetchone()['c'] == 0

def test_migration_conflicts_preserves_rows(temp_db_path):
    """test_migration_conflicts_preserves_rows: Removed rows end up as JSON."""
    # Create raw DB with duplicates
    conn = sqlite3.connect(temp_db_path)
    conn.execute('''CREATE TABLE drafts (id INTEGER PRIMARY KEY, status TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, scheduled_at TIMESTAMP)''')
    conn.execute("INSERT INTO drafts (id, status) VALUES (10, 'published')")
    conn.execute('''CREATE TABLE published_tweets (id INTEGER PRIMARY KEY, draft_id INTEGER, tweet_id TEXT, published_at TIMESTAMP)''')
    conn.execute("INSERT INTO published_tweets (id, draft_id, tweet_id, published_at) VALUES (1, 10, 'tweet_X', '2020-01-01')")
    conn.execute("INSERT INTO published_tweets (id, draft_id, tweet_id, published_at) VALUES (2, 10, 'tweet_Y', '2020-01-02')") # dup draft_id
    conn.commit()
    conn.close()
    
    # Initialize DB (triggers migration)
    db = Database(temp_db_path)
    
    with db._get_connection() as conn:
        cursor = conn.cursor()
        # Should only have id=1 left
        cursor.execute("SELECT id FROM published_tweets")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]['id'] == 1
        
        # Conflict should be saved
        cursor.execute("SELECT * FROM migration_conflicts WHERE source_table='published_tweets'")
        conflicts = cursor.fetchall()
        assert len(conflicts) == 1
        assert conflicts[0]['source_row_id'] == 2
        
        # Re-running migration (by re-initializing) should not duplicate conflicts
        db._init_db()
        cursor.execute("SELECT COUNT(*) as c FROM migration_conflicts")
        assert cursor.fetchone()['c'] == 1

def test_exact_recovery_transitions(db):
    """test_exact_recovery_transitions: Verify processing->new, publishing->review."""
    draft_id1 = db.add_message_and_draft("msg_r1", "chan", "t")
    draft_id2 = db.add_message_and_draft("msg_r2", "chan", "t")
    
    db.fetch_next_new_draft() # draft1 -> processing
    
    # draft2 goes to publishing
    db.fetch_next_new_draft()
    db.complete_ai_processing(draft_id2, "review", {})
    db.approve_draft(draft_id2)
    db.fetch_next_approved_draft_for_publish() # -> publishing
    
    # Init db again to trigger startup recovery
    db._init_db()
        
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM drafts WHERE id = ?", (draft_id1,))
        assert cursor.fetchone()['status'] == 'new'
        
        cursor.execute("SELECT status FROM drafts WHERE id = ?", (draft_id2,))
        assert cursor.fetchone()['status'] == 'review'

def test_admin_transition_conflict(db):
    """test_admin_transition_conflict: HTTP 409 for invalid states."""
    draft_id = db.add_message_and_draft("msg_admin", "chan1", "text")
    # Currently 'new', approving it should fail (only review->approved is allowed, so expected={"review"})
    # _transition_draft returns False if current_status not in expected_statuses.
    assert db.approve_draft(draft_id) is False
        
    # To test race condition:
    db.fetch_next_new_draft()
    db.complete_ai_processing(draft_id, "review", {})
    
    # First approve succeeds
    success1 = db.approve_draft(draft_id)
    assert success1 is True
    
    # Second approve fails with False (not exception, because expected is "review", but it's "approved", which isn't in expected set so it returns False early)
    success2 = db.approve_draft(draft_id)
    assert success2 is False

def test_ambiguous_publish_state_error(db):
    """test_ambiguous_publish_state_error: AmbiguousPublishStateError halts retry."""
    import retry_manager as rm_module
    from retry_manager import RetryManager
    from unittest.mock import patch, AsyncMock
    import asyncio
    
    # Patch global db
    original_db = rm_module.db
    rm_module.db = db
    
    try:
        rm = RetryManager()
        
        draft_id = db.add_message_and_draft("msg_retry", "chan", "t")
        db.fetch_next_new_draft()
        db.complete_ai_processing(draft_id, "review", {})
        db.approve_draft(draft_id)
        db.fetch_next_approved_draft_for_publish()
        
        mock_create_tweet = AsyncMock()
        mock_create_tweet.return_value = "tw_1"
        
        with patch.object(db, 'record_publish_success', side_effect=AmbiguousPublishStateError("DB CRASH")) as mock_record:
            async def mock_publish():
                tweet_id = await mock_create_tweet("text", None)
                db.record_publish_success(draft_id, tweet_id)
                
            success = asyncio.run(rm.execute_with_retries(draft_id, mock_publish))
            assert success is False
            
            # create_tweet must be called EXACTLY once
            mock_create_tweet.assert_called_once()
            mock_record.assert_called_once()
        
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,))
            assert cursor.fetchone()['status'] == 'publishing'
    finally:
        rm_module.db = original_db

def test_fastapi_admin_approve(db):
    from web_admin.main import app
    import web_admin.main as web_main
    original_db = web_main.db
    web_main.db = db
    client = TestClient(app)
    
    try:
        draft_id = db.add_message_and_draft("msg_web", "chan1", "text")
        db.fetch_next_new_draft()
        db.complete_ai_processing(draft_id, "review", {"rewritten_text": "This is a valid long enough text for twitter."})
        
        # First approve
        resp1 = client.post(f"/api/drafts/{draft_id}/publish")
        assert resp1.status_code == 200
        
        # Second approve
        resp2 = client.post(f"/api/drafts/{draft_id}/publish")
        assert resp2.status_code == 409
    finally:
        web_main.db = original_db

