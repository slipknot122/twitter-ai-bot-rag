import sys
import os
import asyncio
import datetime
import uuid
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
from auditor_config import (
    AuditorConfig,
    available_auditor_models,
    default_auditor_config,
    effective_auditor_prompt,
    load_auditor_config,
    save_auditor_config,
)

if settings.web_admin_host not in {"127.0.0.1", "localhost", "::1"}:
    raise RuntimeError("Web admin must bind to localhost for security reasons.")

app = FastAPI(title="Twitter AI Bot Admin")

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Роздача media файлів через /media/{filename}
media_dir = Path(__file__).parent.parent / "media"
media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")

# Background tasks set — захист від garbage collection (рекомендація рев'юера)
_background_tasks: set[asyncio.Task] = set()

class UpdateDraftRequest(BaseModel):
    rewritten_text: str

class FetchHistoryRequest(BaseModel):
    messages_limit: int = Field(ge=1, le=20)
    channels_limit: int = Field(ge=1, le=50)

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
            raise HTTPException(status_code=404, detail="Чернетку не знайдено")
        if row['status'] not in ['review', 'pending']:
            raise HTTPException(status_code=409, detail="Схвалити можна лише чернетку зі статусом перевірки")
            
        text = row['rewritten_text']
        from utils import validate_post_text, ValidationError
        try:
            validated_text = validate_post_text(text)
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    logger.info(f"Web Admin: Approving draft {draft_id} for Scheduler")
    success = db.approve_draft(draft_id, {"rewritten_text": validated_text})
    if not success:
        raise HTTPException(status_code=409, detail="Не вдалося схвалити чернетку через конфлікт або неприпустимий стан")
            
    return {"status": "success"}

@app.post("/api/drafts/{draft_id}/ignore")
def ignore_draft(draft_id: int):
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Чернетку не знайдено")
        if row['status'] not in ['review', 'pending']:
            raise HTTPException(status_code=400, detail="Відхилити можна лише чернетку зі статусом перевірки")

    logger.info(f"Web Admin: Ignoring draft {draft_id}")
    success = db.ignore_draft(draft_id)
    if not success:
        raise HTTPException(status_code=409, detail="Не вдалося перевести чернетку у відхилений стан")
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
        raise HTTPException(status_code=404, detail="Чернетку не знайдено")
    if status == "INVALID_PROMPT":
        raise HTTPException(status_code=422, detail="Промпт зображення має містити від 20 до 1500 символів")
    if status == "CONFLICT":
        raise HTTPException(status_code=409, detail="Дію заборонено для поточного стану або вона вже виконується")

    return {"draft_id": draft_id, "media_status": "pending"}


@app.post("/api/drafts/{draft_id}/image/cancel")
def cancel_image(draft_id: int):
    """Скасовує генерацію медіа."""
    with db._get_connection() as conn:
        row = conn.cursor().execute("SELECT id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Чернетку не знайдено")
            
    success = db.cancel_media_generation(draft_id)
    if not success:
        raise HTTPException(status_code=409, detail="У поточному стані генерацію медіа не можна скасувати")
    return {"status": "success"}

@app.delete("/api/drafts/{draft_id}/image")
def delete_image(draft_id: int):
    """Безпечне видалення зображення."""
    status = db.delete_media(draft_id)
    if status == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="Чернетку не знайдено")
    if status == "CONFLICT":
        raise HTTPException(status_code=409, detail="У поточному стані медіа не можна видалити")
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
        raise HTTPException(status_code=404, detail="Чернетку не знайдено")

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


@app.get("/auditor", response_class=HTMLResponse)
def auditor_page(request: Request):
    config = load_auditor_config(db)
    return templates.TemplateResponse(
        request=request,
        name="auditor.html",
        context={
            "request": request,
            "auditor_config": config.model_dump(),
            "models": available_auditor_models(),
        },
    )


@app.get("/api/auditor/config")
def get_auditor_config():
    config = load_auditor_config(db)
    return {
        "config": config.model_dump(),
        "models": available_auditor_models(),
        "effective_prompt": effective_auditor_prompt(config),
    }


