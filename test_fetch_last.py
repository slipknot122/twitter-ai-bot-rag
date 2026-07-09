import sys
import asyncio
from loguru import logger
from config import settings
from telegram_listener import TelegramListener
from main import process_telegram_message, setup_logger

async def fetch_last_post():
    setup_logger()
    logger.info("Starting script to fetch the LAST post...")
    
    listener = TelegramListener()
    if not listener.client:
        logger.error("No Telegram credentials. Exiting.")
        return
        
    # Стартуємо клієнт (якщо сесії ще немає, тут теж попросить номер)
    await listener.client.start()
    
    for channel in settings.telegram_channels:
        logger.info(f"Fetching the latest message from '{channel}'...")
        try:
            # Отримуємо 1 останнє повідомлення
            messages = await listener.client.get_messages(channel, limit=1)
            if messages:
                msg = messages[0]
                if msg.text:
                    logger.info(f"Found message ID {msg.id}. Sending to pipeline...")
                    await process_telegram_message(str(msg.id), channel, msg.text)
                else:
                    logger.warning(f"The last message in {channel} has no text (maybe just an image).")
            else:
                logger.warning(f"Could not find any messages in {channel}.")
        except Exception as e:
            logger.error(f"Failed to fetch from {channel}: {e}")

if __name__ == "__main__":
    asyncio.run(fetch_last_post())
