# Twitter AI Bot with RAG (Retrieval-Augmented Generation)

An autonomous AI agent designed for monitoring Telegram news channels and publishing expert-level rewritten tweets using an advanced RAG pipeline.

## Features

- **Telegram Listener**: Monitors specified Telegram channels in real-time.
- **RAG Architecture**:
  - **Knowledge Base**: Uses local JSON files (`knowledge/*.json`) to inject opinions, background context, and narratives about specific entities.
  - **Semantic Memory**: Saves all published tweets to an SQLite vector database (via `numpy` and `gemini-embedding-2`), preventing duplicate takes on similar news.
- **Multi-Model LLM Fallback**: Includes a robust fallback cascade of 10 Google Gemini models to handle API rate limits smoothly.
- **Reliable Task Queue**: Powered by SQLite to manage drafts. If the bot crashes, no news is lost.
- **Retry Manager & Scheduler**: Built-in exponential backoff for failed LLM requests and delayed publishing.
- **Web Admin UI**: A lightweight local FastAPI interface to manage and approve drafts before they go live.

## Getting Started

1. Clone the repository.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your API keys (Telegram, Twitter, Gemini/OpenAI).
4. Run the bot:
   ```bash
   python main.py
   ```
5. Open the Web Admin UI at `http://127.0.0.1:8000` to review incoming drafts.

## Architecture

- `telegram_listener.py`: Fetches news.
- `ai_worker.py`: Manages the processing queue.
- `ai_engine.py`: Core AI logic.
- `context_builder.py`: Synthesizes RAG context from the Knowledge Base and Semantic Memory.
- `semantic_memory.py`: Vector DB logic.
- `scheduler.py`: Handles delayed posting and retries.
- `web_admin/main.py`: FastAPI backend and HTML frontend for manual approval.
