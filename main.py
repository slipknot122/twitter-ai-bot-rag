import asyncio
import uvicorn
from loguru import logger
import sys

from database import db
from telegram_listener import start_listener
from ai_worker import ai_worker_loop
from scheduler import scheduler_loop
from web_admin.main import app as web_app

# Налаштовуємо логування у файл
logger.add("bot.log", rotation="10 MB", retention="7 days", encoding="utf-8")

async def start_web_admin():
    """Запускає FastAPI сервер у фоновому таску."""
    logger.info("Starting Web Admin server on http://127.0.0.1:8000")
    config = uvicorn.Config(web_app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    logger.info("Twitter AI Bot: Starting all services...")
    
    # Recovery: повертаємо завислі статуси після можливого крашу
    db.recover_stuck_drafts()
    
    # Запускаємо всі 4 сервіси одночасно
    tasks = [
        asyncio.create_task(start_listener(), name="TelegramListener"),
        asyncio.create_task(ai_worker_loop(), name="AIWorker"),
        asyncio.create_task(scheduler_loop(), name="Scheduler"),
        asyncio.create_task(start_web_admin(), name="WebAdmin")
    ]
    
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutting down gracefully...")
    except Exception as e:
        logger.error(f"Critical error in main loop: {e}")

if __name__ == "__main__":
    try:
        # Для Windows важливо використовувати правильний event loop
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
