import asyncio
import random
from datetime import datetime, timedelta
from loguru import logger
from database import db
from twitter_publisher import publisher
from retry_manager import retry_manager

async def scheduler_loop():
    logger.info("Scheduler started. Waiting for approved drafts...")
    
    while True:
        try:
            check_interval = db.get_setting("scheduler_check_interval_seconds", 60)
            await asyncio.sleep(check_interval)
            
            # Перевіряємо, чи є взагалі що публікувати (не блокуючи їх)
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE status = 'approved' AND (scheduled_at IS NULL OR scheduled_at <= CURRENT_TIMESTAMP)")
                approved_count = cursor.fetchone()['c']
                
            if approved_count == 0:
                continue
                
            # Logger для інформативності (логуємо тільки якщо є що публікувати)
            logger.debug(f"Scheduler: Found {approved_count} approved drafts ready for check.")

            # Перевірка інтервалів (delay + jitter)
            last_pub_str = db.get_last_published_time()
            if last_pub_str:
                last_pub_dt = datetime.strptime(last_pub_str, "%Y-%m-%d %H:%M:%S")
                delay_minutes = db.get_setting("publish_delay_minutes", 45)
                jitter_percent = db.get_setting("publish_jitter_percent", 15)
                
                # Розрахунок jitter
                base_delay_seconds = delay_minutes * 60
                jitter_seconds = base_delay_seconds * (jitter_percent / 100.0)
                actual_delay = base_delay_seconds + random.uniform(-jitter_seconds, jitter_seconds)
                
                next_pub_time = last_pub_dt + timedelta(seconds=actual_delay)
                now = datetime.utcnow()
                
                if now < next_pub_time:
                    wait_seconds = (next_pub_time - now).total_seconds()
                    logger.debug(f"Scheduler: Next publish in {int(wait_seconds)} seconds (at {next_pub_time} UTC).")
                    continue

            # Якщо час настав (або це перший пост)
            draft = db.fetch_next_approved_draft_for_publish()
            if not draft:
                continue
                
            draft_id = draft["id"]
            text = draft.get("rewritten_text") or draft.get("original_text")
            
            logger.info(f"Scheduler: Publishing Draft #{draft_id}...")
            
            # Публікація через Retry Manager
            # Якщо publish() падає, retry_manager обробить помилку і перенесе пост на потім або у FAILED
            def do_publish():
                publisher.publish(draft_id, text)
                
            success = await retry_manager.execute_with_retries(draft_id, do_publish)
            
            if success:
                logger.success(f"Scheduler: Draft #{draft_id} published successfully.")
                db.record_published_tweet(draft_id, f"auto_{draft_id}") # Temporary fake ID till real API
                
        except asyncio.CancelledError:
            logger.info("Scheduler loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Scheduler loop encountered an error: {e}")
            await asyncio.sleep(10)

def start_scheduler():
    return asyncio.create_task(scheduler_loop())
