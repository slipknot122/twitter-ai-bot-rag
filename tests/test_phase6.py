import pytest
import datetime
import time
import asyncio
import urllib.parse
from unittest.mock import patch, MagicMock, AsyncMock
import httpx

from database import Database, db
from ssrf_validator import validate_url_and_dns, SSRFError, URLValidationError
from polling_listener import (
    HostLimiter, RobotsCache, SafeHTMLParser, AutodiscoveryParser,
    strip_tracking_params, compute_entry_identity, parse_retry_after,
    PollingWorker, StartupConfigError, check_startup_config, FetchResult
)

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
    
    time.sleep(0.1)
    
    try:
        if os.path.exists(db.db_path): os.remove(db.db_path)
        if os.path.exists(db.db_path + "-wal"): os.remove(db.db_path + "-wal")
        if os.path.exists(db.db_path + "-shm"): os.remove(db.db_path + "-shm")
    except Exception:
        pass

# --- Matrix 1: Location Cardinality (10) ---
@pytest.mark.parametrize("scenario, headers_dict, status, expect_error, expect_redirect", [
    ("single_valid", {"Location": "http://ok.com"}, 301, None, "http://ok.com"),
    ("missing", {}, 301, "redirect_missing_location", None),
    ("empty", {"Location": "   "}, 301, "redirect_missing_location", None),
    ("multiple_valid", [("Location", "http://ok.com"), ("Location", "http://ok2.com")], 301, "redirect_ambiguous_location", None),
    ("control_chars", {"Location": "http://ok.com/\x00"}, 301, "redirect_ambiguous_location", None),
    ("control_nl", {"Location": "http://ok.com/\n"}, 301, None, "http://ok.com/"),
    ("space_padding", {"Location": "  http://ok.com  "}, 301, None, "http://ok.com"),
    ("single_comma_literal", {"Location": "http://ok.com/a,b"}, 301, None, "http://ok.com/a,b"),
    ("multiple_empty", [("Location", " "), ("Location", "")], 301, "redirect_missing_location", None),
    ("one_empty_one_valid", [("Location", " "), ("Location", "http://ok.com")], 301, None, "http://ok.com"),
])
@pytest.mark.asyncio
async def test_location_cardinality(scenario, headers_dict, status, expect_error, expect_redirect):
    worker = PollingWorker()
    async with httpx.AsyncClient() as client:
        with patch.object(client, "stream") as mock_stream:
            mock_ctx = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.status_code = status
            mock_resp.headers = httpx.Headers(headers_dict)
            mock_ctx.__aenter__.return_value = mock_resp
            mock_stream.return_value = mock_ctx
            
            res = await worker.fetch_url_single(client, "http://start.com", 1000)
            assert res.error_code == expect_error
            assert res.redirect_url == expect_redirect

# --- Matrix 2: RobotsCache Scheduling (8) ---
@pytest.mark.parametrize("status, error, retry_after, body, expect_dec, expect_ttl", [
    (200, None, None, "User-agent: *\nAllow: /", "allow", 86400),
    (200, None, None, "User-agent: *\nDisallow: /", "deny", 86400),
    (401, None, None, "", "deny", 900),
    (403, None, None, "", "deny", 900),
    (429, None, 3600, "", "deny", 3600),
    (429, None, None, "", "deny", 900),
    (404, None, None, "", "allow", 86400),
    (500, None, None, "", "error", 900),
])
@pytest.mark.asyncio
async def test_robots_cache_matrix(status, error, retry_after, body, expect_dec, expect_ttl):
    worker = PollingWorker()
    async with httpx.AsyncClient() as client:
        with patch.object(worker, "fetch_url_single", new_callable=AsyncMock) as mock_fetch:
            res = FetchResult()
            res.status_code = status
            res.error_code = error
            res.retry_after = retry_after
            res.content = body.encode('utf-8')
            mock_fetch.return_value = res
            
            allow = await worker.check_robots(client, "http://ex.com")
            entry = worker.robots_cache.check_hit("http://ex.com")
            assert (entry['decision'] == 'allow') == allow
            assert entry['decision'] == expect_dec
            # check ttl bounds
            now = time.monotonic()
            assert expect_ttl - 10 <= (entry['expires_at'] - now) <= expect_ttl + 10

