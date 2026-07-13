from litellm import completion, embedding
from loguru import logger
from config import settings
from tenacity import retry, wait_exponential, stop_after_attempt
from typing import List, Optional, Tuple
from dataclasses import dataclass
import os

@dataclass(frozen=True)
class LLMResult:
    text: str
    model_used: str | None

class LLMProvider:
    def __init__(self):
        self.model = settings.llm_model
        # LiteLLM automatically picks up OPENAI_API_KEY from environment variables
        # If no key is set and we try to call it, litellm will throw an error.
        
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def generate_with_metadata(self, prompt: str, system_prompt: str = "", temperature: Optional[float] = None) -> LLMResult:
        """
        Відправляє запит до LLM через LiteLLM та повертає результат з метаданими.
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
            
        messages.append({"role": "user", "content": prompt})
        
        logger.debug(f"Calling LLM ({self.model}) with prompt length: {len(prompt)}")
        
        try:
            # Якщо ключі задані в .env, передаємо їх в os.environ для LiteLLM
            if settings.openai_api_key:
                os.environ["OPENAI_API_KEY"] = settings.openai_api_key
            if settings.gemini_api_key:
                os.environ["GEMINI_API_KEY"] = settings.gemini_api_key

            if "gemini" in self.model:
                models_to_try = [
                    "gemini/gemini-3.5-flash",
                    "gemini/gemini-3.1-flash-lite",
                    "gemini/gemini-2.5-flash"
                ]
            else:
                models_to_try = [self.model]

            last_error = None
            for current_model in models_to_try:
                try:
                    response = completion(
                        model=current_model,
                        messages=messages,
                        temperature=temperature if temperature is not None else settings.llm_temperature
                    )
                    result = response.choices[0].message.content
                    
                    # Логування використання токенів (Cost Management)
                    usage = response.usage
                    logger.info(f"LLM Success with {current_model}. Tokens used: {usage.total_tokens} (Prompt: {usage.prompt_tokens}, Completion: {usage.completion_tokens})")
                    
                    return LLMResult(text=result.strip(), model_used=current_model)
                except Exception as e:
                    logger.warning(f"Model {current_model} failed: {e}. Trying next model...")
                    last_error = e
                    continue
                    
            logger.error(f"All models failed. Last error: {last_error}")
            raise last_error

        except Exception as e:
            logger.error(f"Error during LLM generation setup: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def generate(self, prompt: str, system_prompt: str = "", temperature: Optional[float] = None) -> str:
        """
        Backward compatible generate that returns only the text.
        """
        return self.generate_with_metadata(prompt, system_prompt, temperature).text

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get_embedding(self, text: str) -> List[float]:
        """Отримує векторне представлення тексту."""
        try:
            if "gemini" in self.model and settings.gemini_api_key:
                import requests
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent?key={settings.gemini_api_key}"
                data = {"model": "models/gemini-embedding-2", "content": {"parts": [{"text": text}]}}
                res = requests.post(url, json=data)
                if res.status_code == 200:
                    return res.json()['embedding']['values']
                else:
                    raise Exception(f"Gemini API error: {res.text}")
            else:
                if settings.openai_api_key:
                    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
                response = embedding(
                    model="text-embedding-3-small",
                    input=[text]
                )
                return response.data[0]['embedding']
        except Exception as e:
            logger.error(f"Error getting embedding: {e}")
            raise

llm = LLMProvider()

if __name__ == "__main__":
    # Test script
    logger.info("Testing LLM Provider...")
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY is not set in .env. Skipping real test.")
    else:
        res = llm.generate("Say hello world simply.", "You are a helpful assistant.")
        logger.success(f"Response: {res}")
