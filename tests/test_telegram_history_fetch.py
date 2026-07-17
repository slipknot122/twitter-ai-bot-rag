import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import telegram_listener
from web_admin.main import app


client = TestClient(app)


def test_fetch_history_returns_503_when_listener_not_ready():
    with patch.object(telegram_listener, "history_fetch_queue", None):
        response = client.post(
            "/api/telegram/fetch-history",
            json={"messages_limit": 5, "channels_limit": 10},
        )

    assert response.status_code == 503


def test_fetch_history_enqueues_bounded_request():
    queue = asyncio.Queue(maxsize=1)
    with patch.object(telegram_listener, "history_fetch_queue", queue):
        response = client.post(
            "/api/telegram/fetch-history",
            json={"messages_limit": 4, "channels_limit": 7},
        )

    assert response.status_code == 200
    assert queue.get_nowait() == {"messages": 4, "channels": 7}


def test_fetch_history_accepts_documented_maximum_limits():
    queue = asyncio.Queue(maxsize=1)
    with patch.object(telegram_listener, "history_fetch_queue", queue):
        response = client.post(
            "/api/telegram/fetch-history",
            json={"messages_limit": 20, "channels_limit": 50},
        )

    assert response.status_code == 200
    assert queue.get_nowait() == {"messages": 20, "channels": 50}


def test_fetch_history_returns_409_when_request_already_queued():
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait({"messages": 1, "channels": 1})

    with patch.object(telegram_listener, "history_fetch_queue", queue):
        response = client.post(
            "/api/telegram/fetch-history",
            json={"messages_limit": 5, "channels_limit": 10},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Завантаження історії Telegram уже додано до черги."


@pytest.mark.parametrize(
    "payload",
    [
        {"messages_limit": 0, "channels_limit": 1},
        {"messages_limit": 21, "channels_limit": 1},
        {"messages_limit": 1, "channels_limit": 0},
        {"messages_limit": 1, "channels_limit": 51},
    ],
)
def test_fetch_history_rejects_out_of_range_limits(payload):
    queue = asyncio.Queue(maxsize=1)
    with patch.object(telegram_listener, "history_fetch_queue", queue):
        response = client.post("/api/telegram/fetch-history", json=payload)

    assert response.status_code == 422
    assert queue.empty()


def test_history_worker_filters_sources_and_honors_limits():
    async def scenario():
        queue = asyncio.Queue(maxsize=1)
        await queue.put({"messages": 2, "channels": 1})

        db_instance = MagicMock()
        db_instance.get_sources.return_value = [
            {"source_type": "rss", "resolution_status": "resolved", "external_id": "1"},
            {"source_type": "telegram", "resolution_status": "unresolved", "external_id": "2"},
            {"source_type": "telegram", "resolution_status": "resolved", "external_id": "-1003"},
            {"source_type": "telegram", "resolution_status": "resolved", "external_id": "-1004"},
        ]

        messages = [
            SimpleNamespace(text="first message", chat_id=-1003, id=1),
            SimpleNamespace(text="second message", chat_id=-1003, id=2),
        ]
        client_instance = MagicMock()
        client_instance.get_dialogs = AsyncMock()

        async def iter_messages(channel_id, limit):
            assert channel_id == -1003
            assert limit == 2
            for message in messages:
                yield message

        client_instance.iter_messages = iter_messages

        with (
            patch.object(telegram_listener, "process_telegram_event", new=AsyncMock()) as process,
            patch.object(telegram_listener.asyncio, "sleep", new=AsyncMock()),
        ):
            worker = asyncio.create_task(
                telegram_listener._history_fetch_worker(
                    client_instance,
                    MagicMock(),
                    db_instance,
                    queue,
                )
            )
            await asyncio.wait_for(queue.join(), timeout=1)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

        assert process.await_count == 2
        client_instance.get_dialogs.assert_awaited_once_with(limit=100)

    asyncio.run(scenario())


def test_history_worker_calls_task_done_when_processing_fails():
    async def scenario():
        queue = asyncio.Queue(maxsize=1)
        await queue.put({"messages": 1, "channels": 1})
        db_instance = MagicMock()
        db_instance.get_sources.side_effect = RuntimeError("database unavailable")

        worker = asyncio.create_task(
            telegram_listener._history_fetch_worker(
                MagicMock(),
                MagicMock(),
                db_instance,
                queue,
            )
        )
        await asyncio.wait_for(queue.join(), timeout=1)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

        assert queue.empty()

    asyncio.run(scenario())


def test_start_listener_waits_for_start_and_resets_queue_after_shutdown():
    async def scenario():
        client_instance = MagicMock()
        client_instance.start = AsyncMock()
        client_instance.run_until_disconnected = AsyncMock(return_value=None)
        client_instance.on.side_effect = lambda _event: lambda handler: handler

        with (
            patch.object(telegram_listener, "create_telegram_client", return_value=client_instance),
            patch.object(telegram_listener.source_cache, "reload", return_value=True),
        ):
            await telegram_listener.start_listener()

        client_instance.start.assert_awaited_once_with()
        assert telegram_listener.history_fetch_queue is None

    asyncio.run(scenario())