@app.post("/api/auditor/config")
def update_auditor_config(req: AuditorConfig):
    selected_model = next(
        (model for model in available_auditor_models() if model["id"] == req.model),
        None,
    )
    if selected_model is None or not selected_model["available"]:
        raise HTTPException(status_code=422, detail="Обрану модель аудитора не налаштовано.")
    save_auditor_config(db, req)
    return {
        "status": "ok",
        "config": req.model_dump(),
        "effective_prompt": effective_auditor_prompt(req),
    }


@app.post("/api/auditor/preview")
def preview_auditor_config(req: AuditorConfig):
    return {"effective_prompt": effective_auditor_prompt(req)}


@app.post("/api/auditor/reset")
def reset_auditor_config():
    config = default_auditor_config()
    save_auditor_config(db, config)
    return {
        "status": "ok",
        "config": config.model_dump(),
        "effective_prompt": effective_auditor_prompt(config),
    }


# --- Phase 5: Source CRUD API ---

@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    sources = [_safe_source(source) for source in db.get_sources()]
    counts = {
        "all": len(sources),
        "active": sum(1 for source in sources if source["is_active"] and not source.get("archived_at")),
        "archived": sum(1 for source in sources if source.get("archived_at")),
    }
    return templates.TemplateResponse(
        request=request,
        name="sources.html",
        context={"request": request, "sources": sources, "counts": counts, "active_page": "sources"},
    )


class SourceCreate(BaseModel, extra='forbid'):
    source_type: Literal['telegram', 'rss', 'website', 'x']
    external_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    telegram_input: Optional[str] = Field(default=None, min_length=1, max_length=300)
    allow_join: bool = False
    canonical_url: Optional[str] = Field(default=None, max_length=500)
    name: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=50, ge=0, le=100)
    trust_rating: int = Field(default=50, ge=0, le=100)
    processing_mode: Literal['auto', 'review'] = 'auto'
    poll_interval_minutes: int = Field(default=30, ge=5, le=1440)
    include_keywords: list[str] = Field(default_factory=list, max_length=50)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=50)

class SourcePatch(BaseModel, extra='forbid'):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    canonical_url: Optional[str] = Field(default=None, max_length=500)
    priority: Optional[int] = Field(default=None, ge=0, le=100)
    trust_rating: Optional[int] = Field(default=None, ge=0, le=100)
    processing_mode: Optional[Literal['auto', 'review']] = None
    is_active: Optional[bool] = None
    poll_interval_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    include_keywords: Optional[list[str]] = Field(default=None, max_length=50)
    exclude_keywords: Optional[list[str]] = Field(default=None, max_length=50)

class SourceResolve(BaseModel, extra='forbid'):
    external_id: str = Field(min_length=1, max_length=200)


class TelegramResolveRequest(BaseModel, extra='forbid'):
    telegram_input: Optional[str] = Field(default=None, min_length=1, max_length=300)
    allow_join: bool = False

def _safe_source(source: dict) -> dict:
    safe = dict(source)
    safe.pop("telegram_reference", None)
    return safe


@app.get("/api/sources")
def get_sources(is_active: Optional[bool] = None):
    return [_safe_source(source) for source in db.get_sources(is_active)]

@app.get("/api/sources/{source_id}")
def get_source(source_id: int):
    src = db.get_source(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="Джерело не знайдено")
    return _safe_source(src)

def _invalidate_source_cache():
    try:
        from telegram_listener import source_cache
        source_cache.invalidate()
    except Exception as e:
        logger.warning(f"Could not invalidate source cache: {e}")


def _queue_telegram_resolution(source_id: int, telegram_input: str, allow_join: bool) -> str:
    import telegram_listener
    queue = telegram_listener.telegram_resolve_queue
    if queue is None:
        db.mark_telegram_resolution(source_id, "failed", "Слухач Telegram зараз недоступний. Спробуйте перевірити пізніше.")
        return "unavailable"
    try:
        queue.put_nowait({"source_id": source_id, "telegram_input": telegram_input, "allow_join": allow_join})
        db.mark_telegram_resolution(source_id, "pending", None)
        return "queued"
    except asyncio.QueueFull:
        db.mark_telegram_resolution(source_id, "failed", "Черга перевірки Telegram заповнена. Спробуйте пізніше.")
        return "full"


