import sqlite3
import json
from typing import Optional, List, Dict, Any, Set
from loguru import logger
from config import settings
import datetime

class InvalidStateTransitionError(Exception):
    pass

class InvalidUpdateColumnError(Exception):
    pass

class AmbiguousPublishStateError(Exception):
    pass

ALLOWED_TRANSITIONS = {
    ("new", "processing"),
    ("processing", "review"),
    ("processing", "approved"),
    ("processing", "ignored"),
    ("processing", "failed"),
    ("review", "approved"),
    ("review", "ignored"),
    ("approved", "publishing"),
    ("publishing", "published"),
    ("publishing", "approved"),
    ("publishing", "failed"),
}

ALLOWED_UPDATE_COLUMNS = {
    "rewritten_text", "status", "reason", "confidence",
    "image_prompt", "sentiment", "category",
    "scheduled_at", "last_error", "retry_count", "last_retry_at",
    "updated_at", "media_status", "media_error", "media_path",
    "media_provider", "media_created_at", "media_mime_type",
    "media_size_bytes", "media_width", "media_height",
    "audit_status", "audit_decision", "audit_score", "audit_result",
    "audit_error_code", "audit_model", "revision_count", "audited_at"
}

class Database:
    def __init__(self, db_path: str = settings.db_path):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        
        # Увімкнення WAL для конкурентного доступу
        conn.execute('PRAGMA journal_mode=WAL;')
        # Рекомендується з WAL для кращої швидкості без втрати надійності
        conn.execute('PRAGMA synchronous=NORMAL;')
        # Вмикаємо перевірку зовнішніх ключів на кожному з'єднанні
        conn.execute('PRAGMA foreign_keys = ON;')
        
        return conn

    def _init_db(self):
        """Ініціалізація таблиць та міграції."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Schema migration
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS processed_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    source_channel TEXT NOT NULL,
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_id, source_channel)
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    processed_message_id INTEGER,
                    original_text TEXT NOT NULL,
                    rewritten_text TEXT,
                    reason TEXT,
                    confidence REAL,
                    status TEXT DEFAULT 'new',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (processed_message_id) REFERENCES processed_messages (id)
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS published_tweets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER,
                    tweet_id TEXT NOT NULL,
                    published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (draft_id) REFERENCES drafts (id)
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS semantic_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS migration_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    migration_name TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    source_row_id INTEGER NOT NULL,
                    original_row_json TEXT NOT NULL,
                    conflict_reason TEXT NOT NULL,
                    backed_up_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(migration_name, source_table, source_row_id)
                )
            ''')
            
            new_columns = [
                "scheduled_at DATETIME NULL",
                "retry_count INTEGER DEFAULT 0",
                "last_error TEXT NULL",
                "last_retry_at DATETIME NULL",
                "media_path TEXT NULL",
                "image_prompt TEXT NULL",
                "media_status TEXT NOT NULL DEFAULT 'none'",
                "media_error TEXT NULL",
                "media_provider TEXT NULL",
                "media_created_at TEXT NULL",
                "media_mime_type TEXT NULL",
                "media_size_bytes INTEGER NULL",
                "media_width INTEGER NULL",
                "media_height INTEGER NULL",
                "sentiment TEXT NULL",
                "category TEXT NULL"
            ]
            for col in new_columns:
                try:
                    cursor.execute(f"ALTER TABLE drafts ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass

            conn.commit()
            
            # Транзакція для логічних міграцій
            cursor.execute('BEGIN IMMEDIATE')
            
            # 2. Pending migration
            cursor.execute("UPDATE drafts SET status = 'review' WHERE status = 'pending'")
            
            # 3. Duplicate backup/deduplication

            # Note: published_tweets has published_at, not created_at, fixing ORDER BY to published_at
            # Actually just fetch all duplicates
            cursor.execute("""
                SELECT id, draft_id, tweet_id, published_at FROM published_tweets
                WHERE tweet_id IN (
                    SELECT tweet_id FROM published_tweets GROUP BY tweet_id HAVING COUNT(*) > 1
                )
                ORDER BY published_at ASC, id ASC
            """)
            rows = cursor.fetchall()
            seen_tweets = set()
            for r in rows:
                if r['tweet_id'] not in seen_tweets:
                    seen_tweets.add(r['tweet_id'])
                else:
                    # Duplicate! Backup and delete
                    row_dict = dict(r)
                    cursor.execute(
                        "INSERT OR IGNORE INTO migration_conflicts (migration_name, source_table, source_row_id, original_row_json, conflict_reason) VALUES (?, ?, ?, ?, ?)",
                        ("phase2_dedup", "published_tweets", r["id"], json.dumps(row_dict), "duplicate tweet_id")
                    )
                    cursor.execute("DELETE FROM published_tweets WHERE id = ?", (r["id"],))

            # Deduplicate published_tweets by draft_id
            cursor.execute("""
                SELECT id, draft_id, tweet_id, published_at FROM published_tweets
                WHERE draft_id IN (
                    SELECT draft_id FROM published_tweets GROUP BY draft_id HAVING COUNT(*) > 1
                )
                ORDER BY published_at ASC, id ASC
            """)
            rows = cursor.fetchall()
            seen_drafts = set()
            for r in rows:
                if r['draft_id'] not in seen_drafts:
                    seen_drafts.add(r['draft_id'])
                else:
                    row_dict = dict(r)
                    cursor.execute(
                        "INSERT OR IGNORE INTO migration_conflicts (migration_name, source_table, source_row_id, original_row_json, conflict_reason) VALUES (?, ?, ?, ?, ?)",
                        ("phase2_dedup", "published_tweets", r["id"], json.dumps(row_dict), "duplicate draft_id")
                    )
                    cursor.execute("DELETE FROM published_tweets WHERE id = ?", (r["id"],))

            # 4. Phase 3 Migration
            cursor.execute("PRAGMA table_info(drafts)")
            existing_columns = {row['name'] for row in cursor.fetchall()}
            
            phase3_columns = {
                "audit_status": "TEXT DEFAULT NULL",
                "audit_decision": "TEXT DEFAULT NULL",
                "audit_score": "REAL DEFAULT NULL",
                "audit_result": "TEXT DEFAULT NULL",
                "audit_error_code": "TEXT DEFAULT NULL",
                "audit_model": "TEXT DEFAULT NULL",
                "audited_at": "TIMESTAMP DEFAULT NULL",
                "revision_count": "INTEGER NOT NULL DEFAULT 0"
            }
            
            for col_name, col_def in phase3_columns.items():
                if col_name not in existing_columns:
                    cursor.execute(f"ALTER TABLE drafts ADD COLUMN {col_name} {col_def}")
                    
            # 5. Unique indexes creation
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_drafts_status_created ON drafts(status, created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_drafts_status_scheduled ON drafts(status, scheduled_at)')
            cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_published_tweets_tweet_id ON published_tweets(tweet_id)')
            cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_published_tweets_draft_id ON published_tweets(draft_id)')
            
            conn.commit()
            
            # 6. Transient status recovery
            self._recover_stuck_drafts(cursor)
            conn.commit()
            
            logger.debug(f"Database initialized at {self.db_path}")

            if self.get_setting("publish_delay_minutes") is None:
                self.set_setting("publish_delay_minutes", 45)
            if self.get_setting("publish_jitter_percent") is None:
                self.set_setting("publish_jitter_percent", 15)
            if self.get_setting("max_retries") is None:
                self.set_setting("max_retries", 3)
            if self.get_setting("scheduler_check_interval_seconds") is None:
                self.set_setting("scheduler_check_interval_seconds", 60)
            if self.get_setting("semantic_top_k") is None:
                self.set_setting("semantic_top_k", 2)
            if self.get_setting("minimum_similarity") is None:
                self.set_setting("minimum_similarity", 0.82)

    def _recover_stuck_drafts(self, cursor: sqlite3.Cursor):
        cursor.execute("UPDATE drafts SET status = 'new', updated_at = CURRENT_TIMESTAMP WHERE status = 'processing'")
        cursor.execute("UPDATE drafts SET status = 'review', last_error = 'Recovered after crash during publishing', updated_at = CURRENT_TIMESTAMP WHERE status = 'publishing'")
        # Also media recovery
        cursor.execute(
            "UPDATE drafts SET media_status = 'failed', "
            "media_error = 'Process restart during generation' "
            "WHERE media_status = 'generating'"
        )

    def _transition_draft(self, conn: sqlite3.Connection, draft_id: int, expected_statuses: Set[str], new_status: str, updates: dict = None) -> bool:
        cursor = conn.cursor()
        
        expected_tuple = tuple(expected_statuses)
        
        # Check current status
        cursor.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,))
        row = cursor.fetchone()
        if not row:
            return False
            
        current_status = row['status']
        if current_status not in expected_tuple:
            return False
            
        if (current_status, new_status) not in ALLOWED_TRANSITIONS:
            raise InvalidStateTransitionError(f"Forbidden transition: {current_status} -> {new_status}")
            
        updates = updates or {}
        for k in updates.keys():
            if k not in ALLOWED_UPDATE_COLUMNS:
                raise InvalidUpdateColumnError(f"Column '{k}' is not in allowlist.")
                
        set_clauses = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        params = [new_status]
        
        for k, v in updates.items():
            set_clauses.append(f"{k} = ?")
            params.append(v)
            
        params.extend([draft_id] + list(expected_tuple))
        placeholders = ",".join(["?"] * len(expected_tuple))
        
        query = f"UPDATE drafts SET {', '.join(set_clauses)} WHERE id = ? AND status IN ({placeholders})"
        cursor.execute(query, params)
        return cursor.rowcount == 1

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row['value'])
                except (json.JSONDecodeError, ValueError, TypeError):
                    return row['value']
            return default

    def set_setting(self, key: str, value: Any):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            str_val = json.dumps(value) if isinstance(value, (dict, list, bool, int, float)) else str(value)
            cursor.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP",
                (key, str_val, str_val)
            )
            conn.commit()

    def add_message_and_draft(self, source_id: str, source_channel: str, original_text: str) -> Optional[int]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                conn.execute('BEGIN IMMEDIATE')
                cursor.execute(
                    "INSERT INTO processed_messages (source_id, source_channel) VALUES (?, ?)",
                    (str(source_id), str(source_channel))
                )
                processed_id = cursor.lastrowid
                
                cursor.execute(
                    "INSERT INTO drafts (processed_message_id, original_text, status) VALUES (?, ?, 'new')",
                    (processed_id, original_text)
                )
                draft_id = cursor.lastrowid
                conn.commit()
                return draft_id
            except sqlite3.IntegrityError:
                conn.rollback()
                return None
            except Exception:
                conn.rollback()
                raise

    def fetch_next_new_draft(self) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')
            cursor.execute("SELECT id FROM drafts WHERE status = 'new' ORDER BY created_at ASC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                return None
            draft_id = row['id']
            
            success = self._transition_draft(conn, draft_id, {"new"}, "processing")
            if not success:
                conn.rollback()
                return None
                
            cursor.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,))
            draft = cursor.fetchone()
            conn.commit()
            return dict(draft) if draft else None

    def get_draft(self, draft_id: int) -> Optional[dict]:
        """Gets a draft by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def complete_ai_processing(self, draft_id: int, new_status: str, updates: dict = None) -> bool:
        """
        Атомарно зберігає фінальний текст, результати аудиту та переводить статус.
        """
        with self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            success = self._transition_draft(conn, draft_id, {"processing"}, new_status, updates)
            conn.commit()
            return success

    def approve_draft(self, draft_id: int, updates: dict = None) -> bool:
        with self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # review -> approved
            updates = updates or {}
            updates["retry_count"] = 0
            success = self._transition_draft(conn, draft_id, {"review"}, "approved", updates)
            conn.commit()
            return success

    def ignore_draft(self, draft_id: int) -> bool:
        with self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # review -> ignored
            success = self._transition_draft(conn, draft_id, {"review"}, "ignored", {"retry_count": 0})
            conn.commit()
            return success

    def fetch_next_approved_draft_for_publish(self) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')
            cursor.execute("SELECT id FROM drafts WHERE status = 'approved' AND (scheduled_at IS NULL OR scheduled_at <= CURRENT_TIMESTAMP) ORDER BY created_at ASC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                return None
            draft_id = row['id']
            
            success = self._transition_draft(conn, draft_id, {"approved"}, "publishing")
            if not success:
                conn.rollback()
                return None
                
            cursor.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,))
            draft = cursor.fetchone()
            conn.commit()
            return dict(draft) if draft else None

    def record_publish_success(self, draft_id: int, tweet_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                conn.execute('BEGIN IMMEDIATE')
                success = self._transition_draft(conn, draft_id, {"publishing"}, "published")
                if not success:
                    conn.rollback()
                    raise AmbiguousPublishStateError(f"Draft {draft_id} is not in publishing state.")
                    
                cursor.execute(
                    "INSERT INTO published_tweets (draft_id, tweet_id) VALUES (?, ?)",
                    (draft_id, str(tweet_id))
                )
                conn.commit()
                return True
            except AmbiguousPublishStateError:
                raise
            except Exception as e:
                conn.rollback()
                raise AmbiguousPublishStateError(f"Failed to record publish success for draft {draft_id}: {str(e)}") from e

    def schedule_publish_retry(self, draft_id: int, scheduled_at_str: str, error_msg: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')
            
            cursor.execute("SELECT retry_count FROM drafts WHERE id = ?", (draft_id,))
            row = cursor.fetchone()
            if not row:
                return False
            current_retry = row['retry_count'] + 1
            
            success = self._transition_draft(
                conn, 
                draft_id, 
                {"publishing"}, 
                "approved", 
                {
                    "scheduled_at": scheduled_at_str, 
                    "retry_count": current_retry,
                    "last_error": error_msg,
                    "last_retry_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                }
            )
            conn.commit()
            return success

    def mark_publish_failed(self, draft_id: int, error_msg: str) -> bool:
        with self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            success = self._transition_draft(
                conn, 
                draft_id, 
                {"publishing"}, 
                "failed", 
                {
                    "last_error": error_msg,
                    "retry_count": 0
                }
            )
            conn.commit()
            return success

    def get_drafts_by_status(self, statuses: List[str]) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ', '.join(['?'] * len(statuses))
            query = f"SELECT * FROM drafts WHERE status IN ({placeholders}) ORDER BY created_at DESC"
            cursor.execute(query, statuses)
            return [dict(row) for row in cursor.fetchall()]

    def get_last_published_time(self) -> Optional[str]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT updated_at FROM drafts WHERE status IN ('published', 'publishing') ORDER BY updated_at DESC LIMIT 1")
            row = cursor.fetchone()
            return row['updated_at'] if row else None

    def update_draft_text(self, draft_id: int, new_text: str):
        # Allow updating text in review state
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE drafts SET rewritten_text = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status IN ('review')", (new_text, draft_id))
            conn.commit()

    def get_analytics(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE date(created_at) = date('now')")
            total_today = cursor.fetchone()['c']
            
            cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE status IN ('published', 'approved', 'publishing') AND date(updated_at) = date('now')")
            published_today = cursor.fetchone()['c']
            
            cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE status IN ('ignored', 'failed') AND date(updated_at) = date('now')")
            ignored_today = cursor.fetchone()['c']
            
            cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE status IN ('review')")
            pending_total = cursor.fetchone()['c']
            
            return {
                "total_today": total_today,
                "published_today": published_today,
                "ignored_today": ignored_today,
                "pending_total": pending_total
            }

db = Database()
