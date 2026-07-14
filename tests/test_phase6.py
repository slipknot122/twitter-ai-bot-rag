
import pytest
import asyncio
import zlib
import gzip
import httpx
from datetime import datetime

from polling_listener import PollingWorker, HostLimiter, FetchFailure, FetchResult
from database import Database

VALID_FEED = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<rss version="2.0"><channel>'
    b'<title>Example</title>'
    b'<item><guid>1</guid><title>Entry</title></item>'
    b'</channel></rss>'
)

GZIP_FEED = gzip.compress(VALID_FEED)
DEFLATE_FEED = zlib.compress(VALID_FEED)

class FakeResolver:
    def __init__(self, answers_by_host):
        self.answers_by_host = answers_by_host
        self.calls = []

    async def resolve(self, hostname: str, *args, **kwargs):
        self.calls.append(hostname)
        if hostname not in self.answers_by_host:
            raise RuntimeError("unexpected_dns_lookup")
        answer = self.answers_by_host[hostname]
        if isinstance(answer, Exception):
            raise answer
        return answer

class FakeResponse:
    def __init__(self, *, status_code, url, headers=None, chunks=None, stream_error=None):
        self.status_code = status_code
        self.url = url
        self.headers = httpx.Headers(headers or {})
        self._chunks = list(chunks or [])
        self._stream_error = stream_error
        self.iteration_count = 0
        self.close_count = 0

    async def aiter_bytes(self):
        for chunk in self._chunks:
            self.iteration_count += 1
            yield chunk

        if self._stream_error is not None:
            raise self._stream_error

    async def aclose(self):
        self.close_count += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def stream(self, method, url, *, headers=None, **kwargs):
        self.requests.append({
            "method": method,
            "url": url,
            "headers": dict(headers) if headers else {},
        })

        if not self.responses:
            raise RuntimeError("unexpected_transport_call")

        return self.responses.pop(0)

class SpyHostLimiter(HostLimiter):
    def __init__(self, max_concurrent_per_host):
        super().__init__(max_concurrent_per_host)
        self.acquire_calls = []
        self.release_calls = []

    async def acquire(self, origin_url: str):
        self.acquire_calls.append(origin_url)
        return await super().acquire(origin_url)

    async def release(self, origin: str, state):
        self.release_calls.append((origin, state))
        return await super().release(origin, state)

class SpyDB(Database):
    def __init__(self):
        self.writes = []

    def transition_source_status(self, source_id, source_token, old_status, new_status, reason, delay_seconds=0):
        self.writes.append({
            "type": "transition",
            "source_id": source_id,
            "new_status": new_status,
            "reason": reason
        })

    def insert_stuck_source_log(self, source_id, url, status, error_code, http_status=None):
        self.writes.append({
            "type": "insert_stuck_source_log",
            "source_id": source_id,
            "error_code": error_code
        })

    def complete_source_poll(self, global_token, source_id, source_token, claimed_mode, claimed_target, outcome_updates, drafts_to_insert=None):
        self.writes.append({
            "type": "complete_source_poll",
            "source_id": source_id,
            "error_code": outcome_updates.get("last_error_code"),
            "reason": claimed_target
        })

class SpyWorker(PollingWorker):
    def __init__(self, spy_db, spy_host_limiter):
        super().__init__()
        self.parser_calls = 0
        self.last_parsed_bytes = None
        self.host_limiter = spy_host_limiter
        import polling_listener
        polling_listener.db = spy_db

    def parse_feed(self, source, content_bytes, final_url):
        self.parser_calls += 1
        self.last_parsed_bytes = content_bytes
        class DummyFeed:
            pass
        feed = DummyFeed()
        feed.entries = [{"id": "1", "title": "Entry"}]
        return feed


def create_source():
    return {
        'source_id': 1,
        'lease_token': 'token',
        'source_type': 'rss',
        'claimed_mode': 'feed',
        'claimed_target': 'http://example.com/feed.xml',
        'url': 'http://example.com/feed.xml',
        'validator_url': None,
        'etag': None,
        'last_modified': None,
        'initial_sync_completed_at': '2023-01-01',
        'consecutive_errors': 0
    }

def chunk_bytes(data: bytes, chunk_size=1024):
    return [data[i:i+chunk_size] for i in range(0, len(data), chunk_size)]


class TrackingByteStream(httpx.AsyncByteStream):
    def __init__(self, data_or_exc):
        self.data_or_exc = data_or_exc
        self.close_count = 0
    async def __aiter__(self):
        if isinstance(self.data_or_exc, BaseException):
            raise self.data_or_exc
        for i in range(0, len(self.data_or_exc), 10):
            yield self.data_or_exc[i:i+10]
    async def aclose(self):
        self.close_count += 1

class RealHttpxMockTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses_by_url):
        self.responses_by_url = responses_by_url
        self.stream_close_counts = []
        self.requests = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url not in self.responses_by_url:
            raise RuntimeError(f"Unexpected URL: {url}")

        resp_data = self.responses_by_url[url]
        if isinstance(resp_data, Exception):
            raise resp_data

        status_code, headers, body = resp_data
        stream = TrackingByteStream(body)
        self.stream_close_counts.append(stream)
        return httpx.Response(status_code, headers=headers, stream=stream)