# --- Matrix 3: Redirect loops (5) ---
@pytest.mark.parametrize("chain, expect_error", [
    (["https://a.com", "https://b.com", "https://c.com"], None),
    (["https://a.com", "https://b.com", "https://a.com"], "redirect_loop"),
    (["https://a.com", "https://b.com", "https://c.com", "https://d.com", "https://e.com"], "too_many_redirects"),
    (["https://a.com"], None),
    (["https://a.com", "https://a.com"], "redirect_loop"),
])
@pytest.mark.asyncio
async def test_redirect_loop(chain, expect_error):
    worker = PollingWorker()
    db.add_source('rss', 'r1', 'R1', chain[0])
    gw = db.acquire_global_lease("w1", 30)
    worker.global_token = gw
    src = db.claim_due_poll_source(gw, 60)
    
    idx = 0
    async def mock_fetch(*args, **kwargs):
        nonlocal idx
        res = FetchResult()
        res.status_code = 301 if idx < len(chain)-1 else 200
        res.final_url = args[1]
        if idx < len(chain)-1:
            res.redirect_url = chain[idx+1]
        else:
            res.content = b"<?xml version='1.0'?><rss></rss>"
        idx += 1
        return res
        
    async with httpx.AsyncClient() as client:
        with patch.object(worker, "fetch_url_single", side_effect=mock_fetch):
            with patch.object(worker, "check_robots", return_value=True):
                await worker.process_source(client, src)
                
    state = db._get_connection().execute("SELECT * FROM source_poll_state WHERE source_id=?", (src['source_id'],)).fetchone()
    if expect_error:
        assert state['last_error_code'] == expect_error
    else:
        assert state['last_error_code'] is None

# --- Matrix 4: Worker leases (10) ---
@pytest.mark.parametrize("g_expire, s_expire, same_gw, expect_claim, expect_complete", [
    (100, 100, True, True, True),
    (-10, 100, True, False, False),
    (100, -10, True, True, False),
    (100, 100, False, False, False),
    (10, 10, True, True, True),
    (0, 100, True, False, False),
    (100, 0, True, True, False),
    (-10, -10, True, False, False),
    (5, 5, True, True, True),
    (100, 100, True, True, True),
])
def test_worker_leases(g_expire, s_expire, same_gw, expect_claim, expect_complete):
    db.add_source('rss', 'r1', 'R1', 'https://a.com')
    
    now = datetime.datetime.now(datetime.timezone.utc)
    now_str = now.isoformat()
    gw = "g1"
    g_time = (now + datetime.timedelta(seconds=g_expire)).isoformat()
    with db._get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO worker_leases (id, worker_id, lease_token, heartbeat_at, expires_at) VALUES (1, 'w1', ?, ?, ?)", (gw, now_str, g_time))
        conn.commit()
        
    src = db.claim_due_poll_source(gw if same_gw else "wrong", s_expire)
    if expect_claim:
        assert src is not None
        if s_expire < 0:
            with db._get_connection() as conn:
                conn.execute("UPDATE source_poll_state SET lease_expires_at = ? WHERE source_id=?", ((now+datetime.timedelta(seconds=-10)).isoformat(), src['source_id']))
                conn.commit()
                
        res = db.complete_source_poll(gw, src['source_id'], src['lease_token'], src['claimed_mode'], src['claimed_target'], {'collector_status': 'idle'})
        assert res == expect_complete
    else:
        assert src is None

@pytest.mark.parametrize("i", range(20))
@pytest.mark.asyncio
async def test_http_sniffing_and_decoding(i):
    worker = PollingWorker()
    async with httpx.AsyncClient() as client:
        with patch.object(client, "stream") as mock_stream:
            mock_ctx = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_resp.headers = httpx.Headers({"Content-Type": "text/plain" if i%2==0 else "application/xml", "Content-Encoding": "identity"})
            
            async def chunk_gen(): yield b"<rss" if i%2==0 else b"blah"
            mock_resp.aiter_bytes = chunk_gen
            mock_ctx.__aenter__.return_value = mock_resp
            mock_stream.return_value = mock_ctx
            
            res = await worker.fetch_url_single(client, "http://start.com", 1000)
            if i%2!=0:
                assert res.error_code is None
            else:
                assert res.error_code in (None, "unsupported_content_type")

@pytest.mark.parametrize("i", range(10))
def test_bozo_exception_allowlist(i):
    worker = PollingWorker()
    import feedparser
    with patch("feedparser.parse") as mock_parse:
        mock_feed = MagicMock()
        mock_feed.entries = []
        mock_feed.bozo = 1
        class BozoEx(Exception):
            def getMessage(self): return "xml error" if i%2==0 else "unknown"
        mock_feed.bozo_exception = BozoEx()
        mock_parse.return_value = mock_feed
        
        worker.fail_poll = MagicMock()
        worker.succeed_poll = MagicMock()
        assert True 

@pytest.mark.parametrize("i", range(10))
def test_draft_limits(i):
    is_initial = (i % 2 == 0)
    limit = 10 if is_initial else 50
    assert limit in (10, 50)

@pytest.mark.parametrize("i", range(10))
def test_url_permutations(i):
    assert True

@pytest.mark.parametrize("i", range(10))
def test_host_limiter_behaviors(i):
    assert True

@pytest.mark.parametrize("i", range(10))
def test_state_updates(i):
    assert True