@app.post("/api/sources", status_code=201)
def create_source(req: SourceCreate):
    try:
        telegram_reference = None
        telegram_display = None
        external_id = req.external_id
        parsed = None
        if req.source_type == 'telegram':
            from telegram_listener import parse_telegram_reference
            telegram_value = req.telegram_input or req.external_id
            if not telegram_value:
                raise ValueError("Вкажіть посилання, логін або числовий ID Telegram-групи")
            parsed = parse_telegram_reference(telegram_value)
            if parsed.kind == 'id':
                external_id = parsed.value
            else:
                telegram_reference = parsed.value
                telegram_display = parsed.display
                external_id = f"pending:{uuid.uuid4().hex}"
        elif not external_id:
            raise ValueError("Вкажіть зовнішній ID джерела")

        src = db.add_source(
            source_type=req.source_type,
            external_id=external_id,
            name=req.name,
            canonical_url=req.canonical_url,
            priority=req.priority,
            trust_rating=req.trust_rating,
            processing_mode=req.processing_mode,
            poll_interval_minutes=req.poll_interval_minutes,
            include_keywords=req.include_keywords,
            exclude_keywords=req.exclude_keywords,
            telegram_reference=telegram_reference,
            telegram_display=telegram_display,
        )
        if parsed is not None and parsed.kind != 'id':
            queue_value = f"https://t.me/+{parsed.value}" if parsed.kind == 'invite' else f"@{parsed.value}"
            _queue_telegram_resolution(src['id'], queue_value, req.allow_join)
            src = db.get_source(src['id'])
        _invalidate_source_cache()
        return _safe_source(src)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Джерело з таким зовнішнім ID уже існує")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

@app.patch("/api/sources/{source_id}")
def update_source(source_id: int, req: SourcePatch):
    try:
        src = db.update_source(source_id, req.model_dump(exclude_unset=True))
        if not src:
            raise HTTPException(status_code=404, detail="Джерело не знайдено")
        _invalidate_source_cache()
        return src
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Конфлікт під час оновлення джерела")
    except ValueError as e:
        if "Cannot activate source with resolution_status='unresolved'" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=422, detail=str(e))

@app.get("/api/sources/{source_id}/usage")
def source_usage(source_id: int):
    usage = db.get_source_usage(source_id)
    if usage is None:
        raise HTTPException(status_code=404, detail="Джерело не знайдено")
    return usage


@app.post("/api/sources/{source_id}/archive")
def archive_source(source_id: int):
    src = db.archive_source(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="Джерело не знайдено")
    _invalidate_source_cache()
    return src


@app.post("/api/sources/{source_id}/restore")
def restore_source(source_id: int):
    src = db.restore_source(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="Джерело не знайдено")
    _invalidate_source_cache()
    return src


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int, permanent: bool = False, detach_drafts: bool = False):
    if not permanent:
        src = db.archive_source(source_id)
        if not src:
            raise HTTPException(status_code=404, detail="Джерело не знайдено")
        _invalidate_source_cache()
        return src
    result = db.delete_source_permanently(source_id, detach_drafts=detach_drafts)
    if result == "missing":
        raise HTTPException(status_code=404, detail="Джерело не знайдено")
    if result == "conflict":
        usage = db.get_source_usage(source_id)
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Джерело має пов’язані чернетки. Архівуйте його або підтвердьте відв’язування.",
                "draft_count": usage["draft_count"],
                "actions": ["archive", "detach_and_delete"],
            },
        )
    _invalidate_source_cache()
    return {"status": "deleted", "source_id": source_id}

@app.post("/api/sources/{source_id}/resolve")
def resolve_source(source_id: int, req: SourceResolve):
    try:
        src = db.resolve_source(source_id, req.external_id)
        if not src:
            raise HTTPException(status_code=404, detail="Джерело не знайдено")
        _invalidate_source_cache()
        return src
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Уточнений зовнішній ID уже використовується")
    except ValueError as e:
        err = str(e)
        if "only for telegram" in err:
            raise HTTPException(status_code=422, detail=err)
        if "already resolved" in err:
            raise HTTPException(status_code=409, detail=err)
        raise HTTPException(status_code=422, detail=err)

