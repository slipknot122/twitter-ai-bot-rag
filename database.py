import sqlite3
import json
import re
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


def normalize_telegram_id(raw) -> str:
    """Нормалізує Telegram channel ID до канонічного формату '-100...'.
    Піднімає ValueError при невалідному вводі (юзернейми, URL, порожні, нечислові)."""
    s = str(raw).strip()
    if not s:
        raise ValueError("Empty Telegram ID")

    # Якщо вже має префікс -100
    if s.startswith('-100'):
        abs_part = s[4:]
        if not abs_part.isdigit() or not abs_part:
            raise ValueError(f"Missing or invalid channel payload after -100")
        if len(abs_part) > 20:
            raise ValueError(f"Telegram ID too long")
        if int(abs_part) == 0:
            raise ValueError(f"Telegram ID cannot be zero")
        return s

    # Якщо починається з -, але не -100
    if s.startswith('-'):
        raise ValueError(f"Negative Telegram ID must start with -100")

    # Має складатися лише з цифр (positive bare ID)
    if not s.isdigit() or not s:
        raise ValueError(f"Invalid Telegram ID")

    if len(s) > 20:
        raise ValueError(f"Telegram ID too long")

    if int(s) == 0:
        raise ValueError(f"Telegram ID cannot be zero")

    return f"-100{s}"


