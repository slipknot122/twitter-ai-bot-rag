import asyncio
import random
from datetime import datetime, timedelta, timezone
from loguru import logger
from database import db
from twitter_publisher import publisher
from retry_manager import retry_manager

# Зафіксований момент наступної публікації (перераховується ОДИН раз після кожного посту)
_next_publish_time: datetime | None = None

def _compute_next_publish_time(last_pub_dt: datetime) -> datetime:
    delay_minutes = db.get_setting("publish_delay_minutes", 45)
    jitter_percent = db.get_setting("publish_jitter_percent", 15)
    base = delay_minutes * 60
    jitter = base * (jitter_percent / 100.0)
    return last_pub_dt + timedelta(seconds=base + random.uniform(-jitter, jitter))

async def scheduler_loop():
    global _next_publish_time
    logger.info("Scheduler started. Waiting for approved drafts...")

    while True:
        try:
            check_interval = db.get_setting("scheduler_check_interval_seconds", 60)
            await asyncio.sleep(check_interval)

            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE status = 'approved' AND (scheduled_at IS NULL OR scheduled_at <= CURRENT_TIMESTAMP)")
                approved_count = cursor.fetchone()['c']

            if approved_count == 0:
                continue

            logger.debug(f"Scheduler: Found {approved_count} approved drafts ready for check.")

            # Розрахунок часу наступної публікації — jitter фіксується один раз
            last_pub_str = db.get_last_published_time()
            if last_pub_str:
                last_pub_dt = datetime.strptime(last_pub_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if _next_publish_time is None or _next_publish_time < last_pub_dt:
                    _next_publish_time = _compute_next_publish_time(last_pub_dt)

                now = datetime.now(timezone.utc)
                if now < _next_publish_time:
                    wait = (_next_publish_time - now).total_seconds()
                    logger.debug(f"Scheduler: Next publish in {int(wait)} seconds (at {_next_publish_time} UTC).")
                    continue

            draft = db.fetch_next_approved_draft_for_publish()
            if not draft:
                continue

            draft_id = draft["id"]
            text = draft.get("rewritten_text") or draft.get("original_text")
            
            from utils import validate_post_text, ValidationError
            try:
                text = validate_post_text(text)
            except ValidationError as e:
                logger.error(f"Scheduler: Draft #{draft_id} text validation failed: {e}. Failing.")
                db.update_draft_status(draft_id, "failed", last_error=f"Validation error: {e}")
                continue

            logger.info(f"Scheduler: Publishing Draft #{draft_id}...")

            def do_publish():
                media_path = draft.get("media_path") if draft.get("media_status") == "ready" else None
                publisher.publish(draft_id, text, media_path=media_path)

            success = await retry_manager.execute_with_retries(draft_id, do_publish)

            if success:
                logger.success(f"Scheduler: Draft #{draft_id} published successfully.")
                # publisher.publish() вже записав реальний (або mock) tweet_id
                _next_publish_time = None  # перерахуємо від нової публікації

        except asyncio.CancelledError:
            logger.info("Scheduler loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Scheduler loop encountered an error: {e}")
            await asyncio.sleep(10)

def start_scheduler():
    return asyncio.create_task(scheduler_loop())
