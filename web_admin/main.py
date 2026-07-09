import sys
import os
from pathlib import Path
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Додаємо кореневу папку в sys.path, щоб імпортувати наші модулі
sys.path.append(str(Path(__file__).parent.parent))

from database import db
from twitter_publisher import publisher
from semantic_memory import semantic_memory
from loguru import logger

app = FastAPI(title="Twitter AI Bot Admin")

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

class UpdateDraftRequest(BaseModel):
    rewritten_text: str

class SettingsRequest(BaseModel):
    system_prompt: str
    shadow_mode: bool
    publish_delay_minutes: int
    publish_jitter_percent: int
    max_retries: int
    scheduler_check_interval_seconds: int

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
    logger.info(f"Web Admin: Approving draft {draft_id} for Scheduler")
    db.update_draft_status(draft_id, "approved")
    
    # Витягуємо текст для збереження у пам'ять
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT rewritten_text FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        if row and row['rewritten_text']:
            semantic_memory.save(row['rewritten_text'])
            
    return {"status": "success"}

@app.post("/api/drafts/{draft_id}/ignore")
def ignore_draft(draft_id: int):
    logger.info(f"Web Admin: Ignoring draft {draft_id}")
    db.update_draft_status(draft_id, "ignored")
    return {"status": "success"}

@app.post("/api/drafts/{draft_id}/update")
def update_draft(draft_id: int, request: UpdateDraftRequest):
    logger.info(f"Web Admin: Updating draft {draft_id}")
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE drafts SET rewritten_text = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (request.rewritten_text, draft_id)
        )
        conn.commit()
    return {"status": "success"}

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
    
    return templates.TemplateResponse(
        request=request, name="settings.html", context={
            "request": request, 
            "system_prompt": current_prompt, 
            "shadow_mode": shadow_mode,
            "publish_delay_minutes": publish_delay_minutes,
            "publish_jitter_percent": publish_jitter_percent,
            "max_retries": max_retries,
            "scheduler_check_interval_seconds": scheduler_check_interval_seconds
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
