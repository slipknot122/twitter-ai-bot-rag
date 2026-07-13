import sys
import os
import asyncio
import datetime
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# Додаємо кореневу папку в sys.path, щоб імпортувати наші модулі
sys.path.append(str(Path(__file__).parent.parent))

from database import db
from twitter_publisher import publisher
from semantic_memory import semantic_memory
from media_builder import media_builder, MediaGenerationError
from loguru import logger

from config import settings

if settings.web_admin_host not in {"127.0.0.1", "localhost", "::1"}:
    raise RuntimeError("Web admin must bind to localhost for security reasons.")

app = FastAPI(title="Twitter AI Bot Admin")

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Роздача media файлів через /media/{filename}
media_dir = Path(__file__).parent.parent / "media"
media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")

# Background tasks set — захист від garbage collection (рекомендація рев'юера)
_background_tasks: set[asyncio.Task] = set()

class UpdateDraftRequest(BaseModel):
    rewritten_text: str

class SettingsRequest(BaseModel):
    system_prompt: str
    shadow_mode: bool
    publish_delay_minutes: int = Field(ge=1, le=1440)
    publish_jitter_percent: int = Field(ge=0, le=100)
    max_retries: int = Field(ge=0, le=10)
    scheduler_check_interval_seconds: int = Field(ge=10, le=3600)
    image_overlay: str
    allowed_categories: str

@app.get("/", response_class=HTMLResponse)
def index(request: Request, tab: str = "review"):
    if tab == "approved":
        statuses = ["approved", "published", "publishing"]
    elif tab == "ignored":
        statuses = ["ignored", "failed"]
    else:
        # Default tab is review
        tab = "review"
        statuses = ["review", "pending"]

    drafts = db.get_drafts_by_status(statuses)
    analytics = db.get_analytics()
        
    return templates.TemplateResponse(
        request=request, name="index.html", context={"request": request, "drafts": drafts, "current_tab": tab, "analytics": analytics}
    )

@app.post("/api/drafts/{draft_id}/publish")
def publish_draft(draft_id: int):
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, rewritten_text FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found")
        if row['status'] not in ['review', 'pending']:
            raise HTTPException(status_code=409, detail="Only drafts in review status can be approved")
            
        text = row['rewritten_text']
        from utils import validate_post_text, ValidationError
        try:
            validated_text = validate_post_text(text)
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    logger.info(f"Web Admin: Approving draft {draft_id} for Scheduler")
    success = db.approve_draft(draft_id, {"rewritten_text": validated_text})
    if not success:
        raise HTTPException(status_code=409, detail="Failed to transition draft to approved state (possible race condition or invalid state)")
    
    if validated_text:
        semantic_memory.save(validated_text)
            
    return {"status": "success"}

@app.post("/api/drafts/{draft_id}/ignore")
def ignore_draft(draft_id: int):
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found")
        if row['status'] not in ['review', 'pending']:
            raise HTTPException(status_code=400, detail="Only drafts in review status can be ignored")

    logger.info(f"Web Admin: Ignoring draft {draft_id}")
    success = db.ignore_draft(draft_id)
    if not success:
        raise HTTPException(status_code=409, detail="Failed to transition draft to ignored state")
    return {"status": "success"}

@app.post("/api/drafts/{draft_id}/update")
def update_draft(draft_id: int, request: UpdateDraftRequest):
    from utils import validate_post_text, ValidationError
    try:
        validated_text = validate_post_text(request.rewritten_text)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
        
    logger.info(f"Web Admin: Updating draft {draft_id}")
    db.update_draft_text(draft_id, validated_text)
    return {"status": "success"}


# ---------------------------------------------------------------------------
# Media Generation API
# ---------------------------------------------------------------------------

class GenerateImageRequest(BaseModel):
    image_prompt: Optional[str] = Field(default=None, min_length=20, max_length=1500)
    regenerate: bool = False


