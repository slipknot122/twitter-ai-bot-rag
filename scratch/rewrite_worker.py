import sys
import json

with open('ai_worker.py', 'r', encoding='utf-8') as f:
    content = f.read()

import_addition = '''import asyncio
import json
from typing import Literal, Tuple
'''

content = content.replace('import asyncio\nimport json\n', import_addition)

functions_to_add = '''
TrustPolicyReason = Literal[
    "processing_mode_review",
    "trust_tier_low",
    "trust_tier_medium_risky_category",
    "standard_routing",
]

def compute_mandatory_review(
    processing_mode_snapshot: str,
    trust_snapshot: int,
    category: str,
) -> Tuple[bool, TrustPolicyReason]:
    RISKY = {"HACK", "SECURITY", "REGULATION", "MEME"}

    if processing_mode_snapshot == "review":
        return True, "processing_mode_review"
    if trust_snapshot < 50:
        return True, "trust_tier_low"
    if trust_snapshot < 80 and category in RISKY:
        return True, "trust_tier_medium_risky_category"
    return False, "standard_routing"

def _build_audit_result_json(first_audit, final_audit, trust_snapshot, processing_mode_snapshot, category, mandatory_review, reason):
    base = {
        "first_audit": first_audit.model_dump() if first_audit else None,
        "final_audit": final_audit.model_dump() if final_audit else None
    }
    if trust_snapshot is not None and processing_mode_snapshot is not None:
        base["schema_version"] = 2
        base["trust_policy"] = {
            "source_trust_snapshot": trust_snapshot,
            "source_processing_mode_snapshot": processing_mode_snapshot,
            "category": category,
            "mandatory_review": mandatory_review,
            "reason": reason
        }
    else:
        base["schema_version"] = 1
        
    return json.dumps(base)
'''

content = content.replace('def _revise_draft(', functions_to_add + '\ndef _revise_draft(')

content = content.replace("original_text = draft['original_text']", '''original_text = draft['original_text']
    trust_snapshot = draft.get('source_trust_snapshot')
    processing_mode_snapshot = draft.get('source_processing_mode_snapshot')''')

content = content.replace('''            # Good first audit
            audit_result_json = json.dumps({
                "schema_version": 1,
                "first_audit": first_audit.model_dump(),
                "final_audit": None
            })
            media_req = settings.media_generation_enabled and not settings.twitter_dry_run and bool(image_prompt)
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text,
                "audit_status": "passed",
                "audit_decision": first_audit.recommendation,''', 
'''            # Good first audit
            mandatory_review = False
            reason = "standard_routing"
            if trust_snapshot is not None and processing_mode_snapshot is not None:
                mandatory_review, reason = compute_mandatory_review(processing_mode_snapshot, trust_snapshot, category)
            
            audit_result_json = _build_audit_result_json(first_audit, None, trust_snapshot, processing_mode_snapshot, category, mandatory_review, reason)
            
            final_decision = "REVIEW" if mandatory_review else first_audit.recommendation
            media_req = settings.media_generation_enabled and not settings.twitter_dry_run and bool(image_prompt)
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text,
                "audit_status": "passed",
                "audit_decision": final_decision,''')


content = content.replace('''            # Revision failed or text invalid. Fallback to candidate and first audit
            safe_code = classify_safe_error(e) if not isinstance(e, ValidationError) else "candidate_validation"
            audit_result_json = json.dumps({
                "schema_version": 1,
                "first_audit": first_audit.model_dump(),
                "final_audit": None
            })
            media_req = settings.media_generation_enabled and not settings.twitter_dry_run and bool(image_prompt)
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text, # Best valid
                "audit_status": "failed",
                "audit_score": None,
                "audit_decision": first_audit.recommendation,''',
'''            # Revision failed or text invalid. Fallback to candidate and first audit
            safe_code = classify_safe_error(e) if not isinstance(e, ValidationError) else "candidate_validation"
            mandatory_review = False
            reason = "standard_routing"
            if trust_snapshot is not None and processing_mode_snapshot is not None:
                mandatory_review, reason = compute_mandatory_review(processing_mode_snapshot, trust_snapshot, category)
            audit_result_json = _build_audit_result_json(first_audit, None, trust_snapshot, processing_mode_snapshot, category, mandatory_review, reason)
            final_decision = "REVIEW" if mandatory_review else first_audit.recommendation
            media_req = settings.media_generation_enabled and not settings.twitter_dry_run and bool(image_prompt)
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text, # Best valid
                "audit_status": "failed",
                "audit_score": None,
                "audit_decision": final_decision,''')


content = content.replace('''            # Second audit failed technically, fallback to the original candidate
            audit_result_json = json.dumps({
                "schema_version": 1,
                "first_audit": first_audit.model_dump(),
                "final_audit": None
            })
            media_req = settings.media_generation_enabled and not settings.twitter_dry_run and bool(image_prompt)
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text, # fallback to candidate
                "audit_status": "failed",
                "audit_score": None,
                "audit_decision": None,''',
'''            # Second audit failed technically, fallback to the original candidate
            mandatory_review = False
            reason = "standard_routing"
            if trust_snapshot is not None and processing_mode_snapshot is not None:
                mandatory_review, reason = compute_mandatory_review(processing_mode_snapshot, trust_snapshot, category)
            audit_result_json = _build_audit_result_json(first_audit, None, trust_snapshot, processing_mode_snapshot, category, mandatory_review, reason)
            final_decision = "REVIEW" if mandatory_review else None
            media_req = settings.media_generation_enabled and not settings.twitter_dry_run and bool(image_prompt)
            db_instance.complete_ai_processing(draft_id, "review", {
                "rewritten_text": candidate_text, # fallback to candidate
                "audit_status": "failed",
                "audit_score": None,
                "audit_decision": final_decision,''')


content = content.replace('''        # 7. Final processing after second audit
        still_needs_revision = auditor.requires_revision(second_audit, category)
        audit_result_json = json.dumps({
            "schema_version": 1,
            "first_audit": first_audit.model_dump(),
            "final_audit": second_audit.model_dump()
        })
        
        media_req = settings.media_generation_enabled and not settings.twitter_dry_run and bool(image_prompt)
        db_instance.complete_ai_processing(draft_id, "review", {
            "rewritten_text": revised_text,
            "audit_status": "needs_review" if still_needs_revision else "passed",
            "audit_decision": "REVIEW" if still_needs_revision else second_audit.recommendation,''',
'''        # 7. Final processing after second audit
        still_needs_revision = auditor.requires_revision(second_audit, category)
        mandatory_review = False
        reason = "standard_routing"
        if trust_snapshot is not None and processing_mode_snapshot is not None:
            mandatory_review, reason = compute_mandatory_review(processing_mode_snapshot, trust_snapshot, category)
        audit_result_json = _build_audit_result_json(first_audit, second_audit, trust_snapshot, processing_mode_snapshot, category, mandatory_review, reason)
        
        final_decision = "REVIEW" if (still_needs_revision or mandatory_review) else second_audit.recommendation
        media_req = settings.media_generation_enabled and not settings.twitter_dry_run and bool(image_prompt)
        db_instance.complete_ai_processing(draft_id, "review", {
            "rewritten_text": revised_text,
            "audit_status": "needs_review" if still_needs_revision else "passed",
            "audit_decision": final_decision,''')

with open('ai_worker.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('ai_worker.py rewritten successfully')