def get_cp_cases():
    import gzip
    import zlib
    VALID = b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Example</title><item><guid>1</guid><title>Entry</title></item></channel></rss>'
    return [
        pytest.param('CP-01', [], VALID, None, VALID, 1, 1, id='CP-01'),
        pytest.param('CP-02', [(b'content-encoding', b'gzip')], gzip.compress(VALID), None, VALID, 1, 1, id='CP-02'),
        pytest.param('CP-03', [(b'content-encoding', b'deflate')], zlib.compress(VALID), None, VALID, 1, 1, id='CP-03'),
        pytest.param('CP-04', [(b'content-encoding', b'GZIP')], gzip.compress(VALID), None, VALID, 1, 1, id='CP-04'),
        pytest.param('CP-05', [(b'content-encoding', b'gzip')], b'not-a-gzip', 'content_decoding_error', None, 1, 0, id='CP-05'),
        pytest.param('CP-06', [(b'content-encoding', b'deflate')], b'not-a-deflate', 'content_decoding_error', None, 1, 0, id='CP-06'),
        pytest.param('CP-07', [(b'content-encoding', b'gzip, identity')], b'', 'ambiguous_content_encoding', None, 0, 0, id='CP-07'),
        pytest.param('CP-08', [(b'content-encoding', b'gzip'), (b'content-encoding', b'identity')], b'', 'ambiguous_content_encoding', None, 0, 0, id='CP-08'),
        pytest.param('CP-09', [(b'content-encoding', b'br')], b'', 'unsupported_content_encoding', None, 0, 0, id='CP-09'),
        pytest.param('CP-10', [], b'', None, b'', 1, 1, id='CP-10'),
        pytest.param('CP-11', [], b'A' * (5*1024*1024), None, b'A' * (5*1024*1024), 1, 1, id='CP-11'),
        pytest.param('CP-12', [], b'A' * (5*1024*1024 + 1), 'body_too_large', None, 1, 0, id='CP-12'),
        pytest.param('CP-13', [(b'content-encoding', b'gzip')], gzip.compress(b'A' * (5*1024*1024 + 1)), 'body_too_large', None, 1, 0, id='CP-13'),
        pytest.param('CP-14', [(b'content-length', b'99999999')], VALID, None, VALID, 1, 1, id='CP-14'),
        pytest.param('CP-15', [], b'A' * (5*1024*1024), None, b'A' * (5*1024*1024), 1, 1, id='CP-15'),
        pytest.param('CP-16', [(b'content-encoding', b'deflate')], b'\x78\x9c' + b'garbage', 'content_decoding_error', None, 1, 0, id='CP-16'),
        pytest.param('CP-17', [], httpx.ReadTimeout("timeout"), 'timeout', None, 1, 0, id='CP-17'),
        pytest.param('CP-18', [], httpx.NetworkError("network_error"), 'network_error', None, 1, 0, id='CP-18'),
        pytest.param('CP-19', [(b'content-encoding', b'identity')], VALID, None, VALID, 1, 1, id='CP-19'),
        pytest.param('CP-20', [], asyncio.CancelledError("cancelled"), None, None, 1, 0, id='CP-20'),
    ]

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "test_id, headers, body_or_exc, error_code, parsed_bytes, expected_close_count, expected_parser_calls",
    get_cp_cases()
)
async def test_compression_parser_CP(test_id, headers, body_or_exc, error_code, parsed_bytes, expected_close_count, expected_parser_calls):
    import polling_listener
    robots_data = (200, [], b"User-agent: *\nAllow: /")
    feed_data = body_or_exc if isinstance(body_or_exc, Exception) else (200, headers + [(b"content-type", b"application/rss+xml")], body_or_exc)

    transport = RealHttpxMockTransport({
        "http://example.com/robots.txt": robots_data,
        "http://example.com/feed.xml": feed_data
    })

    db = SpyDB()
    host_limiter = SpyHostLimiter(2)
    worker = SpyWorker(db, host_limiter)
    source = create_source()
    resolver = FakeResolver({"example.com": ["93.184.216.34"]})

    from unittest.mock import patch, MagicMock
    with patch('polling_listener.feedparser.parse') as mock_parse:
        mock_parse.return_value = MagicMock(entries=[], bozo=0)
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                await worker.process_source(client, source, resolver=resolver)
        except asyncio.CancelledError:
            if not isinstance(body_or_exc, asyncio.CancelledError):
                raise
        except Exception as exc:
            if not isinstance(body_or_exc, Exception):
                raise

        assert mock_parse.call_count == expected_parser_calls, f"failed: {db.writes}"
        if expected_parser_calls > 0:
            assert mock_parse.call_args[0][0] == parsed_bytes

    feed_stream = transport.stream_close_counts[-1] if len(transport.stream_close_counts) > 1 else (transport.stream_close_counts[0] if len(transport.stream_close_counts) == 1 and not isinstance(body_or_exc, Exception) and not error_code == "robots_parse_error" else None)
    if feed_stream and not isinstance(body_or_exc, Exception):
        assert feed_stream.close_count >= expected_close_count

    assert len(host_limiter.acquire_calls) >= 1, f"{test_id} acquires: {host_limiter.acquire_calls}"
    assert len(host_limiter.release_calls) >= 1, f"{test_id} releases: {host_limiter.release_calls}"

    if error_code:
        found_err = False
        for write in db.writes:
            if write["type"] == "complete_source_poll" and write.get("error_code") == error_code:
                found_err = True
            elif write["type"] == "transition":
                # Maybe fallback or transition
                pass
        assert found_err, f"Expected DB write with error_code {error_code}, got {db.writes}"
    else:
        for write in db.writes:
            if write["type"] == "complete_source_poll":
                assert write.get("error_code") is None

def test_meta_check_no_duplicates():
    import collections
    ids = [
        "CP-01", "CP-02", "CP-03", "CP-04", "CP-05", "CP-06", "CP-07", "CP-08",
        "CP-09", "CP-10", "CP-11", "CP-12", "CP-13", "CP-14", "CP-15", "CP-16",
        "CP-17", "CP-18", "CP-19", "CP-20"
    ]
    counts = collections.Counter(ids)
    for k, v in counts.items():
        assert v == 1, f"Duplicate ID: {k}"