@app.post("/api/drafts/{draft_id}/image", status_code=202)
async def generate_image(draft_id: int, request: Optional[GenerateImageRequest] = None):
    """
    Запускає генерацію зображення у фоні. Повертає 202 Accepted.
    Атомарна зміна статусу захищає від подвійного кліку.
    """
    # 1. Отримуємо драфт
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, image_prompt, media_status, media_path FROM drafts WHERE id = ?", (draft_id,))
        draft = cursor.fetchone()

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # 2. Визначаємо промпт: з тіла запиту або з БД
    prompt = None
    if request and request.image_prompt:
        prompt = request.image_prompt
    elif draft["image_prompt"]:
        prompt = draft["image_prompt"]
    
    if not prompt or len(prompt.strip()) < 20:
        raise HTTPException(status_code=400, detail="image_prompt is required (min 20 chars)")

    # 3. Атомарна зміна статусу (захист від подвійного кліку)
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE drafts SET media_status = 'generating', media_error = NULL, "
            "image_prompt = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND media_status != 'generating'",
            (prompt, draft_id)
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=409, detail="Image generation already in progress")

    # 4. Запускаємо фонову задачу (захищену від GC)
    task = asyncio.create_task(_generate_image_background(draft_id, prompt, draft["media_path"] if request and request.regenerate else None))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"draft_id": draft_id, "media_status": "generating"}


async def _generate_image_background(draft_id: int, prompt: str, old_media_path: Optional[str] = None):
    """
    Фонова генерація зображення. При regeneration старий файл видаляється
    тільки ПІСЛЯ успішної генерації нового.
    """
    try:
        result = await asyncio.to_thread(media_builder.generate, draft_id, prompt)

        if result:
            # Успіх — оновлюємо БД
            now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE drafts SET media_status = 'ready', media_path = ?, "
                    "media_provider = ?, media_mime_type = ?, media_size_bytes = ?, "
                    "media_width = ?, media_height = ?, media_error = NULL, "
                    "media_created_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (result["media_path"], result["media_provider"], result["mime_type"],
                     result["size_bytes"], result["width"], result["height"], now_utc, draft_id)
                )
                conn.commit()

            # Видаляємо старий файл тільки після успіху нового
            if old_media_path and old_media_path != result["media_path"]:
                media_builder.delete_media_file(old_media_path)

            logger.success(f"Background image generation completed for draft {draft_id}")
        else:
            # Усі провайдери впали
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE drafts SET media_status = 'failed', "
                    "media_error = 'All providers failed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (draft_id,)
                )
                conn.commit()

    except Exception as e:
        logger.error(f"Background image generation error for draft {draft_id}: {e}")
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE drafts SET media_status = 'failed', "
                "media_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(e)[:500], draft_id)
            )
            conn.commit()


@app.delete("/api/drafts/{draft_id}/image")
def delete_image(draft_id: int):
    """Безпечне видалення зображення."""
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT media_path FROM drafts WHERE id = ?", (draft_id,))
        draft = cursor.fetchone()

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Видаляємо файл (з перевіркою path traversal)
    if draft["media_path"]:
        media_builder.delete_media_file(draft["media_path"])

    # Очищаємо всі media-поля
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE drafts SET media_status = 'none', media_path = NULL, "
            "media_error = NULL, media_provider = NULL, media_mime_type = NULL, "
            "media_size_bytes = NULL, media_width = NULL, media_height = NULL, "
            "media_created_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (draft_id,)
        )
        conn.commit()

    return {"status": "success"}


