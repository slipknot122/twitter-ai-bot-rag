import pytest
from unittest.mock import patch, MagicMock
from database import Database
from web_admin.main import app
from fastapi.testclient import TestClient
from pathlib import Path
import json

client = TestClient(app)

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test_bot.db"
    db = Database(str(db_path))
    yield db

def test_failed_regeneration_retains_ready_media(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    
    # Simulate first generation success
    job = temp_db.claim_next_pending_media()
    temp_db.complete_media_generation(draft_id, job["media_generation_token"], {"media_path": "path/1.jpg"})
    
    # Verify it is ready
    with temp_db._get_connection() as conn:
        row = conn.execute("SELECT media_status, media_path FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        assert row["media_status"] == "ready"
        assert row["media_path"] == "path/1.jpg"
        
    # Queue regeneration
    temp_db.queue_media_generation(draft_id, action="regenerate", prompt="a valid prompt with enough length over 20 chars")
    
    # Claim and fail it
    job2 = temp_db.claim_next_pending_media()
    temp_db.fail_media_generation(draft_id, job2["media_generation_token"], "ERR", "Failed", is_transient=False)
    
    # Verify it went back to ready and kept the old path
    with temp_db._get_connection() as conn:
        row = conn.execute("SELECT media_status, media_path, media_error_code FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        assert row["media_status"] == "ready"
        assert row["media_path"] == "path/1.jpg"
        assert row["media_error_code"] == "ERR"

def test_api_generate_endpoints(temp_db):
    with patch("web_admin.main.db", temp_db):
        temp_db.add_message_and_draft("123", "telegram", "Original text")
        draft_id = temp_db.fetch_next_new_draft()["id"]
        temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=False)
        
        # 1. no body -> 422
        resp = client.post(f"/api/drafts/{draft_id}/image")
        assert resp.status_code == 422
        
        # 2. invalid prompt length -> 422
        resp = client.post(f"/api/drafts/{draft_id}/image", json={"action": "generate", "image_prompt": "short"})
        assert resp.status_code == 422
        
        # 3. successful generation queue
        resp = client.post(f"/api/drafts/{draft_id}/image", json={"action": "generate", "image_prompt": "a valid prompt with enough length over 20 chars"})
        assert resp.status_code == 202
        
        # 4. conflict (already generating/pending)
        resp = client.post(f"/api/drafts/{draft_id}/image", json={"action": "generate", "image_prompt": "a valid prompt with enough length over 20 chars"})
        assert resp.status_code == 409
        
        # 5. delete success (since it's pending it should be conflict first wait...)
        resp = client.delete(f"/api/drafts/{draft_id}/image")
        assert resp.status_code == 409 # Conflict because it's pending
        
        # Try cancel then delete
        temp_db.cancel_media_generation(draft_id)
        resp = client.delete(f"/api/drafts/{draft_id}/image")
        assert resp.status_code == 200
        
        # 6. delete missing
        resp = client.delete(f"/api/drafts/9999/image")
        assert resp.status_code == 404
        
        # 7. 404 for generate
        resp = client.post(f"/api/drafts/9999/image", json={"action": "generate", "image_prompt": "a valid prompt with enough length over 20 chars"})
        assert resp.status_code == 404

def test_recovery_token_rotation_and_backoff(temp_db):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    
    job = temp_db.claim_next_pending_media()
    token1 = job["media_generation_token"]
    
    # manually expire the lease
    with temp_db._get_connection() as conn:
        conn.execute("UPDATE drafts SET media_lease_expires_at = ?, media_attempt_count = 1 WHERE id = ?", ((now - datetime.timedelta(seconds=1)).isoformat(), draft_id))
        conn.commit()
        
    recovered = temp_db.recover_expired_media_jobs(max_attempts=3)
    assert recovered == 1
    
    with temp_db._get_connection() as conn:
        row = conn.execute("SELECT media_status, media_generation_token, media_attempt_count FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        assert row["media_status"] == "pending"
        assert row["media_generation_token"] != token1
        assert row["media_attempt_count"] == 1
        
    # Set attempts to 3 (max) and expire
    with temp_db._get_connection() as conn:
        conn.execute("UPDATE drafts SET media_status = 'generating', media_lease_expires_at = ?, media_attempt_count = 3 WHERE id = ?", ((now - datetime.timedelta(seconds=1)).isoformat(), draft_id))
        conn.commit()
        
    recovered = temp_db.recover_expired_media_jobs(max_attempts=3)
    assert recovered == 1
    
    with temp_db._get_connection() as conn:
        row = conn.execute("SELECT media_status, media_error_code FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        assert row["media_status"] == "failed"
        assert row["media_error_code"] == "timeout"
