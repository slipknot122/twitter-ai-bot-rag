"""
Media Builder — каскадна система генерації зображень.

Архітектура:
- MediaProvider (Protocol) — інтерфейс для будь-якого провайдера
- CloudflareProvider — основний (безкоштовний щоденний ліміт)
- PollinationsProvider — fallback (безкоштовний з rate limits)
- MediaBuilder — каскадний оркестратор з валідацією файлів
"""

import io
import os
import uuid
import base64
import urllib.parse
from pathlib import Path
from typing import Protocol, Optional, runtime_checkable

import requests
from PIL import Image
from loguru import logger
from config import settings
from database import db
from market_data import market_data


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MediaGenerationError(Exception):
    """Базова помилка генерації медіа."""
    pass


class PermanentMediaError(MediaGenerationError):
    """Постійна помилка (не робити fallback): 401, 403, 400, moderation."""
    pass


class TransientMediaError(MediaGenerationError):
    """Тимчасова помилка (fallback дозволено): timeout, 429, 5xx, мережа."""
    pass


# ---------------------------------------------------------------------------
# Provider Interface (Protocol)
# ---------------------------------------------------------------------------

@runtime_checkable
class MediaProvider(Protocol):
    """Абстрактний інтерфейс провайдера зображень."""
    name: str

    def generate(self, prompt: str, output_path: Path,
                 width: int, height: int, timeout: int) -> Path:
        """
        Генерує зображення за промптом і зберігає на диск.
        Повертає Path до збереженого файлу.
        Кидає PermanentMediaError або TransientMediaError.
        """
        ...


# ---------------------------------------------------------------------------
# Google Imagen Provider
# ---------------------------------------------------------------------------

class GoogleImagenProvider:
    """
    Google Imagen (через Gemini API).
    Використовує ключ GEMINI_API_KEY з конфігу.
    """
    name = "google"

    def __init__(self):
        self.api_key = settings.gemini_api_key
        # Використовуємо актуальну Imagen 4 (якщо є доступ)
        self.model = "imagen-4.0-generate-001"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def generate(self, prompt: str, output_path: Path,
                 width: int, height: int, timeout: int) -> Path:
        if not self.is_configured():
            raise PermanentMediaError("Google Gemini API key not configured")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:predict?key={self.api_key}"
        
        # Визначаємо aspect ratio
        aspect_ratio = "1:1"
        if width > height:
            aspect_ratio = "16:9" if width / height >= 1.5 else "4:3"
        elif height > width:
            aspect_ratio = "9:16" if height / width >= 1.5 else "3:4"

        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "sampleCount": 1,
                "aspectRatio": aspect_ratio,
                "outputOptions": {"mimeType": "image/jpeg"}
            }
        }

        try:
            resp = requests.post(url, json=payload, timeout=timeout)
        except requests.Timeout:
            raise TransientMediaError("Google Imagen: timeout")
        except requests.ConnectionError as e:
            raise TransientMediaError(f"Google Imagen: connection error: {e}")

        if resp.status_code == 400:
            err = resp.json().get("error", {}).get("message", "")
            if "paid plans" in err.lower() or "not supported" in err.lower():
                # Якщо акаунт безкоштовний, падаємо транзитно, щоб спрацював Pollinations
                raise TransientMediaError(f"Google Imagen: Free tier limit or not available ({err})")
            raise PermanentMediaError(f"Google Imagen: Bad Request (400) - {err}")
            
        if resp.status_code == 429:
            raise TransientMediaError("Google Imagen: rate limited (429)")
            
        if resp.status_code != 200:
            raise TransientMediaError(f"Google Imagen: unexpected status {resp.status_code}. {resp.text}")

        data = resp.json()
        predictions = data.get("predictions", [])
        if not predictions:
            raise TransientMediaError("Google Imagen: no predictions returned")
            
        b64_image = predictions[0].get("bytesBase64Encoded")
        if not b64_image:
            raise TransientMediaError("Google Imagen: no image bytes in response")

        import base64
        try:
            image_bytes = base64.b64decode(b64_image)
            return image_bytes
        except Exception as e:
            raise TransientMediaError(f"Google Imagen: failed to decode base64: {e}")


