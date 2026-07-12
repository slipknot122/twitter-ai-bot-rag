import asyncio
from database import db
from ai_engine import AIEngine
from media_builder import media_builder
import os
import shutil

async def test():
    text = 'BREAKING: CZ Binance says the market is looking extremely bullish right now. Next bull run confirmed?'
    engine = AIEngine()
    result = engine.process_text(text)
    print('AI Result:', result)
    
    draft_id = db.create_draft(0, text)
    db.update_draft_status(draft_id, 'approved')
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE drafts SET rewritten_text=?, image_prompt=?, sentiment=?, category=? WHERE id=?',
            (result.get('tweet_text'), result.get('image_prompt'), result.get('sentiment'), result.get('category'), draft_id)
        )
        conn.commit()
        
    print(f'Draft {draft_id} updated. Starting media generation...')
    success = await media_builder.generate(draft_id, result.get('image_prompt', ''))
    print(f'Media generation success: {success}')
    
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT media_path FROM drafts WHERE id = ?', (draft_id,))
        row = cursor.fetchone()
        if row and row['media_path']:
            print(f"Media saved to: {row['media_path']}")
            dest = r'C:\Users\AlxDr\.gemini\antigravity\brain\6c19e0c5-c895-4ef1-bd68-2c491138ab9d\demo_e2e_result.png'
            shutil.copy(row['media_path'], dest)

asyncio.run(test())
