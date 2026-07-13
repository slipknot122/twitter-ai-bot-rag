import json
import numpy as np
from typing import List, Dict, Any
from loguru import logger
from database import db
from llm_provider import llm

class SemanticMemory:
    """
    Абстракція над векторною базою даних.
    Поки що під капотом SQLite + Numpy (Cosine Similarity).
    Якщо знадобиться масштабування, тут можна буде підключити ChromaDB або FAISS, 
    не змінюючи AI Engine.
    """

    def __init__(self):
        # Налаштування витягуються з бази (або дефолтні)
        self.top_k = db.get_setting("semantic_top_k", 2)
        self.min_similarity = db.get_setting("minimum_similarity", 0.82)

    def _cosine_similarity(self, vec_a: List[float], vec_b: List[float]) -> float:
        a = np.array(vec_a)
        b = np.array(vec_b)
        if np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0:
            return 0.0
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    def save(self, text: str):
        """Зберігає текст у векторну пам'ять."""
        try:
            embedding = llm.get_embedding(text)
            embedding_json = json.dumps(embedding)
            
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO semantic_memory (text, embedding_json) VALUES (?, ?)",
                    (text, embedding_json)
                )
                conn.commit()
            logger.debug(f"Saved to Semantic Memory: {text[:50]}...")
        except Exception as e:
            from utils import classify_safe_error
            safe_code = classify_safe_error(e)
            logger.error(f"Failed to save to Semantic Memory: {safe_code}")

    def search(self, query_text: str) -> List[Dict[str, Any]]:
        """Шукає найближчі по смислу тексти в пам'яті."""
        try:
            # Динамічно оновлюємо налаштування на випадок, якщо їх змінили в UI
            self.top_k = int(db.get_setting("semantic_top_k", 2))
            self.min_similarity = float(db.get_setting("minimum_similarity", 0.82))

            query_embedding = llm.get_embedding(query_text)
            
            results = []
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, text, embedding_json FROM semantic_memory")
                rows = cursor.fetchall()
                
                for row in rows:
                    mem_embedding = json.loads(row['embedding_json'])
                    sim = self._cosine_similarity(query_embedding, mem_embedding)
                    
                    if sim >= self.min_similarity:
                        results.append({
                            "id": row["id"],
                            "text": row["text"],
                            "similarity": sim
                        })
            
            # Сортуємо за спаданням схожості і беремо top_k
            results.sort(key=lambda x: x["similarity"], reverse=True)
            return results[:self.top_k]
            
        except Exception as e:
            from utils import classify_safe_error
            safe_code = classify_safe_error(e)
            logger.error(f"Failed to search Semantic Memory: {safe_code}")
            return []

    def delete(self, memory_id: int):
        """Видаляє запис із пам'яті."""
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM semantic_memory WHERE id = ?", (memory_id,))
            conn.commit()

# Singleton для використання в інших модулях
semantic_memory = SemanticMemory()
