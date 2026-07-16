import litellm
from litellm import completion, embedding
from loguru import logger
from config import settings
from tenacity import retry, wait_exponential, stop_after_attempt
from typing import List, Optional, Tuple
from dataclasses import dataclass
import requests
from runtime_types import LLMConfig, GeminiConfig

GEMINI_EMBEDDING_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent"
)


class LLMProviderError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _legacy_llm_config() -> LLMConfig:
    return LLMConfig(
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        openai_api_key=settings.openai_api_key,
        gemini=GeminiConfig(
            api_key=settings.gemini_api_key,
        ),
    )


def _provider_for_model(model: str) -> str | None:
    prefix, separator, _ = model.partition("/")
    if not separator:
        return None
    return {
        "gemini": "gemini",
        "openai": "openai",
    }.get(prefix.casefold())

@dataclass(frozen=True)
class LLMResult:
    text: str
    model_used: str | None

class LLMProvider:
    def __init__(self, config: LLMConfig | None = None) -> None:
        if config is None:
            config = _legacy_llm_config()
        self._config = config
        self.model = self._config.model
        # LiteLLM automatically picks up OPENAI_API_KEY from environment variables
        # If no key is set and we try to call it, litellm will throw an error.
        
    def _models_to_try(self) -> tuple[str, ...]:
        primary = self.model
        if _provider_for_model(primary) != "gemini":
            return (primary,)
        fallbacks = (
            "gemini/gemini-3.5-flash",
            "gemini/gemini-3.1-flash-lite",
            "gemini/gemini-2.5-flash",
        )
        return tuple(dict.fromkeys((primary, *fallbacks)))

    def _api_key_for_model(self, model: str) -> str | None:
        provider = _provider_for_model(model)
        if provider == "gemini":
            return self._config.gemini.api_key
        if provider == "openai":
            return self._config.openai_api_key
        return None

    def _completion_kwargs(
        self,
        *,
        model: str,
        temperature: float | None,
    ) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "model": model,
            "temperature": temperature if temperature is not None else self._config.temperature
        }
        api_key = self._api_key_for_model(model)
        if api_key is not None:
            kwargs["api_key"] = api_key
        return kwargs

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def generate_with_metadata(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: Optional[float] = None,
    ) -> LLMResult:
        """
        Відправляє запит до LLM через LiteLLM та повертає результат з метаданими.
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
            
        messages.append({"role": "user", "content": prompt})
        
        logger.debug(f"Calling LLM ({self.model}) with prompt length: {len(prompt)}")
        
        try:
            for current_model in self._models_to_try():
                try:
                    kwargs = self._completion_kwargs(model=current_model, temperature=temperature)
                    response = completion(messages=messages, **kwargs)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    logger.warning(f"Model {current_model} failed. Trying next model...")
                    continue
                    
                result = response.choices[0].message.content

                # Логування використання токенів (Cost Management)
                usage = response.usage
                logger.info(f"LLM Success with {current_model}. Tokens used: {usage.total_tokens} (Prompt: {usage.prompt_tokens}, Completion: {usage.completion_tokens})")

                return LLMResult(text=result.strip(), model_used=current_model)

            logger.error("All models failed.")
            raise LLMProviderError("completion_failed") from None

        except (KeyboardInterrupt, SystemExit):
            raise
        except LLMProviderError:
            raise
        except Exception:
            logger.error("Error during LLM generation setup")
            raise LLMProviderError("setup_failed") from None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: Optional[float] = None,
    ) -> str:
        """
        Backward compatible generate that returns only the text.
        """
        return self.generate_with_metadata(prompt, system_prompt, temperature).text

    def _gemini_embedding(self, text: str) -> List[float]:
        api_key = self._config.gemini.api_key
        if not api_key:
            raise LLMProviderError("gemini_api_key_missing")
        try:
            res = requests.post(
                GEMINI_EMBEDDING_ENDPOINT,
                params={"key": api_key},
                json={
                    "model": "models/gemini-embedding-2",
                    "content": {"parts": [{"text": text}]}
                },
            )
        except requests.RequestException:
            raise LLMProviderError("gemini_embedding_transport_failed") from None

        if res.status_code != 200:
            raise LLMProviderError("gemini_embedding_failed") from None

        try:
            values = res.json()['embedding']['values']

            is_valid_list = isinstance(values, list)
            is_numbers = all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in values
            )
            if not is_valid_list or not is_numbers:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise LLMProviderError("gemini_embedding_invalid_response") from None

        return [float(v) for v in values]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get_embedding(self, text: str) -> List[float]:
        """Отримує векторне представлення тексту."""
        try:
            provider = _provider_for_model(self.model)
            if provider == "gemini" and self._config.gemini.api_key:
                return self._gemini_embedding(text)
            else:
                api_key = self._config.openai_api_key
                if not api_key:
                     raise LLMProviderError("openai_api_key_missing")
                try:
                    kwargs = {
                        "model": "text-embedding-3-small",
                        "input": [text],
                        "api_key": api_key,
                    }
                    response = embedding(**kwargs)
                    return response.data[0]['embedding']
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    raise LLMProviderError("openai_embedding_failed") from None
        except (KeyboardInterrupt, SystemExit, LLMProviderError):
            raise
        except Exception:
            logger.error("Error getting embedding")
            raise LLMProviderError("embedding_failed_unexpected") from None

llm = LLMProvider()

if __name__ == "__main__":
    # Test script
    logger.info("Testing LLM Provider...")
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY is not set in .env. Skipping real test.")
    else:
        res = llm.generate("Say hello world simply.", "You are a helpful assistant.")
        logger.success(f"Response: {res}")
