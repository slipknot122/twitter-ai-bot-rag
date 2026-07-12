import random
import json
import re
from llm_provider import llm
from database import db
from context_builder import context_builder
from loguru import logger

EDITOR_SYSTEM_PROMPT = """You are an AI Content Moderator and Twitter Editor for a premium crypto account.
Your task is to analyze incoming Telegram news updates and rewrite them into detailed, engaging English Twitter posts. Since the account has X Premium, you DO NOT have a 280-character limit. You can write long-form posts (longreads) using paragraphs and bullet points.

YOUR PERSONA:
You are an enthusiast deeply interested in crypto and AI. You trade, but you aren't a Wall Street pro—just a regular guy who isn't a sucker either. You know the basics: you don't buy the hype at the top, and you can analyze the market reasonably well. You have a calm, chill vibe, and you write casually from your phone. Occasionally use standard CT slang (like anon, wgmi) but without being overly hyped.

MODERATION RULES (CRITICAL):
1. **PUBLISH**: If the original post is informative (news, releases, analytics, research, official statements), rewrite it in English according to the persona.
2. **IGNORE**: If the post is primarily a referral campaign, a giveaway with no informational value, "register here", "use my code", or pure spam/advertising, ignore it.
3. **REVIEW**: If the post contains mixed content (some news, but also promotional elements) or you are unsure, flag it for review. If you see a referral link at the end of a good post, strip the link, keep the news, and mark as REVIEW or PUBLISH depending on confidence.

OUTPUT FORMAT:
You MUST output ONLY valid JSON format exactly like this, with no markdown code blocks outside of the JSON.
{
  "action": "PUBLISH",
  "confidence": 0.95,
  "reason": "market analysis",
  "tweet_text": "Your English rewritten post here... (Leave empty string if action is IGNORE or REVIEW with no rewrite possible)",
  "image_prompt": "A short English description (50-200 chars) for AI image generation. Describe a vivid, eye-catching scene that represents the news topic. Use cinematic style. Leave empty string if action is IGNORE.",
  "sentiment": "Neutral", // MUST be exactly one of: "Bullish", "Bearish", "Neutral"
  "category": "NEWS" // Return ONLY ONE of the allowed categories provided below.
}

REWRITE RULES (If action is PUBLISH or REVIEW):
1. MUST BE IN ENGLISH.
2. Introduce 1 or 2 VERY minor "micro-mistakes" (e.g. missing a comma, lowercase start of a sentence) so it looks 100% human.
3. DO NOT add any new facts or numbers.
"""

class AIEngine:
    def __init__(self):
        pass

    def process_text(self, text: str) -> dict:
        """
        Аналізує текст і повертає JSON-відповідь від LLM у вигляді словника.
        """
        logger.info(f"Sending text to LLM for rewrite (length: {len(text)})")
        
        # Витягуємо налаштування з БД або використовуємо дефолтні
        current_prompt = db.get_setting("system_prompt", EDITOR_SYSTEM_PROMPT)
        current_temp = db.get_setting("llm_temperature", 0.7)
        allowed_categories_str = db.get_setting("allowed_categories", "MARKET, LISTING, HACK, SECURITY, AIRDROP, FUNDING, REGULATION, PARTNERSHIP, TOKEN, AI, NFT, MEME, DEFI, STABLECOIN, EXCHANGE, NEWS")
        
        # Динамічно додаємо інструкцію про категорії в кінець системного промпта
        dynamic_prompt = f"{current_prompt}\n\nALLOWED CATEGORIES: You must choose exactly one category from this list: {allowed_categories_str}"
        
        # 1. Будуємо Soft Context
        context_str = context_builder.build_context(text)
        
        # 2. Формуємо фінальний промпт
        final_prompt = f"Original news update:\n{text}"
        if context_str:
            final_prompt = f"{final_prompt}\n\n{context_str}"
            
        try:
            llm_output = llm.generate(
                prompt=final_prompt,
                system_prompt=dynamic_prompt
            )
            
            # Парсимо JSON
            clean_json = llm_output.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.startswith("```"):
                clean_json = clean_json[3:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            clean_json = clean_json.strip()

            data = json.loads(clean_json)
            
            action = data.get("action", "REVIEW").upper()
            confidence = float(data.get("confidence", 0.0))
            reason = data.get("reason", "unknown")
            tweet_text = data.get("tweet_text", "")
            image_prompt = data.get("image_prompt", "")
            sentiment = data.get("sentiment", "Neutral")
            category_raw = data.get("category", "NEWS").upper()

            # Нормалізація категорії (Fallback на NEWS, якщо AI вигадав щось своє)
            # Ми розбиваємо рядок allowed_categories на список
            allowed_list = [c.strip().upper() for c in allowed_categories_str.split(',')]
            if category_raw not in allowed_list:
                logger.warning(f"AI returned unknown category '{category_raw}'. Fallback to NEWS.")
                category = "NEWS"
            else:
                category = category_raw

            if action == "PUBLISH" and confidence < 0.75:
                action = "REVIEW"
                reason += " (demoted due to low confidence)"
                
            return {
                "action": action,
                "confidence": confidence,
                "reason": reason,
                "tweet_text": tweet_text,
                "image_prompt": image_prompt,
                "sentiment": sentiment,
                "category": category
            }
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM JSON output: {e}\nRaw output: {llm_output}")
            return {
                "action": "FAILED",
                "confidence": 0.0,
                "reason": "json_parse_error",
                "tweet_text": llm_output
            }
        except Exception as e:
            logger.error(f"Failed to process text due to LLM error: {e}")
            return {
                "action": "FAILED",
                "confidence": 0.0,
                "reason": f"llm_error: {str(e)}",
                "tweet_text": ""
            }

ai_engine = AIEngine()

if __name__ == "__main__":
    logger.info("AI Engine module loaded.")
