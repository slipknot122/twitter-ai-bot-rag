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
        except Exception:
            self._consecutive_failures += 1
            logger.error("Failed to reload SourceCache [SAFE_ERR_CACHE_RELOAD]")
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

source_cache = SourceCache(db)

history_fetch_queue: Optional[asyncio.Queue] = None

async def _history_fetch_worker(client, cache, db_instance, queue):
    while True:
        req = await queue.get()
        try:
            msgs_limit = req.get("messages", 5)
            chans_limit = req.get("channels", 10)

            logger.info(f"Starting history fetch: {msgs_limit} msgs from up to {chans_limit} channels")

            sources = db_instance.get_sources(is_active=1)
            active_tg_sources = [
                source
                for source in sources
                if source['source_type'] == 'telegram'
                and source['resolution_status'] == 'resolved'
            ][:chans_limit]

            # Pre-fetch dialogs to populate Telethon's entity cache.
            try:
                await client.get_dialogs(limit=100)
            except Exception:
                logger.warning("Could not pre-fetch Telegram dialogs [SAFE_ERR_HISTORY_DIALOGS]")

            count = 0
            for source in active_tg_sources:
                channel_id = source['external_id']
                try:
                    async for message in client.iter_messages(int(channel_id), limit=msgs_limit):
                        if getattr(message, 'text', None):
                            await process_telegram_event(message, cache, db_instance)
                            count += 1
                except Exception:
                    logger.error("Failed to fetch Telegram history [SAFE_ERR_HISTORY_CHANNEL]")

                await asyncio.sleep(1)  # Prevent flood

            logger.success(f"History fetch complete. Processed {count} messages.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("History fetch worker error [SAFE_ERR_HISTORY_WORKER]")
        finally:
            queue.task_done()

def create_telegram_client() -> TelegramClient:
    """Factory для створення TelegramClient виключно під час виконання."""
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError("Telegram credentials are not configured")

    return TelegramClient(
        'bot_session',
        settings.telegram_api_id,
        settings.telegram_api_hash
    )

async def process_telegram_event(event, cache, db_instance):
    """
    Чиста функція для обробки подій.
    Тут немає бізнес-логіки ШІ. Тільки збереження в SQLite як чергу.
    """
    if not getattr(event, 'chat_id', None):
        return
        
    try:
        normalized_id = normalize_telegram_id(event.chat_id)
    except ValueError:
        return
        
    source = cache.get(normalized_id)
    if not source:
        return

    message_id = str(event.id)

    # Читаємо text тільки перед передачею в БД
    text = getattr(event, 'text', '')
    if not text or len(text) < 10:
        return

    try:
        result = db_instance.create_draft_from_active_source("telegram", normalized_id, message_id, text)
        if result == "duplicate":
            # Не логуємо деталі для duplicate, щоб не смітити
            pass
        elif result == "rejected":
            logger.debug("Listener: Message rejected by DB transaction [SAFE_REJECT]")
        elif result == "created":
            logger.success("Listener: Message added to queue [SAFE_CREATE]")
    except Exception:
        logger.error("Listener: Failed to add message to DB [SAFE_ERR_LISTENER_DB]")

async def start_listener():
    """Запуск Telegram слухача."""
    global history_fetch_queue

    try:
        client = create_telegram_client()
    except RuntimeError:
        logger.error("Cannot start listener [SAFE_ERR_MISSING_CREDENTIALS]")
        return

    logger.info("Starting Telegram Listener with SourceCache filtering")

    # Preload cache
    source_cache.reload()
    logger.info(f"Loaded {len(source_cache._cache)} active telegram sources into cache.")

    @client.on(events.NewMessage(incoming=True))
    async def handle_new_message(event):
        await process_telegram_event(event, source_cache, db)

    worker_task = None
    try:
        await client.start()
        history_fetch_queue = asyncio.Queue(maxsize=1)
        worker_task = asyncio.create_task(
            _history_fetch_worker(client, source_cache, db, history_fetch_queue)
        )
        logger.success("Telegram Client started and listening for messages")
        await client.run_until_disconnected()
    except Exception:
        logger.error("Telegram Client connection failed [SAFE_ERR_CONNECTION_FAILED]")
        raise RuntimeError("Telegram Client connection failed [SAFE_ERR_CONNECTION_FAILED]")
    finally:
        history_fetch_queue = None
        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
