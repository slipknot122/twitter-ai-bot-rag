import asyncio
import time
from typing import Optional
from telethon import TelegramClient, events
from loguru import logger
from config import settings
from database import db, normalize_telegram_id

class SourceCache:
    def __init__(self, db_instance, ttl_seconds=60):
        self._db = db_instance
        self._ttl = ttl_seconds
        self._cache = {}
        self._loaded_at = 0
        self._consecutive_failures = 0

    def reload(self) -> bool:
        try:
            # We only cache active telegram sources
            sources = self._db.get_sources(is_active=1)
            new_cache = {}
            for s in sources:
                if s['source_type'] == 'telegram' and s['resolution_status'] == 'resolved':
                    new_cache[s['external_id']] = s
            self._cache = new_cache
            self._loaded_at = time.time()
            self._consecutive_failures = 0
            return True
        except Exception as e:
            self._consecutive_failures += 1
            logger.error(f"Failed to reload SourceCache: {e}")
            if self._consecutive_failures >= 2 or (time.time() - self._loaded_at >= self._ttl):
                logger.warning("Clearing SourceCache due to fail-closed policy.")
                self._cache = {}
            return False

    def get(self, normalized_id: str) -> Optional[dict]:
        now = time.time()
        if now - self._loaded_at >= self._ttl:
            self.reload()
        return self._cache.get(normalized_id)

    def invalidate(self):
        self._loaded_at = 0

# Initialize Telethon Client
client = TelegramClient('bot_session', settings.telegram_api_id, settings.telegram_api_hash)

source_cache = SourceCache(db)

# Listen to all incoming messages. We'll filter them using the cache.
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    """
    Обробник нових повідомлень з Telegram.
    Тут немає бізнес-логіки ШІ. Тільки збереження в SQLite як чергу.
    """
    if not event.chat_id:
        return
        
    try:
        normalized_id = normalize_telegram_id(event.chat_id)
    except ValueError:
        return
        
    source = source_cache.get(normalized_id)
    if not source:
        return

    message_id = str(event.id)
    text = event.text

    if not text or len(text) < 10:
        return # Ігноруємо порожні або занадто короткі повідомлення

    channel_name = source['name']
    logger.info(f"Listener: New message from {channel_name} (ID: {message_id})")

    try:
        result = db.create_draft_from_active_source("telegram", normalized_id, message_id, text)
        if result == "duplicate":
            logger.debug(f"Listener: Message {normalized_id}:{message_id} already exists. Skipping.")
        elif result == "rejected":
            logger.debug(f"Listener: Message from {normalized_id} rejected by DB transaction.")
        elif result == "created":
            logger.success(f"Listener: Message added to queue (Source: {channel_name}, MsgID: {message_id})")
    except Exception as e:
        logger.error(f"Listener: Failed to add message to DB: {e}")

async def start_listener():
    """Запуск Telegram клієнта."""
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
        return
        
    logger.info("Starting Telegram Listener with SourceCache filtering")
    
    # Preload cache
    source_cache.reload()
    logger.info(f"Loaded {len(source_cache._cache)} active telegram sources into cache.")
    
    # Використовуємо start() з пустими параметрами. 
    # Під час першого запуску він попросить телефон в консолі, якщо сесії немає.
    await client.start()
    logger.success("Telegram Listener connected and running 24/7!")
    
    # Запускаємо безкінечний цикл
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(start_listener())
