import pytest
import asyncio
import datetime
import sqlite3
import httpx
import time
from unittest.mock import MagicMock, patch, AsyncMock
from polling_listener import PollingWorker, HostLimiter, RobotsCache, MAX_ROBOTS_BYTES
import database as db

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_bot.db"
    test_db = db.Database(str(db_path))
    with patch("polling_listener.db", test_db), patch("database.db", test_db):
        yield test_db

@pytest.fixture
def worker():
    w = PollingWorker()
    w.global_token = "test_token"
    return w

@pytest.fixture(autouse=True)
def mock_dns():
    async def dummy_validate(url):
        if "localhost" in url or "127.0.0.1" in url or "169.254.169.254" in url:
            from ssrf_validator import SSRFError
            raise SSRFError()
        return url
    with patch("polling_listener.validate_url_and_dns", side_effect=dummy_validate):
        yield

class MockHeaders:
    def __init__(self, raw_headers):
        self.raw = raw_headers
        self.d = {}
        for k, v in raw_headers:
            self.d[k.decode('ascii', 'ignore').lower()] = v.decode('ascii', 'ignore')
    def get(self, key, default=None):
        return self.d.get(key.lower(), default)
    def multi_items(self):
        return [(k.decode('ascii'), v.decode('ascii')) for k, v in self.raw]
    def __contains__(self, key):
        return key.lower() in self.d
    def items(self):
        return self.d.items()
    def __iter__(self):
        return iter(self.d)
    def __getitem__(self, key):
        return self.d[key.lower()]

class MockStreamResp:
    def __init__(self, status_code, headers, raw_headers, content):
        self.status_code = status_code
        self.headers = MockHeaders(raw_headers)
        self.content_bytes = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def aiter_bytes(self):
        yield self.content_bytes

# 1. Location Cardinality (Real Orchestration Path)
@pytest.mark.asyncio
@pytest.mark.parametrize("headers_raw,expected_error", [
    ([(b"location", b"http://ok.com")], None),
    ([], "redirect_missing_location"),
    ([(b"location", b"   ")], "redirect_missing_location"),
    ([(b"location", b"http://ok.com"), (b"location", b"http://ok2.com")], "redirect_ambiguous_location"),
    ([(b"location", b"http://ok.com/\x00/a")], "redirect_invalid_location"),
    ([(b"location", b"http://ok.com/\n/a")], "redirect_invalid_location"),
    ([(b"location", b"http://ok.com\r/a")], "redirect_invalid_location"),
    ([(b"location", b"http://ok.com"), (b"location", b"   ")], "redirect_ambiguous_location"),
    ([(b"location", b"http://ok.com/a,b")], None),
])
async def test_location_cardinality(worker, headers_raw, expected_error):
    client = httpx.AsyncClient()
    with patch.object(client, 'stream') as mock_stream:
        mock_stream.return_value = MockStreamResp(301, {}, headers_raw, b"")
        res = await worker.fetch_url_single(client, "http://test.com", 1024)
        if expected_error:
            assert res.error_code == expected_error, f"Expected {expected_error}, got {res.error_code}"
        else:
            assert res.error_code is None, f"Expected None, got {res.error_code}"
            assert res.redirect_url is not None

