import asyncio
import uvicorn
from loguru import logger
import sys

from database import db
from telegram_listener import start_listener
from ai_worker import ai_worker_loop
from scheduler import scheduler_loop
from media_worker import media_worker_loop
from web_admin.main import app as web_app

# Налаштовуємо логування у файл
logger.add("bot.log", rotation="10 MB", retention="7 days", encoding="utf-8")

async def start_web_admin():
    """Запускає FastAPI сервер у фоновому таску."""
    logger.info("Starting Web Admin server on http://127.0.0.1:8000")
    config = uvicorn.Config(web_app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

async def run_service_tasks(service_coroutines):
    """Run services as a fail-fast group and always drain sibling tasks."""
    tasks = [
        asyncio.create_task(coroutine, name=name)
        for name, coroutine in service_coroutines
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def main():
    logger.info("Twitter AI Bot: Starting all services...")

    # Recovery must complete before any listener or worker starts.
    db.recover_stuck_drafts()

    services = [
        ("TelegramListener", start_listener()),
        ("AIWorker", ai_worker_loop()),
        ("MediaWorker", media_worker_loop()),
        ("Scheduler", scheduler_loop()),
        ("WebAdmin", start_web_admin()),
    ]
    try:
        await run_service_tasks(services)
    except asyncio.CancelledError:
        logger.info("Shutting down gracefully...")
        raise
    except Exception:
        logger.exception("Critical error in main loop")
        raise

if __name__ == "__main__":
    try:
        # Для Windows важливо використовувати правильний event loop
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
