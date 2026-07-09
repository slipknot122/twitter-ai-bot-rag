import json
import os
import re
from typing import List, Dict, Any
from loguru import logger
from semantic_memory import semantic_memory

class ContextBuilder:
    """
    RAG-двигун для бота.
    Підтягує інформацію з локальних JSON файлів (Knowledge Base) та з векторної пам'яті (Semantic Memory).
    """
    def __init__(self, knowledge_dir: str = "knowledge"):
        self.knowledge_dir = knowledge_dir
        
    def _load_knowledge_files(self) -> Dict[str, Dict[str, str]]:
        knowledge = {}
        if not os.path.exists(self.knowledge_dir):
            return knowledge
            
        for filename in os.listdir(self.knowledge_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(self.knowledge_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            knowledge[filename] = data
                except Exception as e:
                    logger.warning(f"Failed to load knowledge file {filename}: {e}")
        return knowledge

    def _match_knowledge(self, text: str, knowledge_db: Dict[str, Dict[str, str]]) -> List[str]:
        """
        Знаходить відповідники з бази знань.
        Зараз працює як пошук за ключовими словами (regex / exact match).
        """
        matches = []
        text_lower = text.lower()
        
        for category, items in knowledge_db.items():
            for key, value in items.items():
                # Простий пошук по слову з межами слова (щоб 'sol' не мечилося в 'solution')
                pattern = r'\b' + re.escape(key.lower()) + r'\b'
                if re.search(pattern, text_lower):
                    matches.append(f"[{key}]: {value}")
                    
        return matches

    def build_context(self, original_text: str) -> str:
        """
        Збирає єдиний блок контексту для LLM.
        """
        knowledge_db = self._load_knowledge_files()
        
        # 1. Знання (Knowledge Match)
        knowledge_matches = self._match_knowledge(original_text, knowledge_db)
        
        # 2. Пам'ять (Semantic Memory)
        semantic_matches = semantic_memory.search(original_text)
        
        # Формування підсумкового блоку
        context_parts = []
        
        if knowledge_matches:
            context_parts.append("BACKGROUND KNOWLEDGE:")
            context_parts.extend(knowledge_matches)
            
        if semantic_matches:
            context_parts.append("\nPREVIOUS RELATED TWEETS (Semantic Memory):")
            for i, match in enumerate(semantic_matches, 1):
                context_parts.append(f"{i}. {match['text']} (Similarity: {match['similarity']:.2f})")
                
        context_str = "\n".join(context_parts)
        
        # Логування
        logger.info(f"Context Builder: Knowledge matches: {len(knowledge_matches)}, Semantic matches: {len(semantic_matches)}, Context size: {len(context_str)} chars")
        
        if not context_str:
            return ""
            
        # Додавання Soft Context інструкції
        final_context = (
            "<CONTEXT>\n"
            "Використовуй цей контекст лише якщо він релевантний для кращого розуміння теми або щоб уникнути повторень.\n"
            f"{context_str}\n"
            "</CONTEXT>"
        )
        return final_context

# Singleton
context_builder = ContextBuilder()
