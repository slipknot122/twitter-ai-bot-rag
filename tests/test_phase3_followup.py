import pytest
import tempfile
import json
from unittest.mock import patch, MagicMock

from database import Database
from twitter_publisher import publisher
from web_admin.main import app
from fastapi.testclient import TestClient

@pytest.fixture
def db():
    # Use a temporary file for the database
    fd, path = tempfile.mkstemp()
    test_db = Database(db_path=path)
    yield test_db
    import os
    try:
        os.close(fd)
        os.unlink(path)
    except OSError:
        pass

client = TestClient(app)

def mark_draft_processing(db_instance, draft_id):
    with db_instance._get_connection() as conn:
        db_instance._transition_draft(conn, draft_id, {"new"}, "processing")
        conn.commit()

@patch("twitter_publisher.semantic_memory.save")
def test_semantic_memory_real_publish(mock_save, db):
    draft_id = db.add_message_and_draft("msg_mem", "chan_mem", "text")
    mark_draft_processing(db, draft_id)
    db.complete_ai_processing(draft_id, "approved", {"rewritten_text": "real tweet"})
    db.fetch_next_approved_draft_for_publish()

    with patch.object(publisher, "dry_run", False), \
         patch.object(publisher, "client") as mock_client, \
         patch.object(publisher, "api_v1") as mock_api_v1, \
         patch("twitter_publisher.db", db):
        mock_client.create_tweet.return_value = MagicMock(data={"id": "123"})
        publisher.publish(draft_id, "real tweet")

    mock_save.assert_called_once_with("real tweet")
    mock_client.create_tweet.assert_called_once()
    draft = db.get_draft(draft_id)
    assert draft['status'] == 'published'

@patch("twitter_publisher.semantic_memory.save")
def test_semantic_memory_dry_run(mock_save, db):
    draft_id = db.add_message_and_draft("msg_dry", "chan_dry", "text")
    mark_draft_processing(db, draft_id)
    db.complete_ai_processing(draft_id, "approved", {"rewritten_text": "dry tweet"})
    db.fetch_next_approved_draft_for_publish()

    with patch.object(publisher, "dry_run", True), \
         patch.object(publisher, "client") as mock_client, \
         patch.object(publisher, "api_v1") as mock_api_v1, \
         patch("twitter_publisher.db", db):
        publisher.publish(draft_id, "dry tweet")

    mock_save.assert_not_called()
    mock_client.create_tweet.assert_not_called()
    draft = db.get_draft(draft_id)
    assert draft['status'] == 'published'

@patch("twitter_publisher.semantic_memory.save")
def test_semantic_memory_save_failure_does_not_fail_publish(mock_save, db):
    draft_id = db.add_message_and_draft("msg_mem_fail", "chan_mem_fail", "text")
    mark_draft_processing(db, draft_id)
    db.complete_ai_processing(draft_id, "approved", {"rewritten_text": "real tweet"})
    db.fetch_next_approved_draft_for_publish()

    mock_save.side_effect = Exception("Semantic DB down")

    with patch.object(publisher, "dry_run", False), \
         patch.object(publisher, "client") as mock_client, \
         patch.object(publisher, "api_v1") as mock_api_v1, \
         patch("twitter_publisher.db", db):
        mock_client.create_tweet.return_value = MagicMock(data={"id": "123"})
        publisher.publish(draft_id, "real tweet")

    mock_save.assert_called_once_with("real tweet")
    mock_client.create_tweet.assert_called_once()
    draft = db.get_draft(draft_id)
    assert draft['status'] == 'published'

@pytest.fixture
def override_db(db):
    return db

def test_admin_ui_xss_and_audit(override_db):
    # Insert draft with XSS in text and JSON
    draft_id = override_db.add_message_and_draft("msg_xss", "chan_xss", "text")
    mark_draft_processing(override_db, draft_id)
    audit_json = json.dumps({
        "schema_version": 1,
        "first_audit": {
            "feedback": "<script>alert(1)</script>",
            "recommendation": "REVIEW",
            "overall_score": 0.5,
            "blocking_issues": [],
            "suggestions": []
        },
        "final_audit": None
    })
    override_db.complete_ai_processing(draft_id, "review", {
        "rewritten_text": "<script>alert(2)</script>",
        "audit_result": audit_json,
        "audit_error_code": "safe_code"
    })

    with patch("web_admin.main.db", override_db):
        response = client.get("/")
    assert response.status_code == 200
    html = response.text
    # Should be escaped
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "<script>alert(2)</script>" not in html

def test_admin_ui_invalid_audit_json(override_db):
    draft_id = override_db.add_message_and_draft("msg_json", "chan_json", "text")
    mark_draft_processing(override_db, draft_id)
    override_db.complete_ai_processing(draft_id, "review", {
        "rewritten_text": "text",
        "audit_result": "INVALID_JSON_HERE"
    })
    with patch("web_admin.main.db", override_db):
        response = client.get("/")
    assert response.status_code == 200
    # Should gracefully handle invalid json

def test_admin_ui_legacy_null_audit(override_db):
    draft_id = override_db.add_message_and_draft("msg_legacy", "chan_legacy", "text")
    mark_draft_processing(override_db, draft_id)
    override_db.complete_ai_processing(draft_id, "review", {
        "rewritten_text": "text",
        "audit_result": None
    })
    with patch("web_admin.main.db", override_db):
        response = client.get("/")
    assert response.status_code == 200

import sqlite3

def test_connection_closes_after_success(override_db):
    captured_connection = None
    with override_db._get_connection() as conn:
        captured_connection = conn
        conn.execute("SELECT 1")

    assert captured_connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="(?i)closed"):
        captured_connection.execute("SELECT 1")

def test_connection_closes_after_exception(override_db):
    captured_connection = None
    class ExpectedError(Exception):
        pass

    with pytest.raises(ExpectedError):
        with override_db._get_connection() as conn:
            captured_connection = conn
            raise ExpectedError("boom")

    assert captured_connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="(?i)closed"):
        captured_connection.execute("SELECT 1")
