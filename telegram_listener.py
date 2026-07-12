import asyncio
from telethon import TelegramClient, events
from loguru import logger
from config import settings
from database import db

# Initialize Telethon Client
# Ми використовуємо session name 'bot_session', який створить файл bot_session.session
client = TelegramClient('bot_session', settings.telegram_api_id, settings.telegram_api_hash)

# Telethon requires integer IDs for negative chat IDs, but they come as strings from config
parsed_channels = []
for c in settings.telegram_channels:
    try:
        parsed_channels.append(int(c))
    except ValueError:
        parsed_channels.append(c)

@client.on(events.NewMessage(chats=parsed_channels))
async def handle_new_message(event):
    """
    Обробник нових повідомлень з Telegram.
    Тут немає бізнес-логіки ШІ. Тільки збереження в SQLite як чергу.
    """
    channel = event.chat.username if event.chat and event.chat.username else str(event.chat_id)
    message_id = str(event.id)
    text = event.text

    if not text or len(text) < 10:
        return # Ігноруємо порожні або занадто короткі повідомлення

    logger.info(f"Listener: New message from {channel} (ID: {message_id})")

    if db.is_message_processed(message_id, channel):
        logger.debug(f"Listener: Message {channel}:{message_id} already exists. Skipping.")
        return

    try:
        # Зберігаємо повідомлення в базу
        processed_msg_id = db.mark_message_processed(message_id, channel)
        
        # Створюємо чернетку зі статусом 'new' (додаємо в чергу для AI Worker)
        draft_id = db.create_draft(
            processed_message_id=processed_msg_id,
            original_text=text,
            status='new'
        )
        logger.success(f"Listener: Message added to queue (Draft ID: {draft_id})")
    except Exception as e:
        logger.error(f"Listener: Failed to add message to DB: {e}")

async def start_listener():
    """Запуск Telegram клієнта."""
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
        return
        
    logger.info(f"Starting Telegram Listener for channels: {settings.telegram_channels}")
    
    # Використовуємо start() з пустими параметрами. 
    # Під час першого запуску він попросить телефон в консолі, якщо сесії немає.
    await client.start()
    logger.success("Telegram Listener connected and running 24/7!")
    
    # Запускаємо безкінечний цикл
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(start_listener())
