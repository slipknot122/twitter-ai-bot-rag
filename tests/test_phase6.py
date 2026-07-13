import pytest
import datetime
import time
import asyncio
import urllib.parse
from unittest.mock import patch, MagicMock, AsyncMock

from database import Database, db
from ssrf_validator import validate_url_syntax, validate_dns_resolution, validate_url_and_dns, SSRFError, URLValidationError
from polling_listener import (
    HostLimiter, RobotsCache, SafeHTMLParser, AutodiscoveryParser,
    strip_tracking_params, compute_entry_identity, parse_retry_after,
    PollingWorker, StartupConfigError, check_startup_config
)
import httpx

@pytest.fixture(autouse=True)
def setup_teardown(tmp_path):
    import os
    os.environ["RSS_EGRESS_SANDBOX_CONFIRMED"] = "true"
    db.db_path = str(tmp_path / "test.db")
    if hasattr(db, '_local') and hasattr(db._local, 'connection'):
        db._local.connection = None
    db._init_db()
    
    with db._get_connection() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        
    yield
    
    if hasattr(db, '_local') and hasattr(db._local, 'connection') and db._local.connection:
        try:
            db._local.connection.close()
        except:
            pass
        db._local.connection = None
    
    import time
    time.sleep(0.1)
    
    try:
        if os.path.exists(db.db_path):
            os.remove(db.db_path)
            if os.path.exists(db.db_path + "-wal"): os.remove(db.db_path + "-wal")
            if os.path.exists(db.db_path + "-shm"): os.remove(db.db_path + "-shm")
    except Exception:
        pass

def test_startup_guard_missing_flag():
    import os
    if "RSS_EGRESS_SANDBOX_CONFIRMED" in os.environ:
        del os.environ["RSS_EGRESS_SANDBOX_CONFIRMED"]
    with pytest.raises(StartupConfigError):
        check_startup_config()

def test_host_limiter_monotonic(monkeypatch):
    limiter = HostLimiter()
    
    times = [100.0, 100.5, 111.0]
    idx = 0
    def mock_monotonic():
        nonlocal idx
        res = times[idx]
        if idx < len(times)-1: idx += 1
        return res
        
    monkeypatch.setattr(time, "monotonic", mock_monotonic)
    
    async def run():
        k = await limiter.acquire("http://example.com")
        limiter.release(k)
        
    asyncio.run(run())
    assert limiter.next_allowed["http://example.com:80"] > 100.0
    
def test_host_limiter_cleanup():
    limiter = HostLimiter()
    async def run():
        k = await limiter.acquire("http://example.com")
        limiter.release(k)
        limiter.last_used[k] = time.monotonic() - 400 # Over TTL
        await limiter.cleanup()
        assert k not in limiter.locks
    asyncio.run(run())

def test_source_claiming_excludes_unsupported():
    s_rss = db.add_source('rss', 'r1', 'R1', 'https://ok.com/feed.xml')
    s_web = db.add_source('website', 'w1', 'W1', 'https://ok.com')
    
    with db._get_connection() as conn:
        conn.execute("UPDATE source_poll_state SET collector_status = 'unsupported' WHERE source_id = ?", (s_web['id'],))
        conn.commit()
    
    gw = db.acquire_global_lease("w1", 30)
    s1 = db.claim_due_poll_source(gw, 60)
    assert s1['source_type'] == 'rss'
    assert s1['external_id'] == 'r1'
    
    s2 = db.claim_due_poll_source(gw, 60)
    assert s2 is None # source web is unsupported
    
    db.poll_now(s_web['id']) # Requeue
    s2 = db.claim_due_poll_source(gw, 60)
    assert s2['source_id'] == s_web['id']

def test_complete_source_poll_oversize_batch():
    db.add_source('rss', 'r1', 'R1', 'https://ok.com/feed.xml')
    gw = db.acquire_global_lease("w1", 30)
    src = db.claim_due_poll_source(gw, 60)
    
    drafts = [{"source_item_id": f"id{i}", "original_text": f"text{i}"} for i in range(11)]
    
    with pytest.raises(ValueError, match="exceeds limit"):
        db.complete_source_poll(gw, src['source_id'], src['lease_token'], {'collector_status': 'healthy'}, drafts)

