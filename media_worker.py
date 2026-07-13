import asyncio
import traceback
from loguru import logger
from database import db
from media_builder import media_builder, TransientMediaError, PermanentMediaError, ContentRejectionError, ProviderAuthError

def process_media_job(draft_id: int, token: str, prompt: str):
    """
    Executes media generation via MediaBuilder.
    Runs inside asyncio.to_thread in the worker loop.
    """
    try:
        # Pass the token for filename generation and containment validation
        metadata = media_builder.generate(draft_id=draft_id, prompt=prompt, token=token)
        
        if metadata:
            # We got a successful image!
            db.complete_media_generation(draft_id=draft_id, token=token, meta=metadata)
        else:
            # All providers failed, but didn't throw an unhandled exception.
            # We treat this as a transient failure for the overarching system if it didn't throw.
            db.fail_media_generation(
                draft_id=draft_id, 
                token=token, 
                error_code="ALL_PROVIDERS_FAILED", 
                error_message="All configured providers failed to generate an image",
                is_transient=True
            )
            
    except TransientMediaError as e:
        logger.warning(f"Media Worker: Transient error for draft {draft_id}")
        db.fail_media_generation(
            draft_id=draft_id,
            token=token,
            error_code="TRANSIENT_ERROR",
            error_message="A transient error occurred during media generation",
            is_transient=True
        )
        
    except (PermanentMediaError, ContentRejectionError, ProviderAuthError) as e:
        logger.error(f"Media Worker: Permanent error for draft {draft_id}")
        db.fail_media_generation(
            draft_id=draft_id,
            token=token,
            error_code=type(e).__name__,
            error_message="A permanent error occurred during media generation",
            is_transient=False
        )
        
    except Exception as e:
        logger.error(f"Media Worker: Unexpected exception for draft {draft_id}")
        db.fail_media_generation(
            draft_id=draft_id,
            token=token,
            error_code="UNEXPECTED_ERROR",
            error_message="An unexpected error occurred",
            is_transient=False
        )


async def media_worker_loop():
    """
    Background worker loop for generating media.
    - Claims pending jobs atomically.
    - Recovers expired jobs.
    - Executes generation in a thread with a timeout.
    """
    logger.info("Media Worker started and waiting for pending media generation requests...")
    
    while True:
        try:
            # 1. Recover expired leases
            count = db.recover_expired_media_jobs()
            if count > 0:
                logger.info(f"Media Worker: Recovered {count} expired media jobs.")

            # 2. Claim next pending job
            # The claim logic sets media_status='generating' and media_lease_expires_at
            # It also returns the new token and job details
            job = db.claim_next_pending_media(timeout_seconds=600)  # 10 minutes lease to be safe
            
            if not job:
                await asyncio.sleep(5)
                continue
                
            draft_id = job['id']
            token = job['media_generation_token']
            prompt = job['image_prompt']
            
            if not prompt:
                # Should not happen if correctly queued, but just in case
                logger.error(f"Media Worker: Job {draft_id} claimed but has no image_prompt. Failing permanently.")
                db.fail_media_generation(draft_id, token, "MISSING_PROMPT", "Image prompt is empty", is_transient=False)
                continue
                
            logger.info(f"Media Worker: Claimed draft {draft_id} for media generation. Attempt {job['media_attempt_count']}")
            
            # 3. Execute job with timeout
            # We use a 9-minute timeout so it aborts before the 10-minute lease expires
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(process_media_job, draft_id, token, prompt),
                    timeout=540
                )
            except asyncio.TimeoutError:
                logger.error(f"Media Worker: Job for draft {draft_id} timed out after 540s.")
                # We update the DB immediately to invalidate token and trigger retry policy
                db.fail_media_generation(
                    draft_id=draft_id,
                    token=token,
                    error_code="TIMEOUT",
                    error_message="Generation timed out",
                    is_transient=True
                )
                
        except asyncio.CancelledError:
            logger.info("Media Worker loop cancelled.")
            raise
        except Exception as e:
            logger.error(f"Media Worker: Loop exception: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(media_worker_loop())