# 2. HTTP Sniffing Matrix
@pytest.mark.asyncio
@pytest.mark.parametrize("mode,max_bytes,status,headers,content,expected_error", [
    ("website", 1024, 200, {b"Content-Type": b"text/html"}, b"<html></html>", None),
    ("website", 1024, 200, {b"Content-Type": b"application/xml"}, b"<?xml>", "unsupported_content_type"),
    ("rss", 1024, 200, {b"Content-Type": b"application/xml"}, b"<?xml version='1.0'?><rss></rss>", None),
    ("rss", 1024, 200, {b"Content-Type": b"application/rss+xml"}, b"<rss></rss>", None),
    ("rss", 1024, 200, {b"Content-Type": b"application/atom+xml"}, b"<feed></feed>", None),
    ("rss", 1024, 200, {b"Content-Type": b"text/plain"}, b"<?xml version='1.0'?><rss>", None),
    ("rss", 1024, 200, {b"Content-Type": b"text/plain"}, b"<rss></rss>", None),
    ("rss", 1024, 200, {b"Content-Type": b"text/plain"}, b"<feed></feed>", None),
    ("rss", 1024, 200, {b"Content-Type": b"text/plain"}, b"Hello World", "unsupported_content_type"),
    ("rss", 1024, 200, {b"Content-Type": b"application/json"}, b"{}", "unsupported_content_type"),
    ("rss", 1024, 200, {b"Content-Type": b"application/xml", b"Content-Encoding": b"gzip"}, b"<?xml>", None),
    ("rss", 1024, 200, {b"Content-Type": b"application/xml", b"Content-Encoding": b"deflate"}, b"<?xml>", None),
    ("rss", 1024, 200, {b"Content-Type": b"application/xml", b"Content-Encoding": b"identity"}, b"<?xml>", None),
    ("rss", 1024, 200, {b"Content-Type": b"application/xml", b"Content-Encoding": b"br"}, b"<?xml>", "unsupported_content_encoding"),
    ("rss", 1024, 200, {b"Content-Type": b"application/xml", b"Content-Encoding": b"gzip, deflate"}, b"<?xml>", "multiple_content_encodings"),
    ("rss", 1024, 200, {b"Content-Type": b"application/xml", b"Content-Length": b"2048"}, b"<?xml>", "content_too_large"),
    ("rss", 10, 200, {b"Content-Type": b"application/xml"}, b"<?xml><toobig/>", "content_too_large"),
])
async def test_http_sniffing_matrix(worker, mode, max_bytes, status, headers, content, expected_error):
    client = httpx.AsyncClient()
    is_website = mode == "website"
    with patch.object(client, 'stream') as mock_stream:
        raw_headers = [(k, v) for k, v in headers.items()]
        mock_stream.return_value = MockStreamResp(status, headers, raw_headers, content)
        res = await worker.fetch_url_single(client, "https://test.com", max_bytes, website_mode=is_website)
        if expected_error:
            assert res.error_code == expected_error, f"Expected {expected_error}, got {res.error_code}"
        else:
            assert res.error_code is None, f"Expected None, got {res.error_code}"

# 3. Robots Orchestration tests
@pytest.mark.asyncio
@pytest.mark.parametrize("status,headers,content,expected_decision,expected_ttl", [
    (200, {}, b"User-agent: *\nAllow: /", True, 86400),
    (200, {}, b"User-agent: *\nDisallow: /", False, 86400),
    (401, {}, b"", False, 900),
    (403, {}, b"", False, 900),
    (429, {}, b"", False, 900),
    (429, {b"retry-after": b"3600"}, b"", False, 3600),
    (404, {}, b"", True, 86400),
    (410, {}, b"", True, 86400),
    (500, {}, b"", False, 900),
])
async def test_robots_orchestration(worker, status, headers, content, expected_decision, expected_ttl):
    client = httpx.AsyncClient()
    with patch.object(client, 'stream') as mock_stream:
        raw_headers = [(k, v) for k, v in headers.items()]
        mock_stream.return_value = MockStreamResp(status, headers, raw_headers, content)
        origin_url = "https://test.com/path"
        decision = await worker.check_robots(client, origin_url)
        assert decision == expected_decision
        cached = worker.robots_cache.check_hit(origin_url)
        assert cached is not None
        assert abs(cached['expires_at'] - time.monotonic() - expected_ttl) < 5

# 4. Redirect Loop Prevention in check_robots
@pytest.mark.asyncio
async def test_robots_redirect_loop(worker):
    client = httpx.AsyncClient()
    call_count = 0
    def stream_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MockStreamResp(301, {}, [(b"location", b"https://test.com/b")], b"")
        elif call_count == 2:
            return MockStreamResp(301, {}, [(b"location", b"https://test.com/robots.txt")], b"")
        elif call_count <= 5:
            return MockStreamResp(301, {}, [(b"location", b"https://test.com/b")], b"")
        return MockStreamResp(200, {}, [], b"")

    with patch.object(client, 'stream', side_effect=stream_side_effect):
        decision = await worker.check_robots(client, "https://test.com")
        assert decision == False
        cached = worker.robots_cache.check_hit("https://test.com")
        assert cached is not None
        assert cached['error_code'] == 'robots_error'