def test_complete_source_poll_stale_global_token():
    db.add_source('rss', 'r1', 'R1', 'https://ok.com/feed.xml')
    gw = db.acquire_global_lease("w1", 30)
    src = db.claim_due_poll_source(gw, 60)
    
    # Use wrong token
    res = db.complete_source_poll("wrong", src['source_id'], src['lease_token'], {'collector_status': 'healthy'}, [])
    assert res is False

def test_complete_source_poll_stale_source_token():
    db.add_source('rss', 'r1', 'R1', 'https://ok.com/feed.xml')
    gw = db.acquire_global_lease("w1", 30)
    src = db.claim_due_poll_source(gw, 60)
    
    res = db.complete_source_poll(gw, src['source_id'], "wrong", {'collector_status': 'healthy'}, [])
    assert res is False
    
def test_robots_cache():
    cache = RobotsCache()
    assert cache.check_hit("https://example.com/feed") is None
    cache.store("https://example.com/feed", True, 3600)
    assert cache.check_hit("https://example.com/feed") is True
    assert cache.check_hit("https://example.com/other") is True

def test_parse_retry_after():
    assert parse_retry_after("120") == 120
    assert parse_retry_after("5") == 10 # clamp
    assert parse_retry_after("999999") == 86400 # clamp
    assert parse_retry_after("invalid") is None
    
    import email.utils
    now = datetime.datetime.now(datetime.timezone.utc)
    future = now + datetime.timedelta(seconds=120)
    date_str = email.utils.format_datetime(future)
    parsed = parse_retry_after(date_str)
    assert 118 <= parsed <= 122

def test_strip_tracking_params():
    url = "https://ex.com/path?utm_source=a&fbclid=b&valid=c#frag"
    clean = strip_tracking_params(url)
    assert clean == "https://ex.com/path?valid=c"

def test_compute_entry_identity():
    entry1 = {"id": "  hello  \nworld"}
    id1 = compute_entry_identity(entry1, "https://example.com")
    assert id1.startswith("guid:")
    
    entry2 = {"link": "/relative?utm_source=a"}
    id2 = compute_entry_identity(entry2, "https://example.com/base/")
    assert id2.startswith("link:")

def test_safe_html_parser():
    parser = SafeHTMLParser()
    parser.feed("  <script>alert(1)</script> <b>hello</b> \x00 world \n\t ")
    assert parser.get_text() == "hello world"

def test_ssrf_validator_safe():
    res = asyncio.run(validate_url_and_dns("http://google.com"))
    assert res == "http://google.com"

def test_ssrf_validator_private():
    with pytest.raises(SSRFError):
        asyncio.run(validate_url_and_dns("http://192.168.1.1"))

def test_ssrf_validator_metadata():
    with pytest.raises(SSRFError):
        asyncio.run(validate_url_and_dns("http://169.254.169.254"))

def test_heartbeat_cancellation():
    worker = PollingWorker()
    worker.global_token = "dummy"
    async def run():
        async def dummy_poll():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                worker.cancellation_event.set()
        worker.active_poll_task = asyncio.create_task(dummy_poll())
        
        # Start heartbeat, it should notice lease is lost (no DB record)
        await worker.heartbeat_loop()
        
        assert worker.cancellation_event.is_set()
        assert worker.active_poll_task.cancelled() or worker.active_poll_task.done()
    asyncio.run(run())

def test_fetch_url_timeout():
    worker = PollingWorker()
    async def run():
        async with httpx.AsyncClient() as client:
            with patch("httpx.AsyncClient.stream") as mock_stream:
                mock_stream.side_effect = httpx.TimeoutException("timeout")
                res = await worker.fetch_url(client, "http://example.com", 1000)
                assert res.error_code == "timeout"
    asyncio.run(run())

def test_fetch_url_too_large():
    worker = PollingWorker()
    async def run():
        async with httpx.AsyncClient() as client:
            with patch.object(client, "stream") as mock_stream:
                mock_ctx = AsyncMock()
                mock_resp = AsyncMock()
                mock_resp.status_code = 200
                mock_resp.headers = {"Content-Type": "text/html"}
                
                async def chunk_gen():
                    yield b"a" * 6000000
                    
                mock_resp.aiter_bytes = chunk_gen
                mock_ctx.__aenter__.return_value = mock_resp
                mock_stream.return_value = mock_ctx
                
                res = await worker.fetch_url(client, "http://example.com", max_bytes=5000000, website_mode=True)
                assert res.error_code == "content_too_large"
    asyncio.run(run())

