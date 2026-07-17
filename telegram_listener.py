import asyncio
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse
from telethon import TelegramClient, events
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from loguru import logger
from config import settings
from database import db, normalize_telegram_id


@dataclass(frozen=True)
class TelegramReference:
    kind: str
    value: str
    display: str


_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_RESERVED_PATHS = {"addstickers", "blog", "faq", "iv", "proxy", "s", "share", "username"}


def parse_telegram_reference(raw: str) -> TelegramReference:
    """Parse a Telegram ID, username or invite URL without exposing invite hashes."""
    value = str(raw or "").strip()
    if not value:
        raise ValueError("Вкажіть посилання, логін або числовий ID Telegram-групи")
    try:
        numeric = normalize_telegram_id(value)
        return TelegramReference("id", numeric, numeric)
    except ValueError:
        pass

    candidate = value
    if candidate.startswith("@"):
        candidate = candidate[1:]
    elif "://" in candidate or candidate.lower().startswith(("t.me/", "telegram.me/")):
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("t.me", "www.t.me", "telegram.me", "www.telegram.me"):
            raise ValueError("Дозволені лише посилання t.me або telegram.me")
        if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port:
            raise ValueError("Telegram-посилання містить непідтримувані параметри")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 1 and parts[0].startswith("+"):
            invite = parts[0][1:]
            if not _INVITE_RE.fullmatch(invite):
                raise ValueError("Некоректне приватне Telegram-посилання")
            return TelegramReference("invite", invite, "Приватне запрошення Telegram")
        if len(parts) == 2 and parts[0].lower() == "joinchat":
            invite = parts[1]
            if not _INVITE_RE.fullmatch(invite):
                raise ValueError("Некоректне приватне Telegram-посилання")
            return TelegramReference("invite", invite, "Приватне запрошення Telegram")
        if len(parts) != 1:
            raise ValueError("Некоректне Telegram-посилання")
        candidate = parts[0]

    candidate = candidate.strip()
    if candidate.lower() in _RESERVED_PATHS or not _USERNAME_RE.fullmatch(candidate):
        raise ValueError("Некоректний логін Telegram-групи")
    username = candidate.lower()
    return TelegramReference("username", username, f"@{username}")


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
telegram_resolve_queue: Optional[asyncio.Queue] = None
telegram_connection_checks: set[int] = set()


async def _telegram_resolve_worker(client, cache, db_instance, queue):
    while True:
        request = await queue.get()
        try:
            source_id = request["source_id"]
            reference = parse_telegram_reference(request["telegram_input"])
            check_only = bool(request.get("check_only"))
            if reference.kind == "id":
                entity = await asyncio.wait_for(client.get_entity(int(reference.value)), timeout=15)
                entity_id = str(entity.id)
            elif reference.kind == "username":
                entity = await asyncio.wait_for(client.get_entity(reference.value), timeout=15)
                entity_id = str(entity.id)
            else:
                invite = await asyncio.wait_for(client(CheckChatInviteRequest(reference.value)), timeout=15)
                chat = getattr(invite, "chat", None)
                if chat is None:
                    if check_only:
                        db_instance.complete_source_connection_check(
                            source_id, ok=False, error_code="join_required",
                            detail="Потрібне підтвердження вступу до приватної групи",
                        )
                        continue
                    if not request.get("allow_join"):
                        db_instance.mark_telegram_resolution(source_id, "join_required", "Потрібне підтвердження вступу до приватної групи")
                        continue
                    updates = await asyncio.wait_for(client(ImportChatInviteRequest(reference.value)), timeout=20)
                    chats = getattr(updates, "chats", [])
                    chat = chats[0] if chats else None
                if chat is None:
                    raise RuntimeError("Telegram invite did not return a chat")
                entity_id = str(chat.id)
            if check_only:
                db_instance.complete_source_connection_check(source_id, ok=True)
            else:
                db_instance.resolve_source(source_id, entity_id)
                db_instance.mark_telegram_resolution(source_id, "resolved", None)
                cache.invalidate()
        except asyncio.CancelledError:
            raise
        except Exception:
            source_id = request.get("source_id")
            if source_id is not None:
                if request.get("check_only"):
                    db_instance.complete_source_connection_check(
                        source_id, ok=False, error_code="telegram_unavailable",
                        detail="Немає доступу до Telegram-групи",
                    )
                else:
                    db_instance.mark_telegram_resolution(source_id, "failed", "Не вдалося перевірити Telegram-групу. Спробуйте ще раз.")
            logger.error("Telegram source operation failed [SAFE_ERR_TG_SOURCE]")
        finally:
            if request.get("check_only"):
                telegram_connection_checks.discard(request.get("source_id"))
            queue.task_done()


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
    global history_fetch_queue, telegram_resolve_queue

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
    resolve_worker_task = None
    try:
        await client.start()
        history_fetch_queue = asyncio.Queue(maxsize=1)
        telegram_resolve_queue = asyncio.Queue(maxsize=20)
        worker_task = asyncio.create_task(
            _history_fetch_worker(client, source_cache, db, history_fetch_queue)
        )
        resolve_worker_task = asyncio.create_task(
            _telegram_resolve_worker(client, source_cache, db, telegram_resolve_queue)
        )
        logger.success("Telegram Client started and listening for messages")
        await client.run_until_disconnected()
    except Exception:
        logger.error("Telegram Client connection failed [SAFE_ERR_CONNECTION_FAILED]")
        raise RuntimeError("Telegram Client connection failed [SAFE_ERR_CONNECTION_FAILED]")
    finally:
        history_fetch_queue = None
        telegram_resolve_queue = None
        for task in (worker_task, resolve_worker_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
