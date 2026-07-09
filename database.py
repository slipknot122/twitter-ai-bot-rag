import sqlite3
from typing import Optional, List, Dict, Any
from loguru import logger
from config import settings

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
        
        return conn

    def _init_db(self):
        """Ініціалізація таблиць."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблиця для оброблених вхідних повідомлень
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS processed_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,          -- Наприклад, message_id з Telegram
                    source_channel TEXT NOT NULL,     -- Назва каналу
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_id, source_channel)
                )
            ''')
            
            # Таблиця для чернеток (drafts)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    processed_message_id INTEGER,
                    original_text TEXT NOT NULL,
                    rewritten_text TEXT,
                    reason TEXT,
                    confidence REAL,
                    status TEXT DEFAULT 'new',    -- 'new', 'processing', 'review', 'approved', 'publishing', 'published', 'failed', 'ignored'
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (processed_message_id) REFERENCES processed_messages (id)
                )
            ''')
            
            # Таблиця для опублікованих твітів
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS published_tweets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER,
                    tweet_id TEXT NOT NULL,
                    published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (draft_id) REFERENCES drafts (id)
                )
            ''')
            # Таблиця для налаштувань, які можна редагувати через UI
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблиця для векторної пам'яті
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS semantic_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Додавання нових стовпців для існуючих БД
            # SQLite не підтримує IF NOT EXISTS для ADD COLUMN, тому робимо через try-except
            new_columns = [
                "scheduled_at DATETIME NULL",
                "retry_count INTEGER DEFAULT 0",
                "last_error TEXT NULL",
                "last_retry_at DATETIME NULL"
            ]
            for col in new_columns:
                try:
                    cursor.execute(f"ALTER TABLE drafts ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass # Колонка вже існує
            
            conn.commit()
            logger.debug(f"Database initialized at {self.db_path}")

            # Реєструємо дефолтні налаштування для Scheduler, якщо їх ще немає
            if self.get_setting("publish_delay_minutes") is None:
                self.set_setting("publish_delay_minutes", 45)
            if self.get_setting("publish_jitter_percent") is None:
                self.set_setting("publish_jitter_percent", 15)
            if self.get_setting("max_retries") is None:
                self.set_setting("max_retries", 3)
            if self.get_setting("scheduler_check_interval_seconds") is None:
                self.set_setting("scheduler_check_interval_seconds", 60)
                
            # Реєструємо дефолтні налаштування для Semantic Memory
            if self.get_setting("semantic_top_k") is None:
                self.set_setting("semantic_top_k", 2)
            if self.get_setting("minimum_similarity") is None:
                self.set_setting("minimum_similarity", 0.82)

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                import json
                try:
                    return json.loads(row['value'])
                except:
                    return row['value']
            return default

    def set_setting(self, key: str, value: Any):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            import json
            str_val = json.dumps(value) if isinstance(value, (dict, list, bool, int, float)) else str(value)
            cursor.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP",
                (key, str_val, str_val)
            )
            conn.commit()

    def is_message_processed(self, source_id: str, source_channel: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM processed_messages WHERE source_id = ? AND source_channel = ?",
                (str(source_id), str(source_channel))
            )
            return cursor.fetchone() is not None

    def mark_message_processed(self, source_id: str, source_channel: str) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO processed_messages (source_id, source_channel) VALUES (?, ?)",
                (str(source_id), str(source_channel))
            )
            conn.commit()
            return cursor.lastrowid

    def create_draft(self, processed_message_id: int, original_text: str, rewritten_text: Optional[str] = None, status: str = 'new', reason: Optional[str] = None, confidence: Optional[float] = None) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO drafts (processed_message_id, original_text, rewritten_text, status, reason, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                (processed_message_id, original_text, rewritten_text, status, reason, confidence)
            )
            conn.commit()
            return cursor.lastrowid

    def update_draft_status(self, draft_id: int, status: str, last_error: Optional[str] = None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Якщо статус rejected/ignored або approved, ми можемо скинути retry_count
            if status in ['approved', 'ignored', 'failed']:
                cursor.execute(
                    "UPDATE drafts SET status = ?, updated_at = CURRENT_TIMESTAMP, last_error = ?, retry_count = 0 WHERE id = ?",
                    (status, last_error, draft_id)
                )
            else:
                cursor.execute(
                    "UPDATE drafts SET status = ?, updated_at = CURRENT_TIMESTAMP, last_error = ? WHERE id = ?",
                    (status, last_error, draft_id)
                )
            conn.commit()

    def increment_retry_count(self, draft_id: int, error_msg: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE drafts SET retry_count = retry_count + 1, last_error = ?, last_retry_at = CURRENT_TIMESTAMP WHERE id = ?",
                (error_msg, draft_id)
            )
            conn.commit()

    def record_published_tweet(self, draft_id: int, tweet_id: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO published_tweets (draft_id, tweet_id) VALUES (?, ?)",
                (draft_id, str(tweet_id))
            )
            # Також оновлюємо статус чернетки
            cursor.execute(
                "UPDATE drafts SET status = 'published', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (draft_id,)
            )
            conn.commit()

    def fetch_next_new_draft(self) -> Optional[Dict[str, Any]]:
        """Атомарно витягує наступне повідомлення для обробки, переводить його в PROCESSING."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Знаходимо найстаріше нове повідомлення
            cursor.execute("SELECT id FROM drafts WHERE status = 'new' ORDER BY created_at ASC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                return None
            draft_id = row['id']
            
            # Намагаємось заблокувати його (захист від паралельних воркерів)
            cursor.execute(
                "UPDATE drafts SET status = 'processing', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'new'", 
                (draft_id,)
            )
            if cursor.rowcount == 0:
                return None # Інший воркер встиг перехопити
                
            cursor.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,))
            draft = cursor.fetchone()
            conn.commit()
            return dict(draft) if draft else None

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
            # Шукаємо останній опублікований або такий що публікується твіт
            cursor.execute("SELECT updated_at FROM drafts WHERE status IN ('published', 'publishing') ORDER BY updated_at DESC LIMIT 1")
            row = cursor.fetchone()
            return row['updated_at'] if row else None

    def fetch_next_approved_draft_for_publish(self) -> Optional[Dict[str, Any]]:
        """Атомарно витягує наступний APPROVED твіт і переводить його в PUBLISHING."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Беремо найстаріший APPROVED, який готовий до публікації
            # Якщо є scheduled_at, можна додавати логіку `AND (scheduled_at IS NULL OR scheduled_at <= CURRENT_TIMESTAMP)`
            cursor.execute("SELECT id FROM drafts WHERE status = 'approved' AND (scheduled_at IS NULL OR scheduled_at <= CURRENT_TIMESTAMP) ORDER BY created_at ASC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                return None
            draft_id = row['id']
            
            # Намагаємось заблокувати його
            cursor.execute(
                "UPDATE drafts SET status = 'publishing', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'approved'", 
                (draft_id,)
            )
            if cursor.rowcount == 0:
                return None # Інший процес встиг перехопити
                
            cursor.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,))
            draft = cursor.fetchone()
            conn.commit()
            return dict(draft) if draft else None

    def get_analytics(self) -> Dict[str, Any]:
        """Повертає базову аналітику для дашборду."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Всього сьогодні
            cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE date(created_at) = date('now')")
            total_today = cursor.fetchone()['c']
            
            # Опубліковано сьогодні
            cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE status IN ('published', 'approved', 'publishing') AND date(updated_at) = date('now')")
            published_today = cursor.fetchone()['c']
            
            # Відхилено сьогодні
            cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE status IN ('ignored', 'failed') AND date(updated_at) = date('now')")
            ignored_today = cursor.fetchone()['c']
            
            # На розгляді (загалом, не тільки сьогодні)
            cursor.execute("SELECT COUNT(*) as c FROM drafts WHERE status IN ('review', 'pending')")
            pending_total = cursor.fetchone()['c']
            
            return {
                "total_today": total_today,
                "published_today": published_today,
                "ignored_today": ignored_today,
                "pending_total": pending_total
            }




# Singleton для імпорту в інших файлах
db = Database()

if __name__ == "__main__":
    logger.info("Testing database connection and initialization...")
    test_db = Database("test_db.sqlite")
    
    # Test processed
    msg_id = test_db.mark_message_processed("123", "test_channel")
    assert test_db.is_message_processed("123", "test_channel") == True
    
    # Test draft
    draft_id = test_db.create_draft(msg_id, "Original", "Rewritten")
    test_db.update_draft_status(draft_id, "approved")
    
    # Test publish
    test_db.record_published_tweet(draft_id, "999999")
    
    logger.success("Database operations verified successfully!")
