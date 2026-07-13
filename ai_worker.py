import asyncio
import json
from loguru import logger
from database import db
from ai_engine import ai_engine
from post_auditor import auditor, AuditFailure
from semantic_memory import semantic_memory
from utils import validate_post_text, ValidationError
from config import settings

def _revise_draft(original_text: str, candidate_text: str, blocking_issues: list, suggestions: list) -> str:
    # A placeholder for now, or calling ai_engine._revise
    # Phase 3 requires exact revision logic, but I'll mock it in test.
    prompt = f"<original_source>\n{original_text}\n</original_source>\n\n<candidate_post>\n{candidate_text}\n</candidate_post>\n"
    prompt += "Please revise this draft to address the following issues:\n"
    for issue in blocking_issues:
        prompt += f"- {issue}\n"
    for suggestion in suggestions:
        prompt += f"- {suggestion}\n"
    
    # We use LLM for revision
    try:
        from llm_provider import llm
        response = llm.generate(
            prompt=prompt,
            system_prompt="You are an editor. Fix the issues without adding new unverified facts. Output ONLY the revised text.",
            temperature=0.3
        )
        return response
    except Exception as e:
        logger.error(f"Revision LLM failed: {e}")
        raise

def process_draft(draft_id: int, db_instance):
    """
    Process a single draft for Phase 3:
    generate -> validate -> audit -> optional one revision -> validate -> second audit -> atomic save -> review.
    """
    draft = db_instance.get_draft(draft_id)
    if not draft or draft['status'] != 'processing':
        return

    original_text = draft['original_text']
    logger.info(f"AI Worker: Processing Draft ID {draft_id}")

    try:
        # 1. Generate candidate
        result = ai_engine.process_text(original_text)
        action = result.get('action', 'FAILED')
        candidate_text = result.get('tweet_text', '')
        category = result.get('category', 'NEWS')
        sentiment = result.get('sentiment', 'Neutral')
        image_prompt = result.get('image_prompt', '')
        
        if action in ['IGNORE', 'FAILED'] or not candidate_text:
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text,
                "reason": result.get('reason', action),
                "category": category,
                "sentiment": sentiment,
                "image_prompt": image_prompt,
                "revision_count": 0
            })
            return

        # 2. Validate candidate text
        try:
            candidate_text = validate_post_text(candidate_text)
        except ValidationError as ve:
            logger.warning(f"Draft {draft_id}: Generation validation failed: {ve}")
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text,
                "reason": f"Validation failed: {ve}",
                "category": category,
                "sentiment": sentiment,
                "image_prompt": image_prompt,
                "revision_count": 0
            })
            return

        # 3. First audit
        try:
            first_audit = auditor.audit(original_text, candidate_text, None)
        except AuditFailure as af:
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text, # preserve candidate
                "audit_status": "failed",
                "audit_score": None,
                "audit_error_code": af.code,
                "category": category,
                "sentiment": sentiment,
                "image_prompt": image_prompt,
                "revision_count": 0
            })
            return

        # 4. Determine if revision is needed
        needs_revision = auditor.requires_revision(first_audit, category)
        
        if not needs_revision:
            # Good first audit
            audit_result_json = json.dumps({"first_audit": first_audit.model_dump()})
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text,
                "audit_status": "passed",
                "audit_decision": first_audit.recommendation,
                "audit_score": first_audit.overall_score,
                "audit_result": audit_result_json,
                "audit_error_code": None,
                "category": category,
                "sentiment": sentiment,
                "image_prompt": image_prompt,
                "revision_count": 0
            })
            return
            
        # 5. Revise
        try:
            revised_text = _revise_draft(original_text, candidate_text, first_audit.blocking_issues, first_audit.suggestions)
            revised_text = validate_post_text(revised_text)
        except Exception as e:
            # Revision failed or text invalid. Fallback to candidate and first audit
            audit_result_json = json.dumps({"first_audit": first_audit.model_dump()})
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text, # Best valid
                "audit_status": "failed",
                "audit_score": None,
                "audit_decision": first_audit.recommendation,
                "audit_result": audit_result_json,
                "audit_error_code": "revision_error" if not isinstance(e, ValidationError) else "validation_error",
                "category": category,
                "sentiment": sentiment,
                "image_prompt": image_prompt,
                "revision_count": 1 # we attempted exactly once
            })
            return

        # 6. Second audit
        try:
            second_audit = auditor.audit(original_text, revised_text, None)
        except AuditFailure as af:
            # Second audit failed technically, fallback to the original candidate
            audit_result_json = json.dumps({
                "first_audit": first_audit.model_dump()
            })
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text, # fallback to candidate
                "audit_status": "failed",
                "audit_score": None,
                "audit_decision": None,
                "audit_result": audit_result_json,
                "audit_error_code": af.code,
                "category": category,
                "sentiment": sentiment,
                "image_prompt": image_prompt,
                "revision_count": 1
            })
            return

        # 7. Final processing after second audit
        still_needs_revision = auditor.requires_revision(second_audit, category)
        audit_result_json = json.dumps({
            "first_audit": first_audit.model_dump(),
            "final_audit": second_audit.model_dump()
        })
        
        db_instance.complete_ai_processing(draft_id, "review", {
            "rewritten_text": revised_text,
            "audit_status": "needs_review" if still_needs_revision else "passed",
            "audit_decision": "REVIEW" if still_needs_revision else second_audit.recommendation,
            "audit_score": second_audit.overall_score,
            "audit_result": audit_result_json,
            "audit_error_code": None,
            "category": category,
            "sentiment": sentiment,
            "image_prompt": image_prompt,
            "revision_count": 1
        })
            
    except Exception as e:
        logger.error(f"Error processing draft {draft_id}: {e}")
        db_instance.complete_ai_processing(draft_id, "review", {"last_error": str(e)})

async def ai_worker_loop():
    logger.info("AI Worker started and waiting for new messages in queue...")
    
    while True:
        draft_id = None
        try:
            draft = db.fetch_next_new_draft()
            if not draft:
                await asyncio.sleep(5)
                continue
                
            draft_id = draft['id']
            await asyncio.to_thread(process_draft, draft_id, db)
            
        except Exception as e:
            if draft_id is not None:
                logger.error(f"Error in ai_worker_loop for draft {draft_id}: {e}")
            else:
                logger.error(f"Error in AI worker loop before fetching draft: {e}")
            await asyncio.sleep(5)
            continue

if __name__ == "__main__":
    asyncio.run(ai_worker_loop())
