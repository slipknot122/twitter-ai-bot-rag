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
    
    # Parse audit_result for UI rendering
    import json
    for d in drafts:
        if d.get("audit_result"):
            try:
                d["audit_data"] = json.loads(d["audit_result"])
            except Exception:
                d["audit_data"] = None
        else:
            d["audit_data"] = None

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

from typing import Literal

class GenerateImageRequest(BaseModel):
    image_prompt: Optional[str] = Field(default=None, min_length=20, max_length=1500)
    action: Literal["generate", "retry", "regenerate"] = Field(default="generate", description="generate, retry, or regenerate")


@app.post("/api/drafts/{draft_id}/image", status_code=202)
async def generate_image(draft_id: int, request: GenerateImageRequest):
    """
    Чергує медіа на генерацію. Повертає 202 Accepted.
    """
    status = db.queue_media_generation(draft_id, prompt=request.image_prompt, action=request.action)
    if status == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="Draft not found")
    if status == "INVALID_PROMPT":
        raise HTTPException(status_code=422, detail="image_prompt must be 20-1500 chars")
    if status == "CONFLICT":
        raise HTTPException(status_code=409, detail="Action not allowed for current state or already in progress")

    return {"draft_id": draft_id, "media_status": "pending"}


@app.delete("/api/drafts/{draft_id}/image")
def delete_image(draft_id: int):
    """Безпечне видалення зображення."""
    status = db.delete_media(draft_id)
    if status == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="Draft not found")
    if status == "CONFLICT":
        raise HTTPException(status_code=409, detail="Cannot delete media in current state")
    return {"status": "success"}


@app.get("/api/drafts/{draft_id}/image/status")
def get_image_status(draft_id: int):
    """Polling endpoint для статусу генерації."""
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT media_status, media_path, media_error_code, media_error_message, media_provider, "
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