# ---------------------------------------------------------------------------
# Cloudflare Workers AI Provider
# ---------------------------------------------------------------------------

class CloudflareProvider:
    """
    Cloudflare Workers AI — основний безкоштовний провайдер.
    API: POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/{MODEL}
    Response: JSON {"image": "<base64>"}
    """
    name = "cloudflare"

    def __init__(self):
        self.account_id = settings.cloudflare_account_id
        self.api_token = settings.cloudflare_api_token
        self.model = settings.cloudflare_image_model

    def is_configured(self) -> bool:
        return bool(self.account_id and self.api_token)

    def generate(self, prompt: str, output_path: Path,
                 width: int, height: int, timeout: int) -> Path:
        if not self.is_configured():
            raise PermanentMediaError("Cloudflare credentials not configured")

        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/ai/run/{self.model}"
        )
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        # Flux-schnell рекомендує 4 steps для швидкої генерації
        payload = {
            "prompt": prompt,
            "width": _round_to_multiple(width, 32),
            "height": _round_to_multiple(height, 32),
            "steps": 4,
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.Timeout:
            raise TransientMediaError("Cloudflare: timeout")
        except requests.ConnectionError as e:
            raise TransientMediaError(f"Cloudflare: connection error: {e}")

        # Класифікація помилок
        if resp.status_code in (401, 403):
            raise PermanentMediaError(f"Cloudflare: auth error {resp.status_code}")
        if resp.status_code == 400:
            raise PermanentMediaError(f"Cloudflare: bad request: {resp.text[:300]}")
        if resp.status_code == 429:
            raise TransientMediaError("Cloudflare: rate limited (429)")
        if resp.status_code >= 500:
            raise TransientMediaError(f"Cloudflare: server error {resp.status_code}")
        if resp.status_code != 200:
            raise TransientMediaError(f"Cloudflare: unexpected status {resp.status_code}")

        # Парсимо base64 з JSON
        try:
            data = resp.json()
        except ValueError:
            raise TransientMediaError("Cloudflare: invalid JSON response")

        # Cloudflare може повертати {"result": {"image": "..."}} або {"image": "..."}
        image_b64 = None
        if isinstance(data, dict):
            image_b64 = data.get("image") or (data.get("result") or {}).get("image")

        if not image_b64:
            # Перевіримо чи це помилка квоти (Error 4006)
            errors = data.get("errors", [])
            for err in errors:
                code = err.get("code", 0)
                if code == 4006:
                    raise TransientMediaError("Cloudflare: daily free tier limit reached")
            raise TransientMediaError(f"Cloudflare: no image in response: {str(data)[:300]}")

        try:
            image_bytes = base64.b64decode(image_b64)
        except Exception as e:
            raise TransientMediaError(f"Cloudflare: base64 decode failed: {e}")

        return image_bytes


# ---------------------------------------------------------------------------
# Pollinations Provider (Fallback)
# ---------------------------------------------------------------------------

class PollinationsProvider:
    """
    Pollinations AI — fallback провайдер.
    API: GET https://image.pollinations.ai/prompt/{encoded_prompt}?params
    Response: Raw binary image data (PNG/JPEG)
    """
    name = "pollinations"

    def __init__(self):
        self.api_key = settings.pollinations_api_key
        self.model = settings.pollinations_image_model

    def is_configured(self) -> bool:
        # Pollinations працює навіть без ключа (з rate limits)
        return True

    def generate(self, prompt: str, output_path: Path,
                 width: int, height: int, timeout: int) -> Path:
        encoded_prompt = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        params = {
            "model": self.model,
            "width": width,
            "height": height,
            "seed": uuid.uuid4().int % 1_000_000,  # Випадковий seed
        }
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=timeout, stream=True)
        except requests.Timeout:
            raise TransientMediaError("Pollinations: timeout")
        except requests.ConnectionError as e:
            raise TransientMediaError(f"Pollinations: connection error: {e}")

        if resp.status_code in (401, 403):
            raise PermanentMediaError(f"Pollinations: auth error {resp.status_code}")
        if resp.status_code == 400:
            raise PermanentMediaError(f"Pollinations: bad request")
        if resp.status_code == 429:
            raise TransientMediaError("Pollinations: rate limited (429)")
        if resp.status_code >= 500:
            raise TransientMediaError(f"Pollinations: server error {resp.status_code}")
        if resp.status_code != 200:
            raise TransientMediaError(f"Pollinations: unexpected status {resp.status_code}")

        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            raise TransientMediaError(
                f"Pollinations: unexpected Content-Type: {content_type}"
            )

        image_bytes = resp.content
        return image_bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_to_multiple(value: int, multiple: int) -> int:
    """Округлює до найближчого кратного (Cloudflare вимагає кратність 32)."""
    return max(multiple, (value // multiple) * multiple)


def _validate_and_save_image(
    image_bytes: bytes,
    output_path: Path,
    max_bytes: int,
    target_width: int,
    target_height: int,
) -> dict:
    """
    Валідує отримані байти як зображення, конвертує в RGB JPEG,
    strip EXIF, зберігає атомарно через .tmp → rename.
    
    Повертає dict з метаданими: mime_type, size_bytes, width, height.
    Кидає TransientMediaError якщо щось не так.
    """
    # 1. Перевірка розміру
    if len(image_bytes) > max_bytes:
        raise TransientMediaError(
            f"Image too large: {len(image_bytes)} bytes (max {max_bytes})"
        )

    # 2. Декодуємо через Pillow (перевірка що це справжнє зображення)
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()  # Перевіряє цілісність, але закриває об'єкт
        # Після verify() треба перевідкрити
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        raise TransientMediaError(f"Image validation failed (Pillow): {e}")

    # 3. Конвертуємо в RGB (strip alpha, strip EXIF)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")

    actual_width, actual_height = img.size

    # 4. Зберігаємо атомарно: спочатку .tmp, потім rename
    tmp_path = output_path.with_suffix(".tmp")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        img.save(tmp_path, format="JPEG", quality=90, optimize=True)
        # Атомарне перейменування
        tmp_path.replace(output_path)
    except Exception as e:
        # Cleanup tmp якщо щось пішло не так
        if tmp_path.exists():
            tmp_path.unlink()
        raise TransientMediaError(f"Failed to save image: {e}")
    finally:
        img.close()

    final_size = output_path.stat().st_size

    return {
        "mime_type": "image/jpeg",
        "size_bytes": final_size,
        "width": actual_width,
        "height": actual_height,
    }


# ---------------------------------------------------------------------------
# Media Builder (Cascade Orchestrator)
# ---------------------------------------------------------------------------

class MediaBuilder:
    """
    Каскадний оркестратор генерації зображень.
    Пробує провайдерів по черзі. Якщо всі впали — повертає None.
    """

    def __init__(self):
        self._providers: dict[str, object] = {
            "google": GoogleImagenProvider(),
            "cloudflare": CloudflareProvider(),
            "pollinations": PollinationsProvider(),
        }
        self.media_dir = Path(settings.db_path).parent / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def get_provider_order(self) -> list[str]:
        """Повертає список імен провайдерів у порядку пріоритету."""
        raw = settings.media_provider_order
        return [p.strip() for p in raw.split(",") if p.strip()]

    def generate(self, draft_id: int, prompt: str) -> Optional[dict]:
        """
        Генерує зображення каскадно. Повертає dict з метаданими або None.
        
        Повертає:
            {
                "media_path": "media/draft_42_abc123.jpg",
                "media_provider": "cloudflare",
                "mime_type": "image/jpeg",
                "size_bytes": 123456,
                "width": 1024,
                "height": 768,
            }
            або None якщо всі провайдери впали.
        """
        if not settings.media_generation_enabled:
            logger.debug("Media generation is disabled in settings")
            return None

        # Генеруємо безпечне ім'я файлу (код формує, не LLM)
        short_uuid = uuid.uuid4().hex[:8]
        filename = f"draft_{draft_id}_{short_uuid}.jpg"
        output_path = self.media_dir / filename

        provider_order = self.get_provider_order()
        last_error = None

        for provider_name in provider_order:
            provider = self._providers.get(provider_name)
            if provider is None:
                logger.warning(f"MediaBuilder: Unknown provider '{provider_name}', skipping")
                continue

            if hasattr(provider, "is_configured") and not provider.is_configured():
                logger.debug(f"MediaBuilder: {provider_name} not configured, skipping")
                continue

            try:
                logger.info(f"MediaBuilder: Trying {provider_name} for draft {draft_id}...")
                image_bytes = provider.generate(
                    prompt=prompt,
                    output_path=output_path,
                    width=settings.media_image_width,
                    height=settings.media_image_height,
                    timeout=settings.media_generation_timeout,
                )

                # --- ДОДАВАННЯ НАКЛАДАННЯ (WIDGET/BADGE) ---
                image_overlay_type = db.get_setting("image_overlay", "btc_price")
                if image_overlay_type != "none":
                    try:
                        # Отримуємо категорію і сентимент з БД
                        with db._get_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute("SELECT sentiment, category FROM drafts WHERE id = ?", (draft_id,))
                            row = cursor.fetchone()
                            sentiment = row['sentiment'] if row and row['sentiment'] else 'Neutral'
                            category = row['category'] if row and row['category'] else 'NEWS'
                        
                        # Отримуємо актуальну ціну BTC
                        btc_data = market_data.get_btc_data()
                        
                        # Імпорт нової фабрики
                        from widget_renderer import render_overlay, OverlayData
                        overlay_data = OverlayData(
                            price=btc_data['price'],
                            change_pct=btc_data['change_pct'],
                            sentiment=sentiment,
                            category=category
                        )
                        
                        # Накладаємо віджет/бейдж
                        image_bytes = render_overlay(image_bytes, image_overlay_type, overlay_data)
                    except Exception as e:
                        logger.error(f"MediaBuilder: Failed to render overlay for draft {draft_id}: {e}")
                # -------------------------

                # Валідуємо і зберігаємо
                metadata = _validate_and_save_image(
                    image_bytes=image_bytes,
                    output_path=output_path,
                    max_bytes=settings.media_max_bytes,
                    target_width=settings.media_image_width,
                    target_height=settings.media_image_height,
                )

                logger.success(
                    f"MediaBuilder: Image generated by {provider_name} for draft {draft_id} "
                    f"({metadata['width']}x{metadata['height']}, {metadata['size_bytes']} bytes)"
                )

                return {
                    "media_path": str(Path("media") / filename),
                    "media_provider": provider_name,
                    **metadata,
                }

            except PermanentMediaError as e:
                logger.error(f"MediaBuilder: Permanent error from {provider_name}: {e}")
                last_error = str(e)
                # НЕ переходимо до наступного — це постійна помилка
                break

            except TransientMediaError as e:
                logger.warning(f"MediaBuilder: Transient error from {provider_name}: {e}")
                last_error = str(e)
                # Переходимо до наступного провайдера
                continue

            except Exception as e:
                logger.error(f"MediaBuilder: Unexpected error from {provider_name}: {e}")
                last_error = str(e)
                continue

        # Усі провайдери впали
        logger.warning(
            f"MediaBuilder: All providers failed for draft {draft_id}. "
            f"Last error: {last_error}"
        )
        return None

    def delete_media_file(self, media_path: str) -> bool:
        """
        Безпечне видалення файлу медіа.
        Перевіряє що шлях належить дозволеній директорії media/.
        """
        if not media_path:
            return False

        file_path = Path(settings.db_path).parent / media_path

        # Захист від path traversal
        try:
            resolved = file_path.resolve()
            media_resolved = self.media_dir.resolve()
            if not str(resolved).startswith(str(media_resolved)):
                logger.error(f"MediaBuilder: Path traversal attempt blocked: {media_path}")
                return False
        except Exception:
            return False

        if file_path.exists():
            try:
                file_path.unlink()
                logger.info(f"MediaBuilder: Deleted media file: {media_path}")
                return True
            except OSError as e:
                logger.error(f"MediaBuilder: Failed to delete {media_path}: {e}")
                return False

        return False


# Singleton
media_builder = MediaBuilder()