# 5. DB Limits and Worker Leases
@pytest.mark.asyncio
async def test_complete_source_poll_limits_and_allowlists(worker, fresh_db):
    source_id = fresh_db.add_source("rss", "ext1", "name", "https://test.com", priority=50, trust_rating=50)["id"]
    with fresh_db._get_connection() as conn:
        conn.execute("UPDATE sources SET resolution_status='resolved', is_active=1 WHERE id=?", (source_id,))
        conn.execute(
            "UPDATE source_poll_state SET lease_token = ?, lease_expires_at = datetime('now', '+1 hour') WHERE source_id = ?",
            ("src_token", source_id)
        )
        conn.execute(
            "INSERT INTO worker_leases (id, worker_id, lease_token, heartbeat_at, expires_at) VALUES (1, ?, ?, datetime('now'), datetime('now', '+1 hour')) ON CONFLICT(id) DO UPDATE SET worker_id=excluded.worker_id, lease_token=excluded.lease_token, heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at",
            (worker.worker_id, "test_token")
        )
        conn.commit()

    updates = {
        "last_http_status": 200,
        "next_poll_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    res = fresh_db.complete_source_poll("test_token", source_id, "src_token", "rss", "https://test.com", updates)
    assert res == True

    bad_updates = {"last_http_status": 200, "is_active": 0}
    try:
        fresh_db.complete_source_poll("test_token", source_id, "src_token", "rss", "https://test.com", bad_updates)
        assert False, "Should raise ValueError"
    except ValueError:
        pass

# 6. Global and Source Lease Race Conditions
@pytest.mark.asyncio
@pytest.mark.parametrize("worker_lease_diff,source_lease_diff,expected_success", [
    (60, 60, True),
    (-60, 60, False),
    (60, -60, False),
    (-60, -60, False),
])
async def test_lease_integrity(worker, fresh_db, worker_lease_diff, source_lease_diff, expected_success):
    url = f"https://test-{worker_lease_diff}-{source_lease_diff}.com"
    source_id = fresh_db.add_source("rss", "ext2", "name2", url, priority=50, trust_rating=50)["id"]

    with fresh_db._get_connection() as conn:
        conn.execute("UPDATE sources SET resolution_status='resolved', is_active=1 WHERE id=?", (source_id,))
        conn.execute(
            f"UPDATE source_poll_state SET lease_token = ?, lease_expires_at = datetime('now', '{source_lease_diff} seconds') WHERE source_id = ?",
            ("src_token", source_id)
        )
        conn.execute(
            f"INSERT INTO worker_leases (id, worker_id, lease_token, heartbeat_at, expires_at) VALUES (1, ?, ?, datetime('now'), datetime('now', '{worker_lease_diff} seconds')) ON CONFLICT(id) DO UPDATE SET worker_id=excluded.worker_id, lease_token=excluded.lease_token, heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at",
            (worker.worker_id, "test_token")
        )
        conn.commit()

    updates = {
        "last_http_status": 200,
        "next_poll_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    res = fresh_db.complete_source_poll("test_token", source_id, "src_token", "rss", url, updates)
    assert res == expected_success

# 7. HostLimiter
@pytest.mark.asyncio
async def test_host_limiter_real():
    limiter = HostLimiter()
    await limiter.acquire("https://test.com")
    key = "https://test.com:443"
    async with limiter._registry_lock:
        assert limiter._states[key].owner_count == 1

    await limiter.release(key)
    async with limiter._registry_lock:
        assert limiter._states.get(key) is None or limiter._states[key].owner_count == 0

# 8. SSRF checking via validate_url_and_dns
@pytest.mark.asyncio
async def test_ssrf_blocking(worker):
    client = httpx.AsyncClient()
    res = await worker.fetch_url_single(client, "http://localhost", 1024)
    assert res.error_code == "ssrf_blocked"
    res = await worker.fetch_url_single(client, "http://127.0.0.1", 1024)
    assert res.error_code == "ssrf_blocked"
    res = await worker.fetch_url_single(client, "http://169.254.169.254", 1024)
    assert res.error_code == "ssrf_blocked"
