import pytest
import sqlite3
from database import Database
from post_auditor import PostAuditor, AuditResult, AuditFailure
import tempfile
import os
import json
from unittest.mock import patch, MagicMock
import ai_worker
from unittest.mock import patch, MagicMock

@pytest.fixture
def test_db():
    temp_db = tempfile.NamedTemporaryFile(delete=False)
    temp_db.close()
    
    db = Database(temp_db.name)
    # Seed a draft
    with db._get_connection() as conn:
        conn.execute("INSERT INTO drafts (id, original_text, status) VALUES (1, 'Test source', 'processing')")
        conn.commit()
    yield db
    
    try:
        os.unlink(temp_db.name)
    except PermissionError:
        pass


def make_valid_audit(factual_fidelity=0.95, overall_score=0.9):
    return AuditResult(
        factual_fidelity=factual_fidelity,
        clarity=0.9,
        hook_strength=0.8,
        originality=0.85,
        persona_match=0.92,
        duplicate_risk=0.1,
        spam_risk=0.05,
        policy_risk=0.1,
        overall_score=overall_score,
        recommendation="APPROVE",
        blocking_issues=[],
        suggestions=["Great job"],
        feedback="All good"
    )

def test_auditor_schema_validation():
    auditor = PostAuditor()
    valid_json = {
        "factual_fidelity": 0.95,
        "clarity": 0.9,
        "hook_strength": 0.8,
        "originality": 0.85,
        "persona_match": 0.92,
        "duplicate_risk": 0.1,
        "spam_risk": 0.05,
        "policy_risk": 0.1,
        "overall_score": 0.88,
        "recommendation": "APPROVE",
        "blocking_issues": [],
        "suggestions": ["Great job"],
        "feedback": "All good"
    }
    
    # Valid
    result = auditor.parse_result(json.dumps(valid_json))
    assert result.factual_fidelity == 0.95
    
    # Missing field
    invalid_json_missing = dict(valid_json)
    del invalid_json_missing['factual_fidelity']
    with pytest.raises(AuditFailure) as exc:
        auditor.parse_result(json.dumps(invalid_json_missing))
    assert exc.value.code == "schema_validation"

    # Out of range
    invalid_json_range = dict(valid_json)
    invalid_json_range['policy_risk'] = 1.5
    with pytest.raises(AuditFailure) as exc:
        auditor.parse_result(json.dumps(invalid_json_range))
    assert exc.value.code == "schema_validation"

    # Additional properties
    invalid_json_extra = dict(valid_json)
    invalid_json_extra['some_extra'] = True
    with pytest.raises(AuditFailure) as exc:
        auditor.parse_result(json.dumps(invalid_json_extra))
    assert exc.value.code == "schema_validation"

def test_deterministic_decision():
    auditor = PostAuditor()
    
    # High LLM recommendation, but low factual
    result_low_factual = make_valid_audit(factual_fidelity=0.85)
    assert auditor.requires_revision(result_low_factual) is True
    
    # Good
    result_good = make_valid_audit(factual_fidelity=0.92)
    assert auditor.requires_revision(result_good) is False
    
    # HACK category needs 0.95
    assert auditor.requires_revision(result_good, category="HACK") is True

@patch("post_auditor.PostAuditor.audit")
@patch("ai_engine.ai_engine.process_text")
def test_good_first_audit(mock_process, mock_audit, test_db):
    mock_process.return_value = {
        "action": "PUBLISH",
        "tweet_text": "First draft",
        "category": "NEWS",
        "sentiment": "Neutral",
        "confidence": 0.9
    }
    mock_audit.return_value = (make_valid_audit(), "mock_model")
    
    # Process
    ai_worker.process_draft(1, test_db)
    
    # Check DB
    draft = test_db.get_draft(1)
    assert draft['status'] == 'review'
    assert draft['audit_status'] == 'passed'
    assert draft['audit_score'] == 0.9
    assert draft['revision_count'] == 0
    
    audit_res = json.loads(draft['audit_result'])
    assert 'first_audit' in audit_res
    assert audit_res['first_audit']['factual_fidelity'] == 0.95
    assert audit_res.get('final_audit') is None

@patch("post_auditor.PostAuditor.audit")
@patch("ai_engine.ai_engine.process_text")
@patch("ai_worker._revise_draft")
def test_bad_first_audit_good_revision(mock_revise, mock_process, mock_audit, test_db):
    mock_process.return_value = {
        "action": "PUBLISH",
        "tweet_text": "First draft",
        "category": "NEWS",
        "sentiment": "Neutral",
        "confidence": 0.9
    }
    mock_revise.return_value = "Revised text"
    
    # First returns bad, Second returns good
    mock_audit.side_effect = [
        (make_valid_audit(factual_fidelity=0.8), "mock_model"), # needs revision
        (make_valid_audit(factual_fidelity=0.96), "mock_model") # passes
    ]
    
    ai_worker.process_draft(1, test_db)
    
    draft = test_db.get_draft(1)
    assert draft['status'] == 'review'
    assert draft['audit_status'] == 'passed'
    assert draft['revision_count'] == 1
    assert draft['rewritten_text'] == "Revised text"
    
    audit_res = json.loads(draft['audit_result'])
    assert audit_res['first_audit']['factual_fidelity'] == 0.8
    assert audit_res['final_audit']['factual_fidelity'] == 0.96

