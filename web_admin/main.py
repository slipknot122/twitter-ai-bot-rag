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

from typing import Literal
import sqlite3

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

class FetchHistoryRequest(BaseModel):
    messages_limit: int = Field(ge=1, le=10)
    channels_limit: int = Field(ge=1, le=100)

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
    elif tab == "sources":
        statuses = []
    else:
        # Default tab is review
        tab = "review"
        statuses = ["review", "pending"]

    drafts = db.get_drafts_by_status(statuses) if statuses else []
    
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


@app.post("/api/drafts/{draft_id}/image/cancel")
def cancel_image(draft_id: int):
    """Скасовує генерацію медіа."""
    with db._get_connection() as conn:
        row = conn.cursor().execute("SELECT id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found")
            
    success = db.cancel_media_generation(draft_id)
    if not success:
        raise HTTPException(status_code=409, detail="Cannot cancel media in current state")
    return {"status": "success"}

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
    media_path = result.pop("media_path", None)
    if media_path:
        filename = Path(media_path).name
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
def update_settings(req: SettingsRequest):
    db.set_setting("system_prompt", req.system_prompt)
    db.set_setting("shadow_mode", req.shadow_mode)
    db.set_setting("publish_delay_minutes", req.publish_delay_minutes)
    db.set_setting("publish_jitter_percent", req.publish_jitter_percent)
    db.set_setting("max_retries", req.max_retries)
    db.set_setting("scheduler_check_interval_seconds", req.scheduler_check_interval_seconds)
    db.set_setting("image_overlay", req.image_overlay)
    db.set_setting("allowed_categories", req.allowed_categories)
    return {"status": "ok"}


# --- Phase 5: Source CRUD API ---

class SourceCreate(BaseModel, extra='forbid'):
    source_type: Literal['telegram', 'rss', 'website', 'x']
    external_id: str = Field(min_length=1, max_length=200)
    canonical_url: Optional[str] = Field(default=None, max_length=500)
    name: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=50, ge=0, le=100)
    trust_rating: int = Field(default=50, ge=0, le=100)
    processing_mode: Literal['auto', 'review'] = 'auto'

class SourcePatch(BaseModel, extra='forbid'):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    canonical_url: Optional[str] = Field(default=None, max_length=500)
    priority: Optional[int] = Field(default=None, ge=0, le=100)
    trust_rating: Optional[int] = Field(default=None, ge=0, le=100)
    processing_mode: Optional[Literal['auto', 'review']] = None
    is_active: Optional[bool] = None

class SourceResolve(BaseModel, extra='forbid'):
    external_id: str = Field(min_length=1, max_length=200)

@app.get("/api/sources")
def get_sources(is_active: Optional[bool] = None):
    return db.get_sources(is_active)

@app.get("/api/sources/{source_id}")
def get_source(source_id: int):
    src = db.get_source(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="Source not found")
    return src

def _invalidate_source_cache():
    try:
        from telegram_listener import source_cache
        source_cache.invalidate()
    except Exception as e:
        logger.warning(f"Could not invalidate source cache: {e}")

@app.post("/api/sources", status_code=201)
def create_source(req: SourceCreate):
    try:
        src = db.add_source(
            source_type=req.source_type,
            external_id=req.external_id,
            name=req.name,
            canonical_url=req.canonical_url,
            priority=req.priority,
            trust_rating=req.trust_rating,
            processing_mode=req.processing_mode
        )
        _invalidate_source_cache()
        return src
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Source with this external_id already exists")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

@app.patch("/api/sources/{source_id}")
def update_source(source_id: int, req: SourcePatch):
    try:
        src = db.update_source(source_id, req.model_dump(exclude_unset=True))
        if not src:
            raise HTTPException(status_code=404, detail="Source not found")
        _invalidate_source_cache()
        return src
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Conflict updating source")
    except ValueError as e:
        if "Cannot activate source with resolution_status='unresolved'" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=422, detail=str(e))

@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    src = db.deactivate_source(source_id)
    if not src:
        # Check if exists
        if not db.get_source(source_id):
            raise HTTPException(status_code=404, detail="Source not found")
        # Idempotent: already inactive, but get_source exists
        src = db.get_source(source_id)
    _invalidate_source_cache()
    return src

@app.post("/api/sources/{source_id}/resolve")
def resolve_source(source_id: int, req: SourceResolve):
    try:
        src = db.resolve_source(source_id, req.external_id)
        if not src:
            raise HTTPException(status_code=404, detail="Source not found")
        _invalidate_source_cache()
        return src
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Resolved external_id already exists")
    except ValueError as e:
        err = str(e)
        if "only for telegram" in err:
            raise HTTPException(status_code=422, detail=err)
        if "already resolved" in err:
            raise HTTPException(status_code=409, detail=err)
        raise HTTPException(status_code=422, detail=err)

@app.post("/api/sources/{source_id}/poll_now", status_code=202)
def poll_now_endpoint(source_id: int):
    status = db.poll_now(source_id)
    if status == "missing":
        raise HTTPException(status_code=404, detail="Source not found")
    if status == "conflict":
        raise HTTPException(status_code=409, detail="Source cannot be polled currently (inactive, unresolved, or active lease)")
    return {"status": "queued"}

@app.post("/api/telegram/fetch-history")
async def fetch_tg_history(req: FetchHistoryRequest):
    import telegram_listener
    if telegram_listener.history_fetch_queue is None:
        raise HTTPException(status_code=503, detail="Telegram listener is not running or not ready.")
    
    try:
        telegram_listener.history_fetch_queue.put_nowait({
            "messages": req.messages_limit,
            "channels": req.channels_limit
        })
        return {"status": "ok", "message": "Fetch task queued successfully."}
    except asyncio.QueueFull:
        raise HTTPException(
            status_code=409,
            detail="Telegram history fetch is already queued.",
        )
    except Exception:
        logger.exception("Failed to queue Telegram history fetch [SAFE_ERR_HISTORY_QUEUE]")
        raise HTTPException(
            status_code=500,
            detail="Failed to queue Telegram history fetch.",
        )

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