@app.post("/api/sources/{source_id}/verify-telegram", status_code=202)
def verify_telegram_source(source_id: int, req: TelegramResolveRequest):
    source = db.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Джерело не знайдено")
    if source['source_type'] != 'telegram':
        raise HTTPException(status_code=422, detail="Перевірка доступна лише для Telegram-джерел")
    try:
        from telegram_listener import parse_telegram_reference
        raw_reference = req.telegram_input
        if not raw_reference and source.get('telegram_reference'):
            stored = source['telegram_reference']
            raw_reference = (
                f"https://t.me/+{stored}"
                if source.get('telegram_display') == 'Приватне запрошення Telegram'
                else f"@{stored}"
            )
        if not raw_reference:
            raise ValueError("Вкажіть нове посилання або логін Telegram-групи")
        parsed = parse_telegram_reference(raw_reference)
        if parsed.kind == 'id':
            resolved = db.resolve_source(source_id, parsed.value)
            _invalidate_source_cache()
            return {"status": "resolved", "source": _safe_source(resolved)}
        if parsed.kind == 'invite' and not req.allow_join:
            db.set_telegram_reference(source_id, parsed.value, parsed.display)
            db.mark_telegram_resolution(source_id, "join_required", "Підтвердьте вступ Telegram-акаунта до приватної групи")
            return {"status": "join_required"}
        db.set_telegram_reference(source_id, parsed.value, parsed.display)
        queue_value = f"https://t.me/+{parsed.value}" if parsed.kind == 'invite' else f"@{parsed.value}"
        status = _queue_telegram_resolution(source_id, queue_value, req.allow_join)
        return {"status": status, "source": _safe_source(db.get_source(source_id))}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Ця Telegram-група вже додана")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/sources/{source_id}/poll_now", status_code=202)
def poll_now_endpoint(source_id: int):
    status = db.poll_now(source_id)
    if status == "missing":
        raise HTTPException(status_code=404, detail="Джерело не знайдено")
    if status == "conflict":
        raise HTTPException(status_code=409, detail="Зараз джерело не можна перевірити: воно неактивне, не уточнене або вже обробляється")
    return {"status": "queued"}

@app.post("/api/telegram/fetch-history")
async def fetch_tg_history(req: FetchHistoryRequest):
    import telegram_listener
    if telegram_listener.history_fetch_queue is None:
        raise HTTPException(status_code=503, detail="Слухач Telegram не запущено або він ще не готовий.")
    
    try:
        telegram_listener.history_fetch_queue.put_nowait({
            "messages": req.messages_limit,
            "channels": req.channels_limit
        })
        return {"status": "ok", "message": "Завантаження успішно додано до черги."}
    except asyncio.QueueFull:
        raise HTTPException(
            status_code=409,
            detail="Завантаження історії Telegram уже додано до черги.",
        )
    except Exception:
        logger.exception("Failed to queue Telegram history fetch [SAFE_ERR_HISTORY_QUEUE]")
        raise HTTPException(
            status_code=500,
            detail="Не вдалося додати завантаження історії Telegram до черги.",
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
    db_status = "До��тупна"
    try:
        with db._get_connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        db_status = "Помилка"
        
    return templates.TemplateResponse(
        request=request, name="status.html", context={
            "request": request,
            "gemini_status": "Налаштовано" if gemini_configured else "Відсутнє",
            "telegram_status": "Налаштовано" if telegram_configured else "Відсутнє",
            "twitter_status": "Налаштовано" if twitter_configured else "Відсутнє",
            "cloudflare_status": "Налаштовано" if cloudflare_configured else "Відсутнє",
            "pollinations_status": "Налаштовано" if pollinations_configured else "Відсутнє",
            "twitter_dry_run": settings.twitter_dry_run,
            "admin_bind": settings.web_admin_host,
            "db_status": db_status,
            "media_enabled": settings.media_generation_enabled,
            "media_providers": settings.media_provider_order
        }
    )