@app.get("/api/drafts/{draft_id}/image/status")
def get_image_status(draft_id: int):
    """Polling endpoint для статусу генерації."""
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT media_status, media_path, media_error, media_provider, "
            "media_width, media_height, media_size_bytes, image_prompt "
            "FROM drafts WHERE id = ?", (draft_id,)
        )
        draft = cursor.fetchone()

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    result = dict(draft)
    # Не відправляємо абсолютний filesystem path — тільки URL для браузера
    if result.get("media_path"):
        filename = Path(result["media_path"]).name
        result["media_url"] = f"/media/{filename}"
    else:
        result["media_url"] = None

    return result

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    # Import inside to avoid circular deps if any
    from ai_engine import EDITOR_SYSTEM_PROMPT
    current_prompt = db.get_setting("system_prompt", EDITOR_SYSTEM_PROMPT)
    shadow_mode = db.get_setting("shadow_mode", True)
    publish_delay_minutes = db.get_setting("publish_delay_minutes", 45)
    publish_jitter_percent = db.get_setting("publish_jitter_percent", 15)
    max_retries = db.get_setting("max_retries", 3)
    scheduler_check_interval_seconds = db.get_setting("scheduler_check_interval_seconds", 60)
    image_overlay = db.get_setting("image_overlay", "btc_price")
    allowed_categories = db.get_setting("allowed_categories", "MARKET, LISTING, HACK, SECURITY, AIRDROP, FUNDING, REGULATION, PARTNERSHIP, TOKEN, AI, NFT, MEME, DEFI, STABLECOIN, EXCHANGE, NEWS")
    
    return templates.TemplateResponse(
        request=request, name="settings.html", context={
            "request": request, 
            "system_prompt": current_prompt, 
            "shadow_mode": shadow_mode,
            "publish_delay_minutes": publish_delay_minutes,
            "publish_jitter_percent": publish_jitter_percent,
            "max_retries": max_retries,
            "scheduler_check_interval_seconds": scheduler_check_interval_seconds,
            "image_overlay": image_overlay,
            "allowed_categories": allowed_categories
        }
    )

@app.post("/api/settings")
def update_settings(request: SettingsRequest):
    logger.info("Web Admin: Updating settings")
    db.set_setting("system_prompt", request.system_prompt)
    db.set_setting("shadow_mode", request.shadow_mode)
    db.set_setting("publish_delay_minutes", request.publish_delay_minutes)
    db.set_setting("publish_jitter_percent", request.publish_jitter_percent)
    db.set_setting("max_retries", request.max_retries)
    db.set_setting("scheduler_check_interval_seconds", request.scheduler_check_interval_seconds)
    db.set_setting("image_overlay", request.image_overlay)
    db.set_setting("allowed_categories", request.allowed_categories)
    return {"status": "success"}

@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, filter: str = "ALL"):
    logs_content = []
    log_file = Path(__file__).parent.parent / "bot.log"
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            # Беремо останні 500 рядків
            lines = lines[-500:]
            for line in reversed(lines):
                if filter == "ALL" or filter in line:
                    logs_content.append(line)
                    
    return templates.TemplateResponse(
        request=request, name="logs.html", context={
            "request": request,
            "logs": logs_content,
            "current_filter": filter
        }
    )

@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    # Check Gemini
    from config import settings
    gemini_configured = bool(settings.gemini_api_key)
    
    # Check Telegram
    from utils import is_telegram_configured
    telegram_configured = is_telegram_configured()
    
    # Check X (Twitter)
    twitter_configured = bool(settings.twitter_api_key and settings.twitter_api_secret and settings.twitter_access_token and settings.twitter_access_token_secret)
    
    # Check Cloudflare
    cloudflare_configured = bool(settings.cloudflare_account_id and settings.cloudflare_api_token)
    
    # Check Pollinations
    # Pollinations works without a key currently, so it's configured if we have the model string or by default.
    pollinations_configured = True 
    
    # Check Database
    db_status = "Available"
    try:
        with db._get_connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        db_status = "Error"
        
    return templates.TemplateResponse(
        request=request, name="status.html", context={
            "request": request,
            "gemini_status": "Configured" if gemini_configured else "Missing",
            "telegram_status": "Configured" if telegram_configured else "Missing",
            "twitter_status": "Configured" if twitter_configured else "Missing",
            "cloudflare_status": "Configured" if cloudflare_configured else "Missing",
            "pollinations_status": "Configured" if pollinations_configured else "Missing",
            "twitter_dry_run": settings.twitter_dry_run,
            "admin_bind": settings.web_admin_host,
            "db_status": db_status,
            "media_enabled": settings.media_generation_enabled,
            "media_providers": settings.media_provider_order
        }
    )
