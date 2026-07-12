import io
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from datetime import datetime
from loguru import logger
from dataclasses import dataclass
from typing import Optional

OVERLAY_NONE = "none"
OVERLAY_BTC_PRICE = "btc_price"
OVERLAY_CATEGORY_BADGE = "category_badge"

CATEGORY_COLORS = {
    "HACK": (239, 68, 68, 255),       # Red
    "SECURITY": (239, 68, 68, 255),   # Red
    "AIRDROP": (168, 85, 247, 255),   # Purple
    "LISTING": (59, 130, 246, 255),   # Blue
    "FUNDING": (34, 197, 94, 255),    # Green
    "REGULATION": (234, 179, 8, 255), # Yellow
    "MARKET": (255, 255, 255, 255),   # White
    "PARTNERSHIP": (236, 72, 153, 255),# Pink
    "NEWS": (156, 163, 175, 255),     # Gray
    "UNKNOWN": (156, 163, 175, 255)   # Gray
}

@dataclass
class OverlayData:
    price: float = 0.0
    change_pct: float = 0.0
    sentiment: str = "Neutral"
    category: str = "NEWS"

def _get_fonts():
    try:
        font_large = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 26)
        font_medium = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 20)
        font_small = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 16)
        font_badge = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 20)
    except IOError:
        font_large = ImageFont.load_default()
        font_medium = ImageFont.load_default()
        font_small = ImageFont.load_default()
        font_badge = ImageFont.load_default()
    return font_large, font_medium, font_small, font_badge

def _draw_text_with_shadow(d, pos, text, font, fill):
    x, y = pos
    d.text((x+1, y+1), text, font=font, fill=(0,0,0,100))
    d.text(pos, text, font=font, fill=fill)

def _apply_frosted_glass(base_img, x, y, w, h, radius):
    mask = Image.new('L', base_img.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([(x, y), (x + w, y + h)], radius=radius, fill=255)
    blurred_base = base_img.copy().filter(ImageFilter.GaussianBlur(6))
    base_img.paste(blurred_base, mask=mask)

def _draw_btc_widget(base_img: Image.Image, data: OverlayData) -> Image.Image:
    widget_width = 140
    widget_height = 110
    radius = 4
    margin = 15

    x_offset = base_img.width - widget_width - margin
    y_offset = margin

    _apply_frosted_glass(base_img, x_offset, y_offset, widget_width, widget_height, radius)

    overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Напівпрозорий фон
    draw.rounded_rectangle([(x_offset, y_offset), (x_offset + widget_width, y_offset + widget_height)], radius=radius, fill=(10, 10, 10, 30))

    # Колір сентименту
    sentiment_upper = data.sentiment.upper()
    if "BULLISH" in sentiment_upper:
        sent_color = (34, 197, 94, 255) # Green
    elif "BEARISH" in sentiment_upper:
        sent_color = (239, 68, 68, 255) # Red
    else:
        sent_color = (156, 163, 175, 255) # Gray/Neutral

    # Смужка
    draw.rounded_rectangle([(x_offset, y_offset), (x_offset + 6, y_offset + widget_height)], radius=radius, fill=sent_color)
    draw.rectangle([(x_offset + 3, y_offset), (x_offset + 6, y_offset + widget_height)], fill=sent_color)

    font_large, font_medium, font_small, _ = _get_fonts()

    # Дата
    date_str = datetime.now().strftime("%b %d").upper()
    draw.ellipse([(x_offset + 15, y_offset + 10), (x_offset + 35, y_offset + 30)], fill=(247, 147, 26, 255))
    draw.text((x_offset + 19, y_offset + 10), "₿", font=font_medium, fill=(255, 255, 255, 255))
    _draw_text_with_shadow(draw, (x_offset + 45, y_offset + 12), date_str, font_small, (220, 220, 220, 255))

    # Ціна
    price_str = f"${data.price:,.0f}" if data.price > 0 else "$0"
    _draw_text_with_shadow(draw, (x_offset + 15, y_offset + 45), price_str, font_large, (255, 255, 255, 255))

    # Зміна
    sign = "+" if data.change_pct >= 0 else ""
    change_str = f"{sign}{data.change_pct:.1f}%"
    change_color = (34, 197, 94, 255) if data.change_pct >= 0 else (239, 68, 68, 255)
    _draw_text_with_shadow(draw, (x_offset + 15, y_offset + 75), change_str, font_medium, change_color)

    return Image.alpha_composite(base_img, overlay)

def _draw_category_badge(base_img: Image.Image, data: OverlayData) -> Image.Image:
    widget_width = 140
    widget_height = 40
    radius = 4
    margin = 15

    x_offset = base_img.width - widget_width - margin
    y_offset = margin

    _apply_frosted_glass(base_img, x_offset, y_offset, widget_width, widget_height, radius)

    overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Напівпрозорий фон
    draw.rounded_rectangle([(x_offset, y_offset), (x_offset + widget_width, y_offset + widget_height)], radius=radius, fill=(10, 10, 10, 30))

    cat_upper = data.category.upper()[:15] # limit length
    color = CATEGORY_COLORS.get(cat_upper, (255, 255, 255, 255)) # Default white

    # Смужка
    draw.rounded_rectangle([(x_offset, y_offset), (x_offset + 6, y_offset + widget_height)], radius=radius, fill=color)
    draw.rectangle([(x_offset + 3, y_offset), (x_offset + 6, y_offset + widget_height)], fill=color)

    _, _, _, font_badge = _get_fonts()

    # Центрування тексту
    text_width = draw.textlength(cat_upper, font=font_badge)
    text_x = x_offset + 6 + (widget_width - 6 - text_width) / 2
    text_y = y_offset + 8

    _draw_text_with_shadow(draw, (text_x, text_y), cat_upper, font_badge, (255, 255, 255, 255))

    return Image.alpha_composite(base_img, overlay)

def render_overlay(image_bytes: bytes, overlay_type: str, data: OverlayData) -> bytes:
    """
    Головна точка входу для рендерингу накладань на картинку.
    """
    if overlay_type == OVERLAY_NONE:
        return image_bytes

    try:
        base_img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        
        # Перевірка на занадто маленьке зображення
        if base_img.width < 200 or base_img.height < 200:
            logger.warning("Image too small for overlay, skipping.")
            return image_bytes

        if overlay_type == OVERLAY_BTC_PRICE:
            final_img = _draw_btc_widget(base_img, data)
        elif overlay_type == OVERLAY_CATEGORY_BADGE:
            final_img = _draw_category_badge(base_img, data)
        else:
            logger.warning(f"Unknown overlay type: {overlay_type}")
            return image_bytes

        out_bytes = io.BytesIO()
        final_img.convert("RGB").save(out_bytes, format="PNG")
        return out_bytes.getvalue()

    except Exception as e:
        logger.error(f"Error rendering overlay: {e}")
        return image_bytes
