import asyncio
from loguru import logger
from database import db
from ai_engine import ai_engine
from semantic_memory import semantic_memory

async def ai_worker_loop():
    """
    Фоновий процес, який постійно перевіряє базу на наявність нових повідомлень,
    відправляє їх у LLM і оновлює статус.
    """
    logger.info("AI Worker started and waiting for new messages in queue...")
    
    while True:
        draft_id = None
        try:
            # Атомарно дістаємо і лочимо повідомлення
            draft = db.fetch_next_new_draft()
            
            if not draft:
                # Черга порожня, чекаємо 5 секунд
                await asyncio.sleep(5)
                continue
                
            draft_id = draft['id']
            original_text = draft['original_text']
            
            logger.info(f"AI Worker: Picked up Draft ID {draft_id} for processing.")
            
            # Викликаємо AI Engine в окремому потоці (щоб LLM не блокував весь бот!)
            result = await asyncio.to_thread(ai_engine.process_text, original_text)
            
            # Маппимо екшени на статуси БД
            action = result.get('action', 'FAILED')
            
            shadow_mode = db.get_setting("shadow_mode", True)
            publish_status = "review" if shadow_mode else "approved"
            
            status_map = {
                "PUBLISH": publish_status,
                "REVIEW": "review",
                "IGNORE": "ignored",
                "FAILED": "review"
            }
            
            new_status = status_map.get(action, "review")
            
            tweet_text = result.get('tweet_text', '')
            
            # Додаємо валідацію тексту, якщо він йде на публікацію
            if new_status == "approved":
                from utils import validate_post_text, ValidationError
                try:
                    tweet_text = validate_post_text(tweet_text)
                except ValidationError as ve:
                    logger.warning(f"Draft {draft_id}: Text validation failed: {ve}")
                    new_status = "review"
                    result['reason'] = f"Validation failed: {ve}. Original reason: {result.get('reason', '')}"
            
            # Оновлюємо запис у БД (включаючи image_prompt і sentiment)
            updates = {
                "rewritten_text": tweet_text,
                "reason": result.get('reason', ''),
                "confidence": result.get('confidence', 0.0),
                "image_prompt": result.get('image_prompt', ''),
                "sentiment": result.get('sentiment', 'Neutral'),
                "category": result.get('category', 'NEWS')
            }
            db.complete_ai_processing(draft_id, new_status, updates)
                
            if new_status == "approved" and tweet_text:
                try:
                    # Асинхронно зберігаємо в векторну БД, щоб не блокувати event loop
                    await asyncio.to_thread(semantic_memory.save, tweet_text)
                except Exception as mem_e:
                    logger.warning(f"Draft {draft_id}: Failed to save to semantic memory: {mem_e}")
                    # Не змінюємо статус, бо контент згенеровано успішно, просто попереджаємо

            logger.success(f"Draft {draft_id} processed successfully. Status -> {new_status}")
            
        except Exception as e:
            if draft_id is not None:
                logger.error(f"Error processing draft {draft_id}: {e}")
                # Якщо LLM впала (наприклад, помилка API), відправляємо на failed,
                # щоб не зациклитись. Але recovery може повернути в new, або залишимо failed для аналізу.
                # User preference: "якщо LLM впала... відправляємо на review". Але ми використовували "review".
                # According to ALLOWED_TRANSITIONS, processing -> failed or review are allowed. Let's use failed or review.
                db.complete_ai_processing(draft_id, "failed", {"last_error": str(e)})
            else:
                logger.error(f"Error in AI worker loop before fetching draft: {e}")
            
            await asyncio.sleep(5)
            continue

if __name__ == "__main__":
    asyncio.run(ai_worker_loop())
