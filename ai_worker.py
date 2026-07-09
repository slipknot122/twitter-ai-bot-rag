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
            
            status_map = {
                "PUBLISH": "approved", # Одразу готові до публікації
                "REVIEW": "review",
                "IGNORE": "ignored",
                "FAILED": "review" # Змінено з failed на review, щоб людина побачила помилку LLM
            }
            
            new_status = status_map.get(action, "review")
            
            # Оновлюємо запис у БД
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE drafts 
                    SET rewritten_text = ?, 
                        reason = ?, 
                        confidence = ?, 
                        status = ?, 
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                    """,
                    (result.get('tweet_text', ''), result.get('reason', ''), result.get('confidence', 0.0), new_status, draft_id)
                )
                conn.commit()
                
            if new_status == "approved" and result.get('tweet_text'):
                try:
                    # Асинхронно зберігаємо в векторну БД, щоб не блокувати event loop
                    await asyncio.to_thread(semantic_memory.save, result.get('tweet_text'))
                except Exception as mem_e:
                    logger.warning(f"Draft {draft_id}: Failed to save to semantic memory: {mem_e}")
                    # Не змінюємо статус, бо контент згенеровано успішно, просто попереджаємо

            logger.success(f"Draft {draft_id} processed successfully. Status -> {new_status}")
            
        except Exception as e:
            if draft_id is not None:
                logger.error(f"Error processing draft {draft_id}: {e}")
                # Якщо LLM впала (наприклад, помилка API), відправляємо на review, 
                # щоб користувач міг перевірити і не втратив новину
                db.update_draft_status(draft_id, "review")
                # Також записуємо помилку
                with db._get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE drafts SET last_error = ? WHERE id = ?", (str(e), draft_id))
                    conn.commit()
            else:
                logger.error(f"Error in AI worker loop before fetching draft: {e}")
            
            await asyncio.sleep(5)
            continue

if __name__ == "__main__":
    asyncio.run(ai_worker_loop())
