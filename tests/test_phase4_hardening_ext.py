import pytest
import os
import asyncio
from unittest.mock import patch, MagicMock
from database import Database
from web_admin.main import app
from fastapi.testclient import TestClient
from pathlib import Path
from media_worker import process_media_job
from config import settings
from media_builder import media_builder, TransientMediaError, PermanentMediaError, ContentRejectionError

client = TestClient(app)

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test_bot.db"
    db = Database(str(db_path))
    yield db

def test_cancel_api_endpoints(temp_db):
    with patch("web_admin.main.db", temp_db):
        temp_db.add_message_and_draft("123", "telegram", "Original text")
        draft_id = temp_db.fetch_next_new_draft()["id"]
        temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
        
        # Cancel pending
        resp = client.post(f"/api/drafts/{draft_id}/image/cancel")
        assert resp.status_code == 200
        
        # Conflict if cancelled again
        resp = client.post(f"/api/drafts/{draft_id}/image/cancel")
        assert resp.status_code == 409
        
        # 404
        resp = client.post(f"/api/drafts/999/image/cancel")
        assert resp.status_code == 404

def test_transient_failure_changes_token(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    
    job = temp_db.claim_next_pending_media()
    old_token = job["media_generation_token"]
    
    temp_db.fail_media_generation(draft_id, old_token, "TIMEOUT", "Error", is_transient=True)
    
    with temp_db._get_connection() as conn:
        row = conn.execute("SELECT media_generation_token, media_status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        assert row["media_status"] == "pending"
        assert row["media_generation_token"] != old_token
        assert row["media_generation_token"] is not None

def test_old_token_cannot_complete(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    
    job = temp_db.claim_next_pending_media()
    old_token = job["media_generation_token"]
    
    temp_db.fail_media_generation(draft_id, old_token, "TIMEOUT", "Error", is_transient=True)
    
    success = temp_db.complete_media_generation(draft_id, old_token, {"media_path": "path.jpg"})
    assert not success

@patch("media_worker.media_builder.generate")
def test_process_media_job_success(mock_generate, temp_db):
    mock_generate.return_value = {"media_path": "path/test.jpg", "media_provider": "dalle"}
    with patch("media_worker.db", temp_db):
        temp_db.add_message_and_draft("123", "telegram", "Original text")
        draft_id = temp_db.fetch_next_new_draft()["id"]
        temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
        
        job = temp_db.claim_next_pending_media()
        token = job["media_generation_token"]
        
        process_media_job(draft_id, token, "prompt")
        
        with temp_db._get_connection() as conn:
            row = conn.execute("SELECT media_status, media_path FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            assert row["media_status"] == "ready"
            assert row["media_path"] == "path/test.jpg"

@patch("media_worker.media_builder.generate")
@patch("media_worker.media_builder.delete_media_file")
def test_stale_completion_deletes_output(mock_delete, mock_generate, temp_db):
    mock_generate.return_value = {"media_path": "path/test.jpg", "media_provider": "dalle"}
    with patch("media_worker.db", temp_db):
        temp_db.add_message_and_draft("123", "telegram", "Original text")
        draft_id = temp_db.fetch_next_new_draft()["id"]
        temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
        
        job = temp_db.claim_next_pending_media()
        token = job["media_generation_token"]
        
        # Simulate timeout in DB
        temp_db.fail_media_generation(draft_id, token, "TIMEOUT", "Timeout", is_transient=True)
        
        # Process late
        process_media_job(draft_id, token, "prompt")
        
        mock_delete.assert_called_once_with("path/test.jpg")
        
        with temp_db._get_connection() as conn:
            row = conn.execute("SELECT media_status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            assert row["media_status"] == "pending"

def test_ready_media_passed_to_publisher(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    job = temp_db.claim_next_pending_media()
    temp_db.complete_media_generation(draft_id, job["media_generation_token"], {"media_path": "path/test.jpg"})
    
    temp_db.approve_draft(draft_id)
    
    draft = temp_db.fetch_next_approved_draft_for_publish()
    assert draft["media_path"] == "path/test.jpg"

def test_publish_failure_retains_media(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    job = temp_db.claim_next_pending_media()
    temp_db.complete_media_generation(draft_id, job["media_generation_token"], {"media_path": "path/test.jpg"})
    
    temp_db.approve_draft(draft_id)
    
    # record failure
    temp_db.mark_publish_failed(draft_id, "error")
    
    with temp_db._get_connection() as conn:
        row = conn.execute("SELECT media_status, media_path FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        assert row["media_status"] == "ready"
        assert row["media_path"] == "path/test.jpg"

def test_publish_success_text_only_cancels_media(temp_db):
    temp_db.add_message_and_draft("123", "telegram", "Original text")
    draft_id = temp_db.fetch_next_new_draft()["id"]
    temp_db.complete_ai_processing(draft_id, "review", {"rewritten_text": "t"}, media_request=True)
    
    temp_db.approve_draft(draft_id)
    draft = temp_db.fetch_next_approved_draft_for_publish()
    temp_db.record_publish_success(draft_id, "123")
    
    with temp_db._get_connection() as conn:
        row = conn.execute("SELECT media_status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        assert row["media_status"] == "cancelled"

@patch("media_builder.GoogleImagenProvider")
@patch("media_builder.CloudflareProvider")
@patch("media_builder.PollinationsProvider")
def test_media_builder_dry_run(mock_pollinations, mock_cloudflare, mock_google, tmp_path):
    from config import settings
    settings.media_generation_enabled = False
    
    with patch("media_builder.settings", settings):
        res = media_builder.generate(1, "prompt", "token")
        
        assert res is None
        
        mock_google.assert_not_called()
        mock_cloudflare.assert_not_called()
        mock_pollinations.assert_not_called()

def test_path_traversal_and_symlink_rejection():
    # test delete_media_file path traversal check
    from media_builder import media_builder
    
    assert not media_builder.delete_media_file("../../../etc/passwd")
    
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        sym = Path(td) / "link.jpg"
        target = Path(td) / "target.jpg"
        target.write_bytes(b"123")
        try:
            os.symlink(target, sym)
            assert not media_builder.delete_media_file(str(sym))
        except OSError:
            pass # Windows might not support symlinks without admin, skip gracefully

def test_corrupt_image_validation(tmp_path):
    from media_builder import _validate_and_save_image, TransientMediaError
    
    bad_file = tmp_path / "bad.jpg"
    
    with pytest.raises(TransientMediaError, match="Image validation failed"):
        _validate_and_save_image(b"not an image", bad_file, 1024*1024, 1024, 1024)