@patch("post_auditor.PostAuditor.audit")
@patch("ai_engine.ai_engine.process_text")
@patch("ai_worker._revise_draft")
def test_bad_second_audit_stops(mock_revise, mock_process, mock_audit, test_db):
    mock_process.return_value = {
        "action": "PUBLISH",
        "tweet_text": "First draft",
        "category": "NEWS",
        "sentiment": "Neutral",
        "confidence": 0.9
    }
    mock_revise.return_value = "Revised text"
    
    # Both audits bad
    mock_audit.side_effect = [
        (make_valid_audit(factual_fidelity=0.8), "mock_model"),
        (make_valid_audit(factual_fidelity=0.85), "mock_model")
    ]
    
    ai_worker.process_draft(1, test_db)
    
    draft = test_db.get_draft(1)
    assert draft['status'] == 'review'
    assert draft['audit_status'] == 'needs_review'
    assert draft['audit_decision'] == 'REVIEW'
    assert draft['revision_count'] == 1
    # Check that third revision was NOT attempted
    assert mock_revise.call_count == 1
    # Check that revised text is preserved
    assert draft['rewritten_text'] == "Revised text"

@patch("post_auditor.PostAuditor.audit")
@patch("ai_engine.ai_engine.process_text")
def test_timeout_first_audit(mock_process, mock_audit, test_db):
    mock_process.return_value = {
        "action": "PUBLISH",
        "tweet_text": "First draft",
        "category": "NEWS",
        "sentiment": "Neutral",
        "confidence": 0.9
    }
    mock_audit.side_effect = AuditFailure("timeout", "API Timeout")
    
    ai_worker.process_draft(1, test_db)
    
    draft = test_db.get_draft(1)
    assert draft['status'] == 'review'
    assert draft['audit_status'] == 'failed'
    assert draft['audit_score'] is None
    assert draft['audit_error_code'] == 'timeout'
    assert draft['rewritten_text'] == "First draft" # Best candidate preserved

@patch("post_auditor.PostAuditor.audit")
@patch("ai_engine.ai_engine.process_text")
@patch("ai_worker._revise_draft")
def test_revision_failure(mock_revise, mock_process, mock_audit, test_db):
    mock_process.return_value = {
        "action": "PUBLISH",
        "tweet_text": "First draft",
        "category": "NEWS",
        "sentiment": "Neutral",
        "confidence": 0.9
    }
    
    # First audit bad
    mock_audit.side_effect = [
        (make_valid_audit(factual_fidelity=0.8), "mock_model"),
        AuditFailure("timeout", "API Timeout on second audit")
    ]
    
    mock_revise.return_value = "Revised text"
    
    ai_worker.process_draft(1, test_db)
    
    draft = test_db.get_draft(1)
    assert draft['status'] == 'review'
    assert draft['audit_status'] == 'failed'
    assert draft['audit_error_code'] == 'timeout'
    assert draft['audit_score'] is None
    # Wait, the instruction says:
    # "збережи початковий валідний candidate; збережи перший audit; audit_status=failed; revision_count відображає лише фактично виконану спробу"
    assert draft['rewritten_text'] == "First draft"
    
def test_migration_idempotency(test_db):
    # Running init_db multiple times shouldn't fail and shouldn't duplicate
    test_db._init_db()
    test_db._init_db() # Second time
    
    with test_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(drafts)")
        columns = [row['name'] for row in cursor.fetchall()]
        
    assert 'audit_status' in columns
    assert 'revision_count' in columns
    # Make sure we didn't duplicate
    # sqlite wouldn't let us but we must catch error gracefully if we didn't use PRAGMA properly

def test_atomic_completion(test_db):
    # Manually test complete_ai_processing
    # Change status to review, add audit_score and text
    updates = {
        "rewritten_text": "Final text",
        "audit_status": "passed",
        "audit_score": 0.95,
        "audit_decision": "APPROVE",
        "audit_result": json.dumps({"first_audit": {}}),
        "audit_error_code": None,
        "revision_count": 0,
        "category": "NEWS",
        "sentiment": "Neutral"
    }
    success = test_db.complete_ai_processing(1, "review", updates)
    assert success is True
    
    draft = test_db.get_draft(1)
    assert draft['status'] == 'review'
    assert draft['audit_status'] == 'passed'
    assert draft['rewritten_text'] == 'Final text'

def test_state_conflict(test_db):
    # Move to 'review' outside
    with test_db._get_connection() as conn:
        conn.execute("UPDATE drafts SET status = 'review' WHERE id = 1")
        conn.commit()
        
    # complete_ai_processing should fail since draft is no longer 'processing'
    updates = {"rewritten_text": "Final text"}
    success = test_db.complete_ai_processing(1, "review", updates)
    assert success is False
