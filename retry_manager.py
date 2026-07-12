import asyncio
from loguru import logger
from database import db, AmbiguousPublishStateError
from typing import Callable, Any

class RetryManager:
    def __init__(self):
        # Base delays in seconds: 1m, 5m, 15m
        self.retry_delays = [60, 300, 900]

    def _categorize_error(self, e: Exception) -> bool:
        """Повертає True, якщо помилка тимчасова (можна робити retry)."""
        error_str = str(e).lower()
        
        # Постійні помилки
        permanent_keywords = ["401", "unauthorized", "403", "forbidden", "invalid token", "too long", "invalid media", "400"]
        for kw in permanent_keywords:
            if kw in error_str:
                return False
                
        # Тимчасові помилки (за замовчуванням вважаємо невідомі помилки тимчасовими для безпеки)
        return True

    async def execute_with_retries(self, draft_id: int, func: Callable[[], Any]) -> bool:
        """
        Виконує функцію публікації з повторними спробами (Retry).
        Повертає True у разі успіху, False у разі провалу (або перенесення на потім).
        """
        max_retries = db.get_setting("max_retries", 3)
        
        # Fetch current retry count from DB
        drafts = db.get_drafts_by_status(["publishing"])
        draft = next((d for d in drafts if d["id"] == draft_id), None)
        if not draft:
            logger.error(f"RetryManager: Draft {draft_id} not found in PUBLISHING state.")
            return False
            
        current_retry = draft.get("retry_count", 0)

        try:
            # Виклик функції (може бути синхронною, тому обгортаємо)
            if asyncio.iscoroutinefunction(func):
                await func()
            else:
                await asyncio.to_thread(func)
                
            return True
            
        except AmbiguousPublishStateError as ae:
            logger.critical(f"RetryManager: Ambiguous DB failure after successful X call for Draft {draft_id}: {ae}. HALTING retries. Manual intervention required!")
            return False
            
        except Exception as e:
            is_transient = self._categorize_error(e)
            error_msg = f"{type(e).__name__}: {str(e)}"
            
            if not is_transient:
                logger.error(f"RetryManager: Permanent error on Draft {draft_id}: {error_msg}. Moving to FAILED.")
                db.mark_publish_failed(draft_id, error_msg)
                return False
                
            # Якщо тимчасова помилка
            current_retry += 1
            
            if current_retry > max_retries:
                logger.error(f"RetryManager: Max retries ({max_retries}) reached for Draft {draft_id}. Error: {error_msg}. Moving to FAILED.")
                db.mark_publish_failed(draft_id, error_msg)
                return False
            
            # Знаходимо затримку
            delay_index = min(current_retry - 1, len(self.retry_delays) - 1)
            delay_seconds = self.retry_delays[delay_index]
            
            logger.warning(f"RetryManager: Transient error on Draft {draft_id}: {error_msg}. Retry {current_retry}/{max_retries} in {delay_seconds} seconds.")
            
            import datetime
            next_try = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delay_seconds)
            next_try_str = next_try.strftime("%Y-%m-%d %H:%M:%S")
            
            success = db.schedule_publish_retry(draft_id, next_try_str, error_msg)
            if success:
                logger.info(f"RetryManager: Draft {draft_id} scheduled for retry at {next_try} UTC.")
            else:
                logger.error(f"RetryManager: Failed to schedule retry for draft {draft_id}.")
            return False

retry_manager = RetryManager()