def _validate_canonical_url(url: str, source_type: str) -> str:
    """Валідація та нормалізація canonical_url. Повертає очищений URL або піднімає ValueError."""
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("canonical_url is empty after stripping")
    # Перевірка на керуючі символи (всі < 0x20 та 0x7f)
    for ch in cleaned:
        if ord(ch) < 0x20 or ord(ch) == 0x7f:
            raise ValueError(f"canonical_url contains control character: {ord(ch):#x}")
    # Заборонені схеми для всіх типів
    lower = cleaned.lower()
    for scheme in ('javascript:', 'data:', 'file:'):
        if lower.startswith(scheme):
            raise ValueError(f"Forbidden URL scheme: {scheme}")
    # Перевірка на credentials (@ перед першого /)
    slash_pos = cleaned.find('/', cleaned.find('//') + 2) if '//' in cleaned else cleaned.find('/')
    at_pos = cleaned.find('@')
    if at_pos != -1 and (slash_pos == -1 or at_pos < slash_pos):
        raise ValueError("canonical_url contains credentials (@ before path)")
    # Валідація за типом джерела
    if source_type in ('rss', 'website'):
        if not cleaned.startswith('https://'):
            raise ValueError(f"canonical_url for '{source_type}' must start with 'https://'")
    elif source_type == 'x':
        if not (cleaned.startswith('https://x.com/') or cleaned.startswith('https://twitter.com/')):
            raise ValueError("canonical_url for 'x' must start with 'https://x.com/' or 'https://twitter.com/'")
    return cleaned


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
    "rewritten_text", "reason", "confidence",
    "image_prompt", "sentiment", "category",
    "scheduled_at", "last_error", "retry_count", "last_retry_at",
    "updated_at", "media_status", "media_error_code", "media_error_message", "media_path",
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

            cursor.execute("PRAGMA table_info(drafts)")
            existing_columns = {row['name'] for row in cursor.fetchall()}

            new_columns = {
                "scheduled_at": "DATETIME NULL",
                "retry_count": "INTEGER DEFAULT 0",
                "last_error": "TEXT NULL",
                "last_retry_at": "DATETIME NULL",
                "image_prompt": "TEXT NULL",
                "sentiment": "TEXT NULL",
                "category": "TEXT NULL",
                "media_status": "TEXT NOT NULL DEFAULT 'none'",
                "media_error_code": "TEXT NULL",
                "media_error_message": "TEXT NULL",
                "media_path": "TEXT NULL",
                "media_provider": "TEXT NULL",
                "media_created_at": "TEXT NULL",
                "media_requested_at": "TEXT NULL",
                "media_started_at": "TEXT NULL",
                "media_lease_expires_at": "TEXT NULL",
                "media_next_attempt_at": "TEXT NULL",
                "media_attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "media_generation_token": "TEXT NULL",
                "media_mime_type": "TEXT NULL",
                "media_size_bytes": "INTEGER NULL",
                "media_width": "INTEGER NULL",
                "media_height": "INTEGER NULL"
            }
            for col_name, col_def in new_columns.items():
                if col_name not in existing_columns:
                    cursor.execute(f"ALTER TABLE drafts ADD COLUMN {col_name} {col_def}")

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
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_drafts_media_status_requested ON drafts(media_status, media_requested_at)')
            cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_published_tweets_tweet_id ON published_tweets(tweet_id)')
            cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_published_tweets_draft_id ON published_tweets(draft_id)')

            conn.commit()

            # 6. Transient status recovery
            self._recover_stuck_drafts(cursor)
            conn.commit()

            # 7. Phase 5 migration — sources та snapshot-колонки
            self._migrate_phase5(cursor, conn)

            # 8. Phase 6 migration — ingestion state та leases
            self._migrate_phase6(cursor, conn)

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
            "UPDATE drafts SET media_status = 'pending', "
            "media_generation_token = NULL "
            "WHERE media_status = 'generating' "
            "AND media_lease_expires_at < ?",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(),)
        )

    def _migrate_phase5(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection):
        """Міграція Phase 5: таблиця sources та snapshot-колонки в drafts."""
        cursor.execute('BEGIN IMMEDIATE')

        # a) Таблиця sources
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sources (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type     TEXT NOT NULL CHECK(source_type IN ('telegram','rss','website','x')),
                external_id     TEXT NOT NULL,
                canonical_url   TEXT NULL,
                name            TEXT NOT NULL,
                priority        INTEGER NOT NULL DEFAULT 50 CHECK(priority >= 0 AND priority <= 100),
                trust_rating    INTEGER NOT NULL DEFAULT 50 CHECK(trust_rating >= 0 AND trust_rating <= 100),
                processing_mode TEXT NOT NULL DEFAULT 'auto' CHECK(processing_mode IN ('auto','review')),
                resolution_status TEXT NOT NULL DEFAULT 'resolved' CHECK(resolution_status IN ('resolved','unresolved')),
                is_active       INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(source_type, external_id)
            )
        ''')

        # b) Індекс на sources
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sources_active_type ON sources(is_active, source_type)')

        # c) Snapshot-колонки в drafts
        cursor.execute("PRAGMA table_info(drafts)")
        existing_columns = {row['name'] for row in cursor.fetchall()}

        phase5_columns = {
            "source_id": "INTEGER NULL REFERENCES sources(id) ON DELETE RESTRICT",
            "source_name_snapshot": "TEXT NULL",
            "source_priority_snapshot": "INTEGER NULL",
            "source_trust_snapshot": "INTEGER NULL",
            "source_processing_mode_snapshot": "TEXT NULL",
            "source_item_id": "TEXT NULL",
        }
        for col_name, col_def in phase5_columns.items():
            if col_name not in existing_columns:
                cursor.execute(f"ALTER TABLE drafts ADD COLUMN {col_name} {col_def}")

        # d) Індекси на drafts для source-зв'язків
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_drafts_source_id ON drafts(source_id)')
        cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_source_dedup ON drafts(source_id, source_item_id) WHERE source_id IS NOT NULL AND source_item_id IS NOT NULL')

        # e) Seed з settings.telegram_channels
        for raw_channel in settings.telegram_channels:
            try:
                normalized = normalize_telegram_id(raw_channel)
                cursor.execute(
                    "INSERT OR IGNORE INTO sources (source_type, external_id, name, resolution_status, is_active) VALUES ('telegram', ?, ?, 'resolved', 1)",
                    (normalized, str(raw_channel))
                )
            except ValueError:
                # Нечисловий (юзернейм/URL) — вставляємо як unresolved, inactive
                raw_str = str(raw_channel).strip()
                if raw_str:
                    cursor.execute(
                        "INSERT OR IGNORE INTO sources (source_type, external_id, name, resolution_status, is_active) VALUES ('telegram', ?, ?, 'unresolved', 0)",
                        (raw_str, raw_str)
                    )

        # f) Коміт
        conn.commit()

    def _migrate_phase6(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection):
        """Міграція Phase 6: worker_leases, source_poll_state, drafts publication fields."""
        cursor.execute('BEGIN IMMEDIATE')

        # a) worker_leases
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS worker_leases (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                worker_id TEXT NOT NULL,
                lease_token TEXT NOT NULL,
                heartbeat_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL
            )
        ''')

        # b) source_poll_state
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS source_poll_state (
                source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE RESTRICT,
                collector_status TEXT NOT NULL DEFAULT 'idle' CHECK(collector_status IN ('idle', 'queued', 'polling', 'healthy', 'backoff', 'degraded', 'unsupported', 'blocked_by_robots')),
                resolved_feed_url TEXT,
                validator_url TEXT,
                etag TEXT,
                last_modified TEXT,
                last_attempt_at TIMESTAMP,
                last_success_at TIMESTAMP,
                next_poll_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                initial_sync_completed_at TIMESTAMP,
                last_http_status INTEGER,
                consecutive_errors INTEGER DEFAULT 0,
                last_error_code TEXT,
                lease_token TEXT,
                lease_expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # c) drafts update
        cursor.execute("PRAGMA table_info(drafts)")
        existing_columns = {row['name'] for row in cursor.fetchall()}

        phase6_columns = {
            "source_published_at": "TIMESTAMP NULL",
            "source_updated_at": "TIMESTAMP NULL"
        }
        for col_name, col_def in phase6_columns.items():
            if col_name not in existing_columns:
                cursor.execute(f"ALTER TABLE drafts ADD COLUMN {col_name} {col_def}")

        # Fill poll state for active rss/website sources
        cursor.execute("""
            INSERT OR IGNORE INTO source_poll_state (source_id, next_poll_at)
            SELECT id, CURRENT_TIMESTAMP FROM sources
            WHERE source_type IN ('rss', 'website')
        """)

        conn.commit()

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
            cursor.execute("""
                SELECT id FROM drafts WHERE status = 'new'
                ORDER BY
                  (
                    COALESCE(source_priority_snapshot, 50)
                    + MAX(
                        0,
                        MIN(
                          100,
                          CAST(
                            COALESCE(
                              (julianday('now') - julianday(created_at)) * 24 * 60 / 10,
                              0
                            ) AS INTEGER
                          )
                        )
                      )
                  ) DESC,
                  julianday(created_at) ASC,
                  id ASC
                LIMIT 1
            """)
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

    def complete_ai_processing(self, draft_id: int, new_status: str, updates: dict = None, media_request: bool = False) -> bool:
        """
        Атомарно зберігає фінальний текст, результати аудиту та переводить статус.
        Також, якщо media_request=True, встановлює media_status='pending' і генерує generation_token.
        """
        import uuid
        with self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            success = self._transition_draft(conn, draft_id, {"processing"}, new_status, updates)
            if success and media_request:
                new_token = str(uuid.uuid4())
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE drafts
                    SET media_status = 'pending',
                        media_generation_token = ?,
                        media_requested_at = ?,
                        media_attempt_count = 0,
                        media_next_attempt_at = NULL,
                        media_error_code = NULL,
                        media_error_message = NULL,
                        media_path = NULL,
                        media_provider = NULL,
                        media_lease_expires_at = NULL
                    WHERE id = ? AND media_status IN ('none', 'failed', 'cancelled')
                    """,
                    (new_token, now_iso, draft_id)
                )
                if cursor.rowcount == 0:
                    success = False

            if success:
                conn.commit()
            else:
                conn.rollback()
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
                    "UPDATE drafts SET media_status = 'cancelled', media_generation_token = NULL "
                    "WHERE id = ? AND media_status IN ('pending', 'generating')",
                    (draft_id,)
                )

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
                raise AmbiguousPublishStateError(f"Failed to record publish success for draft {draft_id}") from e

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
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE drafts SET rewritten_text = ?, status = 'review' WHERE id = ? AND status IN ('review', 'pending')",
                (new_text, draft_id)
            )
            conn.commit()

    # --- Phase 4: Media State Machine ---

    def recover_expired_media_jobs(self, max_attempts: int = 3) -> int:
        import uuid
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            cursor.execute("BEGIN IMMEDIATE")

            cursor.execute("SELECT id, media_attempt_count, media_path FROM drafts WHERE media_status = 'generating' AND media_lease_expires_at < ?", (now_iso,))
            expired_rows = cursor.fetchall()
            if not expired_rows:
                return 0

            for row in expired_rows:
                draft_id = row["id"]
                attempts = row["media_attempt_count"]
                old_media_path = row["media_path"]
                new_token = str(uuid.uuid4())

                if attempts >= max_attempts:
                    new_status = 'ready' if old_media_path else 'failed'
                    cursor.execute(
                        """
                        UPDATE drafts
                        SET media_status = ?,
                            media_error_code = 'timeout',
                            media_error_message = 'Max attempts reached due to lease expiration',
                            media_started_at = NULL,
                            media_lease_expires_at = NULL,
                            media_generation_token = ?
                        WHERE id = ?
                        """,
                        (new_status, new_token, draft_id)
                    )
                else:
                    delay = 30 * (2 ** (attempts - 1)) if attempts > 0 else 0
                    next_attempt = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delay)).isoformat()
                    cursor.execute(
                        """
                        UPDATE drafts
                        SET media_status = 'pending',
                            media_generation_token = ?,
                            media_started_at = NULL,
                            media_lease_expires_at = NULL,
                            media_next_attempt_at = ?,
                            media_requested_at = ?
                        WHERE id = ?
                        """,
                        (new_token, next_attempt, now_iso, draft_id)
                    )
            conn.commit()
            return len(expired_rows)

    def queue_media_generation(self, draft_id: int, prompt: str = None, action: str = "generate") -> str:
        import uuid
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            cursor.execute("SELECT media_status, image_prompt FROM drafts WHERE id = ?", (draft_id,))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return "NOT_FOUND"

            status = row['media_status']

            # Action guards
            if action == "generate" and status not in ('none', 'cancelled'):
                conn.rollback()
                return "CONFLICT"
            if action == "retry" and status != 'failed':
                conn.rollback()
                return "CONFLICT"
            if action == "regenerate" and status != 'ready':
                conn.rollback()
                return "CONFLICT"

            # Prompt invariants
            final_prompt = prompt if prompt is not None else row['image_prompt']
            if not final_prompt or not final_prompt.strip():
                conn.rollback()
                return "INVALID_PROMPT"
            final_prompt = final_prompt.strip()
            if len(final_prompt) < 20 or len(final_prompt) > 1500:
                conn.rollback()
                return "INVALID_PROMPT"

            new_token = str(uuid.uuid4())
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

            cursor.execute(
                """
                UPDATE drafts
                SET media_status = 'pending',
                    image_prompt = ?,
                    media_generation_token = ?,
                    media_requested_at = ?,
                    media_attempt_count = 0,
                    media_next_attempt_at = NULL,
                    media_error_code = NULL,
                    media_error_message = NULL,
                    media_lease_expires_at = NULL
                WHERE id = ?
                """,
                (final_prompt, new_token, now_iso, draft_id)
            )

            success = cursor.rowcount == 1
            if success:
                conn.commit()
                return "SUCCESS"
            else:
                conn.rollback()
                return "CONFLICT"

    def cancel_media_generation(self, draft_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                """
                UPDATE drafts
                SET media_status = 'cancelled',
                    media_generation_token = NULL
                WHERE id = ? AND media_status IN ('pending', 'generating')
                """,
                (draft_id,)
            )
            success = cursor.rowcount == 1
            if success:
                conn.commit()
            else:
                conn.rollback()
            return success

    def delete_media(self, draft_id: int) -> str:
        """Atomic invalidation first, then cleanup old file."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            cursor.execute("SELECT media_path, media_status FROM drafts WHERE id = ?", (draft_id,))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return "NOT_FOUND"

            if row["media_status"] in ('generating', 'pending'):
                conn.rollback()
                return "CONFLICT"

            old_path = row["media_path"]

            cursor.execute(
                """
                UPDATE drafts
                SET media_status = 'none',
                    media_path = NULL,
                    media_provider = NULL,
                    media_mime_type = NULL,
                    media_size_bytes = NULL,
                    media_width = NULL,
                    media_height = NULL,
                    media_error_code = NULL,
                    media_error_message = NULL,
                    media_generation_token = NULL,
                    media_lease_expires_at = NULL,
                    media_next_attempt_at = NULL,
                    media_attempt_count = 0
                WHERE id = ?
                """,
                (draft_id,)
            )

            success = cursor.rowcount == 1
            if success:
                conn.commit()
                if old_path:
                    try:
                        from media_builder import media_builder
                        media_builder.delete_media_file(old_path)
                    except Exception as e:
                        from utils import classify_safe_error
                        safe_code = classify_safe_error(e)
                        logger.error(f"Failed to delete old media file for draft {draft_id}: {safe_code}")
                return "SUCCESS"
            else:
                conn.rollback()
                return "CONFLICT"

    def claim_next_pending_media(self, timeout_seconds: int = 120) -> Optional[Dict[str, Any]]:
        import uuid
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

            cursor.execute(
                """
                SELECT id FROM drafts
                WHERE media_status = 'pending'
                  AND (media_next_attempt_at IS NULL OR media_next_attempt_at <= ?)
                ORDER BY media_requested_at ASC, id ASC
                LIMIT 1
                """,
                (now_iso,)
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return None

            draft_id = row["id"]
            new_token = str(uuid.uuid4())
            expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=timeout_seconds)).isoformat()

            cursor.execute(
                """
                UPDATE drafts
                SET media_status = 'generating',
                    media_generation_token = ?,
                    media_started_at = ?,
                    media_lease_expires_at = ?,
                    media_attempt_count = media_attempt_count + 1
                WHERE id = ? AND media_status = 'pending'
                """,
                (new_token, now_iso, expires_at, draft_id)
            )

            if cursor.rowcount != 1:
                conn.rollback()
                return None

            cursor.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,))
            claimed_row = cursor.fetchone()
            conn.commit()
            return dict(claimed_row)

    def complete_media_generation(self, draft_id: int, token: str, meta: dict) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            cursor.execute("SELECT media_path FROM drafts WHERE id = ? AND media_status = 'generating' AND media_generation_token = ?", (draft_id, token))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return False

            old_media_path = row['media_path']
            new_media_path = meta.get("media_path")

            cursor.execute(
                """
                UPDATE drafts
                SET media_status = 'ready',
                    media_generation_token = NULL,
                    media_path = ?,
                    media_provider = ?,
                    media_mime_type = ?,
                    media_size_bytes = ?,
                    media_width = ?,
                    media_height = ?,
                    media_created_at = ?
                WHERE id = ? AND media_status = 'generating' AND media_generation_token = ?
                """,
                (
                    new_media_path, meta.get("media_provider"),
                    meta.get("mime_type"), meta.get("size_bytes"),
                    meta.get("width"), meta.get("height"),
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    draft_id, token
                )
            )
            success = cursor.rowcount == 1
            if success:
                conn.commit()
                # If we successfully regenerated and there was an old file, delete it
                if old_media_path and old_media_path != new_media_path:
                    from media_builder import media_builder
                    media_builder.delete_media_file(old_media_path)
            else:
                conn.rollback()
            return success

    def fail_media_generation(self, draft_id: int, token: str, error_code: str, error_message: str, is_transient: bool, max_attempts: int = 3) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            cursor.execute(
                "SELECT media_attempt_count, media_path FROM drafts WHERE id = ? AND media_status = 'generating' AND media_generation_token = ?",
                (draft_id, token)
            )
            row = cursor.fetchone()

            if not row:
                conn.rollback()
                return False

            attempt_count = row['media_attempt_count']
            old_media_path = row['media_path']

            if is_transient and attempt_count < max_attempts:
                import uuid
                # Exponential backoff: 30s, 60s, 120s
                delay = 30 * (2 ** (attempt_count - 1))
                next_attempt = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delay)).isoformat()
                new_token = str(uuid.uuid4())
                cursor.execute(
                    """
                    UPDATE drafts
                    SET media_status = 'pending',
                        media_generation_token = ?,
                        media_next_attempt_at = ?,
                        media_error_code = ?,
                        media_error_message = ?
                    WHERE id = ? AND media_generation_token = ?
                    """,
                    (new_token, next_attempt, error_code, error_message, draft_id, token)
                )
            else:
                new_status = 'ready' if old_media_path else 'failed'
                cursor.execute(
                    """
                    UPDATE drafts
                    SET media_status = ?,
                        media_generation_token = NULL,
                        media_error_code = ?,
                        media_error_message = ?
                    WHERE id = ? AND media_generation_token = ?
                    """,
                    (new_status, error_code, error_message, draft_id, token)
                )

            success = cursor.rowcount == 1
            if success:
                conn.commit()
            else:
                conn.rollback()
            return success

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

    # --- Phase 5: Source CRUD та створення чернеток з джерел ---

    def create_draft_from_active_source(
        self,
        source_type: str,
        external_id: str,
        source_item_id: str,
        original_text: str,
    ) -> str:
        """Створює чернетку прив'язану до активного джерела.
        Повертає 'created', 'duplicate' або 'rejected'."""
        # Валідація source_item_id
        item_id = str(source_item_id).strip()
        if not item_id or len(item_id) > 200:
            raise ValueError(f"source_item_id must be 1-200 chars, got {len(item_id)}")

        # Нормалізація external_id для telegram
        if source_type == 'telegram':
            try:
                external_id = normalize_telegram_id(external_id)
            except ValueError:
                return "rejected"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                conn.execute('BEGIN IMMEDIATE')

                cursor.execute(
                    "SELECT id, name, priority, trust_rating, processing_mode, is_active, resolution_status "
                    "FROM sources WHERE source_type = ? AND external_id = ?",
                    (source_type, external_id)
                )
                src = cursor.fetchone()
                if not src or src['is_active'] == 0 or src['resolution_status'] == 'unresolved':
                    conn.rollback()
                    return "rejected"

                cursor.execute(
                    "INSERT INTO drafts (original_text, status, source_id, source_name_snapshot, "
                    "source_priority_snapshot, source_trust_snapshot, source_processing_mode_snapshot, source_item_id) "
                    "VALUES (?, 'new', ?, ?, ?, ?, ?, ?)",
                    (
                        original_text,
                        src['id'],
                        src['name'],
                        src['priority'],
                        src['trust_rating'],
                        src['processing_mode'],
                        item_id,
                    )
                )
                conn.commit()
                return "created"
            except sqlite3.IntegrityError:
                conn.rollback()
                return "duplicate"
            except Exception:
                conn.rollback()
                raise

    def get_sources(self, is_active=None) -> List[Dict[str, Any]]:
        """Отримати список джерел, опціонально фільтрованих за is_active."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = """
                SELECT s.*,
                       p.collector_status, p.last_error_code, p.consecutive_errors,
                       p.last_attempt_at, p.last_success_at, p.next_poll_at
                FROM sources s
                LEFT JOIN source_poll_state p ON s.id = p.source_id
            """
            if is_active is not None:
                query += " WHERE s.is_active = ? ORDER BY s.id"
                cursor.execute(query, (int(is_active),))
            else:
                query += " ORDER BY s.id"
                cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def get_source(self, source_id: int) -> Optional[Dict[str, Any]]:
        """Отримати джерело за ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def add_source(
        self,
        source_type: str,
        external_id: str,
        name: str,
        canonical_url: str = None,
        priority: int = 50,
        trust_rating: int = 50,
        processing_mode: str = 'auto',
    ) -> Dict[str, Any]:
        """Додати нове джерело. Повертає створене джерело як dict.
        Піднімає ValueError при невалідних даних, sqlite3.IntegrityError при дублікаті."""
        # Валідація імені
        name = str(name).strip()
        if not name:
            raise ValueError("name must not be empty")

        # Валідація source_type
        if source_type not in ('telegram', 'rss', 'website', 'x'):
            raise ValueError(f"Invalid source_type: {source_type!r}")

        # Нормалізація external_id для telegram
        resolution_status = 'resolved'
        if source_type == 'telegram':
            external_id = normalize_telegram_id(external_id)

        # Валідація canonical_url
        if canonical_url is not None:
            canonical_url = _validate_canonical_url(canonical_url, source_type)

        # Валідація числових діапазонів
        if not (0 <= priority <= 100):
            raise ValueError(f"priority must be 0-100, got {priority}")
        if not (0 <= trust_rating <= 100):
            raise ValueError(f"trust_rating must be 0-100, got {trust_rating}")
        if processing_mode not in ('auto', 'review'):
            raise ValueError(f"Invalid processing_mode: {processing_mode!r}")

        is_active = 1 if resolution_status == 'resolved' else 0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO sources (source_type, external_id, canonical_url, name, priority, "
                "trust_rating, processing_mode, resolution_status, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_type, external_id, canonical_url, name, priority,
                 trust_rating, processing_mode, resolution_status, is_active)
            )
            source_id = cursor.lastrowid
            if source_type in ('rss', 'website'):
                cursor.execute("INSERT OR IGNORE INTO source_poll_state (source_id) VALUES (?)", (source_id,))
            conn.commit()
            cursor.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
            return dict(cursor.fetchone())

    def update_source(self, source_id: int, updates: dict) -> Optional[Dict[str, Any]]:
        """Часткове оновлення джерела. Повертає оновлене джерело або None якщо не знайдено.
        Піднімає ValueError при невалідних даних."""
        if not updates:
            return self.get_source(source_id)

        allowed_fields = {
            'name', 'canonical_url', 'priority', 'trust_rating',
            'processing_mode', 'is_active', 'external_id', 'source_type',
        }
        for key in updates:
            if key not in allowed_fields:
                raise ValueError(f"Cannot update field: {key!r}")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')

            # Отримуємо поточний стан
            cursor.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
            current = cursor.fetchone()
            if not current:
                conn.rollback()
                return None

            current_dict = dict(current)

            # Валідація: не можна активувати unresolved джерело
            new_active = updates.get('is_active', current_dict['is_active'])
            new_resolution = current_dict['resolution_status']
            if new_active and new_resolution == 'unresolved':
                conn.rollback()
                raise ValueError("Cannot activate source with resolution_status='unresolved'")

            # Валідація числових діапазонів
            if 'priority' in updates:
                p = updates['priority']
                if not (0 <= p <= 100):
                    conn.rollback()
                    raise ValueError(f"priority must be 0-100, got {p}")
            if 'trust_rating' in updates:
                t = updates['trust_rating']
                if not (0 <= t <= 100):
                    conn.rollback()
                    raise ValueError(f"trust_rating must be 0-100, got {t}")
            if 'processing_mode' in updates and updates['processing_mode'] not in ('auto', 'review'):
                conn.rollback()
                raise ValueError(f"Invalid processing_mode: {updates['processing_mode']!r}")
            if 'canonical_url' in updates and updates['canonical_url'] is not None:
                source_type = updates.get('source_type', current_dict['source_type'])
                updates['canonical_url'] = _validate_canonical_url(updates['canonical_url'], source_type)
            if 'name' in updates:
                updates['name'] = str(updates['name']).strip()
                if not updates['name']:
                    conn.rollback()
                    raise ValueError("name must not be empty")

            set_clauses = []
            params = []
            for key, value in updates.items():
                set_clauses.append(f"{key} = ?")
                params.append(value)
            set_clauses.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
            params.append(source_id)

            query = f"UPDATE sources SET {', '.join(set_clauses)} WHERE id = ?"
            cursor.execute(query, params)
            conn.commit()

            cursor.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def deactivate_source(self, source_id: int) -> Optional[Dict[str, Any]]:
        """Деактивація джерела (ідемпотентна). Повертає джерело або None."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE sources SET is_active = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (source_id,)
            )
            conn.commit()
            if cursor.rowcount == 0:
                return None
            cursor.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def resolve_source(self, source_id: int, new_external_id: str) -> Optional[Dict[str, Any]]:
        """Resolve unresolved telegram джерело з новим числовим external_id.
        Піднімає ValueError при невалідних даних, sqlite3.IntegrityError при дублікаті."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')

            cursor.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
            current = cursor.fetchone()
            if not current:
                conn.rollback()
                return None

            current_dict = dict(current)
            if current_dict['source_type'] != 'telegram':
                conn.rollback()
                raise ValueError("resolve_source is only for telegram sources")
            if current_dict['resolution_status'] != 'unresolved':
                conn.rollback()
                raise ValueError("Source is already resolved")

            # Нормалізуємо новий external_id
            normalized = normalize_telegram_id(new_external_id)

            # Перевірка на дублікат
            cursor.execute(
                "SELECT id FROM sources WHERE source_type = 'telegram' AND external_id = ? AND id != ?",
                (normalized, source_id)
            )
            if cursor.fetchone():
                conn.rollback()
                raise sqlite3.IntegrityError(f"Source with external_id={normalized} already exists")

            cursor.execute(
                "UPDATE sources SET external_id = ?, resolution_status = 'resolved', is_active = 1, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (normalized, source_id)
            )
            conn.commit()

            cursor.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
            row = cursor.fetchone()
            return dict(row) if row else None


    # --- Phase 6: Global Worker Lease and Polling ---

    def acquire_global_lease(self, worker_id: str, duration_sec: int) -> Optional[str]:
        import uuid
        import datetime
        token = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = (now + datetime.timedelta(seconds=duration_sec)).isoformat()
        now_str = now.isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')

            cursor.execute("SELECT lease_token, expires_at FROM worker_leases WHERE id = 1")
            row = cursor.fetchone()
            if row:
                if row['expires_at'] > now_str:
                    conn.rollback()
                    return None
                cursor.execute(
                    "UPDATE worker_leases SET worker_id = ?, lease_token = ?, heartbeat_at = ?, expires_at = ? WHERE id = 1",
                    (worker_id, token, now_str, expires)
                )
            else:
                cursor.execute(
                    "INSERT INTO worker_leases (id, worker_id, lease_token, heartbeat_at, expires_at) VALUES (1, ?, ?, ?, ?)",
                    (worker_id, token, now_str, expires)
                )
            conn.commit()
            return token

    def heartbeat_global_lease(self, token: str, duration_sec: int) -> bool:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = (now + datetime.timedelta(seconds=duration_sec)).isoformat()
        now_str = now.isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE worker_leases SET heartbeat_at = ?, expires_at = ? WHERE id = 1 AND lease_token = ?",
                (now_str, expires, token)
            )
            conn.commit()
            return cursor.rowcount > 0

    def release_global_lease(self, token: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM worker_leases WHERE id = 1 AND lease_token = ?", (token,))
            conn.commit()

    def claim_due_poll_source(self, global_token: str, source_lease_sec: int) -> Optional[Dict[str, Any]]:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        now_str = now.isoformat()
        expires = (now + datetime.timedelta(seconds=source_lease_sec)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')

            # 1. Verify global lease
            cursor.execute("SELECT expires_at FROM worker_leases WHERE id = 1 AND lease_token = ?", (global_token,))
            gw = cursor.fetchone()
            if not gw or gw['expires_at'] <= now_str:
                conn.rollback()
                return None

            # 2. Find eligible source
            cursor.execute("""
                SELECT p.source_id, p.next_poll_at, p.collector_status, p.resolved_feed_url, s.source_type, s.is_active, s.canonical_url
                FROM source_poll_state p
                JOIN sources s ON p.source_id = s.id
                WHERE s.is_active = 1
                  AND s.source_type IN ('rss', 'website')
                  AND p.collector_status != 'unsupported'
                  AND p.next_poll_at <= ?
                  AND (p.lease_expires_at IS NULL OR p.lease_expires_at <= ?)
                ORDER BY p.next_poll_at ASC
                LIMIT 1
            """, (now_str, now_str))

            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return None

            source_id = row['source_id']
            source_type = row['source_type']
            canonical_url = row['canonical_url']
            resolved_feed_url = row['resolved_feed_url']

            if source_type == 'rss':
                claimed_mode = 'rss'
                claimed_target = canonical_url
            else:
                if resolved_feed_url is None:
                    claimed_mode = 'website discovery'
                    claimed_target = canonical_url
                else:
                    claimed_mode = 'website feed'
                    claimed_target = resolved_feed_url

            # 3. Claim it
            import uuid
            source_token = str(uuid.uuid4())
            cursor.execute("""
                UPDATE source_poll_state
                SET lease_token = ?, lease_expires_at = ?, collector_status = CASE WHEN collector_status = 'idle' THEN 'polling' ELSE collector_status END
                WHERE source_id = ?
            """, (source_token, expires, source_id))

            # Fetch complete row
            cursor.execute("""
                SELECT p.*, s.source_type, s.external_id, s.name, s.priority, s.trust_rating, s.processing_mode, s.canonical_url
                FROM source_poll_state p
                JOIN sources s ON p.source_id = s.id
                WHERE p.source_id = ?
            """, (source_id,))
            full_row = dict(cursor.fetchone())
            full_row['claimed_mode'] = claimed_mode
            full_row['claimed_target'] = claimed_target

            conn.commit()
            return full_row

    def complete_source_poll(self, global_token: str, source_id: int, source_token: str, claimed_mode: str, claimed_target: str, outcome_updates: dict, drafts_to_insert: List[dict] = None) -> bool:
        import datetime
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        allowed_fields = {
            'collector_status', 'last_error_code', 'consecutive_errors',
            'last_attempt_at', 'last_success_at', 'next_poll_at',
            'resolved_feed_url', 'validator_url', 'etag', 'last_modified',
            'initial_sync_completed_at', 'last_http_status'
        }
        for k in outcome_updates.keys():
            if k not in allowed_fields:
                raise ValueError(f"Disallowed outcome field: {k}")

        if 'last_http_status' in outcome_updates:
            st = outcome_updates['last_http_status']
            if st is not None and not (isinstance(st, int) and 100 <= st <= 599):
                raise ValueError("last_http_status must be an integer between 100 and 599, or None")

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Check draft limit BEFORE BEGIN IMMEDIATE
            if drafts_to_insert:
                cursor.execute("SELECT initial_sync_completed_at FROM source_poll_state WHERE source_id = ?", (source_id,))
                st = cursor.fetchone()
                if st:
                    limit = 10 if st['initial_sync_completed_at'] is None else 50
                    if len(drafts_to_insert) > limit:
                        raise ValueError(f"drafts_to_insert exceeds limit of {limit}")

            conn.execute('BEGIN IMMEDIATE')

            cursor.execute("""
                SELECT
                    w.expires_at as gw_expires,
                    p.lease_token as src_token,
                    p.lease_expires_at as src_expires,
                    p.resolved_feed_url,
                    s.is_active,
                    s.source_type,
                    s.canonical_url,
                    s.resolution_status
                FROM worker_leases w
                LEFT JOIN source_poll_state p ON p.source_id = ?
                LEFT JOIN sources s ON s.id = p.source_id
                WHERE w.id = 1 AND w.lease_token = ?
            """, (source_id, global_token))
            row = cursor.fetchone()

            if not row:
                conn.rollback()
                return False # Global lease not found or mismatched

            if row['gw_expires'] <= now_str:
                conn.rollback()
                return False # Global lease expired

            if not row['src_token']:
                conn.rollback()
                return False # Source not found

            if row['src_token'] != source_token or row['src_expires'] <= now_str:
                conn.rollback()
                return False # Stale worker or expired source lease

            if row['is_active'] == 0 or row['source_type'] not in ('rss', 'website'):
                conn.rollback()
                return False

            if row['source_type'] == 'rss':
                current_mode = 'rss'
                current_target = row['canonical_url']
            else:
                if row['resolved_feed_url'] is None:
                    current_mode = 'website discovery'
                    current_target = row['canonical_url']
                else:
                    current_mode = 'website feed'
                    current_target = row['resolved_feed_url']

            if current_mode != claimed_mode or current_target != claimed_target:
                conn.rollback()
                return False

            # V-18: Clear validators if discovery target changes
            if 'resolved_feed_url' in outcome_updates:
                if outcome_updates['resolved_feed_url'] != row['resolved_feed_url']:
                    outcome_updates['etag'] = None
                    outcome_updates['last_modified'] = None
                    outcome_updates['validator_url'] = None

            set_clauses = ["lease_token = NULL", "lease_expires_at = NULL", "updated_at = CURRENT_TIMESTAMP"]
            params = []
            for k, v in outcome_updates.items():
                set_clauses.append(f"{k} = ?")
                params.append(v)

            set_clauses_str = ", ".join(set_clauses)
            params.append(source_id)
            try:
                cursor.execute(f"UPDATE source_poll_state SET {set_clauses_str} WHERE source_id = ?", params)

                if drafts_to_insert:
                    cursor.execute(
                        "SELECT name, priority, trust_rating, processing_mode FROM sources WHERE id = ?",
                        (source_id,)
                    )
                    src_meta = cursor.fetchone()

                    for draft in drafts_to_insert:
                        cursor.execute("""
                            INSERT INTO drafts (
                                original_text, status, source_id, source_name_snapshot,
                                source_priority_snapshot, source_trust_snapshot,
                                source_processing_mode_snapshot, source_item_id,
                                source_published_at, source_updated_at, created_at, updated_at
                            ) VALUES (?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(source_id, source_item_id) DO NOTHING
                        """, (
                            draft['original_text'], source_id, src_meta['name'],
                            src_meta['priority'], src_meta['trust_rating'], src_meta['processing_mode'],
                            draft['source_item_id'], draft.get('source_published_at'), draft.get('source_updated_at'),
                            now_str, now_str
                        ))

                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
                raise

    def get_poll_state(self, source_id: int) -> Optional[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM source_poll_state WHERE source_id = ?", (source_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def poll_now(self, source_id: int) -> str:
        """API method to enqueue immediate poll. Returns 'queued', 'missing', or 'conflict'"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')

            cursor.execute("""
                SELECT p.collector_status, p.lease_token, s.is_active, s.source_type
                FROM source_poll_state p
                JOIN sources s ON p.source_id = s.id
                WHERE p.source_id = ?
            """, (source_id,))
            row = cursor.fetchone()

            if not row:
                conn.rollback()
                return "missing"

            if row['is_active'] == 0 or row['source_type'] not in ('rss', 'website'):
                conn.rollback()
                return "conflict"

            if row['lease_token'] is not None:
                conn.rollback()
                return "conflict" # Already polling

            # Allow unsupported website to be retried
            cursor.execute("""
                UPDATE source_poll_state
                SET next_poll_at = CURRENT_TIMESTAMP, collector_status = 'queued', updated_at = CURRENT_TIMESTAMP
                WHERE source_id = ?
            """, (source_id,))
            conn.commit()
            return "queued"

db = Database()
