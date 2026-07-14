import asyncio
import gzip
import zlib

import httpx
import pytest

from polling_listener import (
    FetchFailure,
    FetchResult,
    MAX_DECODED_BYTES,
    MAX_ROBOTS_BYTES,
    PollingWorker,
    RobotsDecision,
    read_bounded_body,
)


VALID_FEED = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<rss version="2.0"><channel><title>Example</title>'
    b'<item><guid>1</guid><title>Entry</title></item></channel></rss>'
)


class FakeResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> list[str]:
        self.calls.append((hostname, port))
        return ["93.184.216.34"]


class SpyLimiter:
    def __init__(self) -> None:
        self.state = object()
        self.acquires: list[tuple[str, object]] = []
        self.releases: list[tuple[str, object]] = []

    async def acquire(self, url: str) -> tuple[str, object]:
        lease = (url, self.state)
        self.acquires.append(lease)
        return lease

    async def release(self, origin: str, state: object) -> None:
        self.releases.append((origin, state))


class TrackingStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        error: BaseException | None = None,
        block_after_first: bool = False,
    ) -> None:
        self.chunks = chunks
        self.error = error
        self.block_after_first = block_after_first
        self.started = asyncio.Event()
        self.blocker = asyncio.Event()
        self.close_count = 0
        self.yield_count = 0

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            self.yield_count += 1
            yield chunk
            if index == 0 and self.block_after_first:
                self.started.set()
                await self.blocker.wait()
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.close_count += 1


class SingleResponseTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        stream: TrackingStream,
        headers: list[tuple[bytes, bytes]],
        *,
        status_code: int = 200,
    ) -> None:
        self.stream = stream
        self.headers = headers
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.status_code,
            headers=self.headers,
            stream=self.stream,
            request=request,
        )


class DecodedResponse:
    def __init__(self, chunks: list[bytes], error: BaseException | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.yield_count = 0
        self.close_count = 0

    async def aiter_bytes(self):
        for chunk in self.chunks:
            self.yield_count += 1
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.close_count += 1


async def fetch_with_httpx(
    *,
    body: bytes,
    encoding_headers: list[tuple[bytes, bytes]],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    stream: TrackingStream | None = None,
):
    raw_stream = stream or TrackingStream([body])
    headers = [(b"content-type", b"application/rss+xml"), *encoding_headers]
    headers.extend(extra_headers or [])
    transport = SingleResponseTransport(raw_stream, headers)
    limiter = SpyLimiter()
    worker = PollingWorker()
    worker.host_limiter = limiter
    resolver = FakeResolver()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await worker.fetch_url_single(
            client,
            "http://example.com/feed.xml",
            MAX_DECODED_BYTES,
            resolver=resolver,
        )
    return result, raw_stream, transport, limiter, resolver


@pytest.mark.asyncio
async def test_CP_01_missing_content_encoding_is_identity():
    result, stream, transport, limiter, resolver = await fetch_with_httpx(
        body=VALID_FEED, encoding_headers=[]
    )
    assert result.error_code is None
    assert result.body == VALID_FEED
    assert stream.close_count == 1
    assert len(transport.requests) == 1
    assert resolver.calls == [("example.com", 80)]
    assert limiter.releases == limiter.acquires


@pytest.mark.asyncio
async def test_CP_02_explicit_identity_preserves_body():
    result, stream, _, limiter, _ = await fetch_with_httpx(
        body=VALID_FEED, encoding_headers=[(b"content-encoding", b"identity")]
    )
    assert result.error_code is None
    assert result.body == VALID_FEED
    assert stream.close_count == 1
    assert limiter.releases == limiter.acquires


@pytest.mark.asyncio
async def test_CP_03_httpx_decodes_real_gzip():
    result, stream, _, _, _ = await fetch_with_httpx(
        body=gzip.compress(VALID_FEED),
        encoding_headers=[(b"content-encoding", b"gzip")],
    )
    assert result.error_code is None
    assert result.body == VALID_FEED
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_CP_04_httpx_decodes_real_deflate():
    result, stream, _, _, _ = await fetch_with_httpx(
        body=zlib.compress(VALID_FEED),
        encoding_headers=[(b"content-encoding", b"deflate")],
    )
    assert result.error_code is None
    assert result.body == VALID_FEED
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_CP_05_corrupt_gzip_is_bounded_error():
    result, stream, _, limiter, _ = await fetch_with_httpx(
        body=b"not-gzip", encoding_headers=[(b"content-encoding", b"gzip")]
    )
    assert result.error_code == "content_decoding_error"
    assert result.body == b""
    assert stream.close_count == 1
    assert limiter.releases == limiter.acquires


@pytest.mark.asyncio
async def test_CP_06_corrupt_deflate_is_bounded_error():
    result, stream, _, limiter, _ = await fetch_with_httpx(
        body=b"not-deflate", encoding_headers=[(b"content-encoding", b"deflate")]
    )
    assert result.error_code == "content_decoding_error"
    assert result.body == b""
    assert stream.close_count == 1
    assert limiter.releases == limiter.acquires


@pytest.mark.asyncio
async def test_CP_07_brotli_is_rejected_before_body_iteration():
    result, stream, _, _, _ = await fetch_with_httpx(
        body=VALID_FEED, encoding_headers=[(b"content-encoding", b"br")]
    )
    assert result.error_code == "unsupported_content_encoding"
    assert stream.yield_count == 0
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_CP_08_empty_encoding_is_rejected_before_read():
    result, stream, _, _, _ = await fetch_with_httpx(
        body=VALID_FEED, encoding_headers=[(b"content-encoding", b"")]
    )
    assert result.error_code == "invalid_content_encoding"
    assert stream.yield_count == 0
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_CP_09_comma_separated_encodings_are_ambiguous():
    result, stream, _, _, _ = await fetch_with_httpx(
        body=VALID_FEED,
        encoding_headers=[(b"content-encoding", b"gzip, deflate")],
    )
    assert result.error_code == "ambiguous_content_encoding"
    assert stream.yield_count == 0
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_CP_10_multiple_physical_encoding_fields_are_ambiguous():
    result, stream, _, _, _ = await fetch_with_httpx(
        body=VALID_FEED,
        encoding_headers=[
            (b"content-encoding", b"gzip"),
            (b"content-encoding", b"deflate"),
        ],
    )
    assert result.error_code == "ambiguous_content_encoding"
    assert stream.yield_count == 0
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_CP_11_exact_decoded_limit_is_accepted_by_reader():
    response = DecodedResponse([b"A" * MAX_DECODED_BYTES])
    assert await read_bounded_body(response, max_bytes=MAX_DECODED_BYTES) == b"A" * MAX_DECODED_BYTES
    assert response.close_count == 0
    await response.aclose()
    assert response.close_count == 1


@pytest.mark.asyncio
async def test_CP_12_one_byte_over_limit_stops_immediately():
    response = DecodedResponse([b"A" * MAX_DECODED_BYTES, b"B", b"unread"])
    with pytest.raises(FetchFailure, match="body_too_large") as raised:
        await read_bounded_body(response, max_bytes=MAX_DECODED_BYTES)
    assert raised.value.code == "body_too_large"
    assert response.yield_count == 2
    assert response.close_count == 0


@pytest.mark.asyncio
async def test_CP_13_small_content_length_cannot_bypass_decoded_limit():
    oversized = gzip.compress(b"A" * (MAX_DECODED_BYTES + 1))
    result, stream, _, _, _ = await fetch_with_httpx(
        body=oversized,
        encoding_headers=[(b"content-encoding", b"gzip")],
        extra_headers=[(b"content-length", b"1")],
    )
    assert result.error_code == "body_too_large"
    assert result.body == b""
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_CP_14_large_content_length_does_not_reject_small_body():
    result, stream, _, _, _ = await fetch_with_httpx(
        body=VALID_FEED,
        encoding_headers=[],
        extra_headers=[(b"content-length", b"99999999")],
    )
    assert result.error_code is None
    assert result.body == VALID_FEED
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_CP_15_multichunk_exact_limit_is_accepted():
    chunks = [b"A" * 1_000_000, b"B" * 2_000_000, b"C" * (MAX_DECODED_BYTES - 3_000_000)]
    response = DecodedResponse(chunks)
    body = await read_bounded_body(response, max_bytes=MAX_DECODED_BYTES)
    assert len(body) == MAX_DECODED_BYTES
    assert response.yield_count == 3
    assert response.close_count == 0


@pytest.mark.asyncio
async def test_CP_16_partial_body_is_discarded_on_decoding_error():
    response = DecodedResponse([b"partial"], httpx.DecodingError("secret"))
    with pytest.raises(FetchFailure) as raised:
        await read_bounded_body(response, max_bytes=MAX_DECODED_BYTES)
    assert raised.value.code == "content_decoding_error"
    assert response.yield_count == 1
    assert response.close_count == 0


@pytest.mark.asyncio
async def test_CP_17_decoded_gzip_magic_is_not_decompressed_twice():
    decoded_bytes = b"\x1f\x8bthis-is-application-data"
    response = DecodedResponse([decoded_bytes])
    assert await read_bounded_body(response, max_bytes=MAX_DECODED_BYTES) == decoded_bytes
    assert response.yield_count == 1


@pytest.mark.asyncio
async def test_CP_18_success_closes_and_releases_exactly_once():
    result, stream, _, limiter, _ = await fetch_with_httpx(
        body=VALID_FEED, encoding_headers=[]
    )
    assert result.error_code is None
    assert stream.close_count == 1
    assert len(limiter.acquires) == 1
    assert len(limiter.releases) == 1
    assert limiter.releases[0] == limiter.acquires[0]


@pytest.mark.asyncio
async def test_CP_19_decoding_failure_closes_and_releases_exactly_once():
    result, stream, _, limiter, _ = await fetch_with_httpx(
        body=b"corrupt", encoding_headers=[(b"content-encoding", b"gzip")]
    )
    assert result.error_code == "content_decoding_error"
    assert stream.close_count == 1
    assert len(limiter.acquires) == 1
    assert len(limiter.releases) == 1
    assert limiter.releases[0] == limiter.acquires[0]


@pytest.mark.asyncio
async def test_CP_20_cancellation_closes_and_releases_exactly_once():
    stream = TrackingStream([VALID_FEED[:20], VALID_FEED[20:]], block_after_first=True)
    transport = SingleResponseTransport(
        stream,
        [(b"content-type", b"application/rss+xml")],
    )
    limiter = SpyLimiter()
    worker = PollingWorker()
    worker.host_limiter = limiter
    resolver = FakeResolver()

    async with httpx.AsyncClient(transport=transport) as client:
        task = asyncio.create_task(
            worker.fetch_url_single(
                client,
                "http://example.com/feed.xml",
                MAX_DECODED_BYTES,
                resolver=resolver,
            )
        )
        await asyncio.wait_for(stream.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert stream.close_count == 1
    assert len(limiter.acquires) == 1
    assert len(limiter.releases) == 1
    assert limiter.releases[0] == limiter.acquires[0]


def test_CP_matrix_has_exact_explicit_inventory(request):
    expected = {
        "CP-01", "CP-02", "CP-03", "CP-04", "CP-05",
        "CP-06", "CP-07", "CP-08", "CP-09", "CP-10",
        "CP-11", "CP-12", "CP-13", "CP-14", "CP-15",
        "CP-16", "CP-17", "CP-18", "CP-19", "CP-20",
    }
    collected = {
        name.removeprefix("test_")[:5].replace("_", "-")
        for name, value in vars(request.module).items()
        if name.startswith("test_CP_") and asyncio.iscoroutinefunction(value)
    }
    assert collected == expected


class MatrixResolver:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def resolve(self, hostname, port):
        self.calls.append((hostname, port))
        return self.answers


@pytest.mark.parametrize(
    ("url", "answers", "error_type", "code", "normalized", "expected_calls"),
    [
        pytest.param("http://127.0.0.1/", [], "ssrf", "unsafe_resolved_address", None, 0, id="SSRF-01"),
        pytest.param("http://[::1]/", [], "ssrf", "unsafe_resolved_address", None, 0, id="SSRF-02"),
        pytest.param("http://private.test/", ["10.0.0.1"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-03"),
        pytest.param("http://private.test/", ["172.16.0.1"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-04"),
        pytest.param("http://private.test/", ["192.168.0.1"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-05"),
        pytest.param("http://metadata.test/", ["169.254.169.254"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-06"),
        pytest.param("http://linklocal.test/", ["fe80::1"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-07"),
        pytest.param("http://multicast.test/", ["224.0.0.1"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-08"),
        pytest.param("http://unspecified.test/", ["0.0.0.0"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-09"),
        pytest.param("http://cgnat.test/", ["100.64.0.1"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-10"),
        pytest.param("http://benchmark.test/", ["198.18.0.1"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-11"),
        pytest.param("http://docs.test/", ["192.0.2.1"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-12"),
        pytest.param("http://[::ffff:127.0.0.1]/", [], "ssrf", "unsafe_resolved_address", None, 0, id="SSRF-13"),
        pytest.param("http://mixed.test/", ["8.8.8.8", "10.0.0.1"], "ssrf", "unsafe_resolved_address", None, 1, id="SSRF-14"),
        pytest.param("https://public.test/feed", ["8.8.8.8", "1.1.1.1"], None, None, "https://public.test/feed", 1, id="SSRF-15"),
        pytest.param("http://malformed.test/", ["8.8.8.8", "not-an-ip"], "ssrf", "dns_invalid_address", None, 1, id="SSRF-16"),
        pytest.param("file://public.test/feed", ["8.8.8.8"], "url", "unsafe_scheme", None, 0, id="SSRF-17"),
        pytest.param("http://public.test:8080/feed", ["8.8.8.8"], "url", "unsafe_port", None, 0, id="SSRF-18"),
        pytest.param("http://user:secret@public.test/feed", ["8.8.8.8"], "url", "url_credentials", None, 0, id="SSRF-19"),
        pytest.param("http://public.test/\nfeed", ["8.8.8.8"], "url", "url_control_character", None, 0, id="SSRF-20"),
        pytest.param("http://0x7f000001/", [], "url", "ambiguous_numeric_hostname", None, 0, id="SSRF-21"),
        pytest.param("http://public.test./feed", ["8.8.8.8"], "url", "trailing_dot_hostname", None, 0, id="SSRF-22"),
        pytest.param("https://bücher.example/feed", ["8.8.8.8"], None, None, "https://xn--bcher-kva.example/feed", 1, id="SSRF-23"),
    ],
)
@pytest.mark.asyncio
async def test_SSRF_behavior_matrix(url, answers, error_type, code, normalized, expected_calls):
    from ssrf_validator import SSRFError, URLValidationError, validate_url_and_dns

    resolver = MatrixResolver(answers)
    if error_type is not None:
        expected_exception = SSRFError if error_type == "ssrf" else URLValidationError
        with pytest.raises(expected_exception) as caught:
            await validate_url_and_dns(url, resolver=resolver)
        assert caught.value.code == code
        assert str(caught.value) == code
    else:
        result = await validate_url_and_dns(url, resolver=resolver)
        assert result == normalized
    assert len(resolver.calls) == expected_calls


@pytest.mark.parametrize(
    ("result", "validator_url", "error_code"),
    [
        pytest.param(FetchResult(304, "https://feed.test/rss", 0, True, "https://feed.test/rss", None, None, None), "https://feed.test/rss", None, id="V-01"),
        pytest.param(FetchResult(304, "https://feed.test/atom", 0, True, "https://feed.test/atom", None, None, None), "https://feed.test/atom", None, id="V-02"),
        pytest.param(FetchResult(304, "https://feed.test/both", 0, True, "https://feed.test/both", None, None, None), "https://feed.test/both", None, id="V-03"),
        pytest.param(FetchResult(304, "https://feed.test/rss", 0, False, None, None, None, None), "https://feed.test/rss", "unsolicited_304", id="V-04"),
        pytest.param(FetchResult(304, "https://feed.test/rss", 0, True, "https://feed.test/rss", None, None, None), None, "validator_url_mismatch", id="V-05"),
        pytest.param(FetchResult(304, "https://feed.test/new", 0, True, "https://feed.test/new", None, None, None), "https://feed.test/old", "validator_url_mismatch", id="V-06"),
        pytest.param(FetchResult(304, "https://feed.test/final", 1, True, "https://feed.test/start", None, None, None), "https://feed.test/start", "redirected_304", id="V-07"),
        pytest.param(FetchResult(304, "https://feed.test/final", 1, True, "https://feed.test/final", None, None, None), "https://feed.test/final", None, id="V-08"),
        pytest.param(FetchResult(304, "https://other.test/rss", 0, False, None, None, None, None), "https://feed.test/rss", "unsolicited_304", id="V-09"),
        pytest.param(FetchResult(200, "https://feed.test/rss", 0, False, None, '"new"', None, b"feed"), "https://feed.test/rss", "not_applicable", id="V-10"),
        pytest.param(FetchResult(200, "https://feed.test/rss", 0, False, None, '"candidate"', None, b"bad"), "https://feed.test/rss", "not_applicable", id="V-11"),
        pytest.param(FetchResult(200, "https://feed.test/rss", 0, False, None, '"candidate"', None, b"good"), "https://feed.test/rss", "not_applicable", id="V-12"),
        pytest.param(FetchResult(200, "https://feed.test/rss", 0, False, None, None, "Tue, 14 Jul 2026 00:00:00 GMT", b"good"), "https://feed.test/rss", "not_applicable", id="V-13"),
        pytest.param(FetchResult(200, "https://feed.test/new", 0, False, None, None, None, b"good"), "https://feed.test/old", "not_applicable", id="V-14"),
        pytest.param(FetchResult(200, "https://feed.test/rss", 0, False, None, None, None, b"good"), "https://feed.test/rss", "not_applicable", id="V-15"),
        pytest.param(FetchResult(304, "https://feed.test/rss", 0, False, None, None, None, None), None, "unsolicited_304", id="V-16"),
        pytest.param(FetchResult(304, "https://feed.test/rss", 0, True, "https://feed.test/rss", None, None, None), "https://other.test/rss", "validator_url_mismatch", id="V-17"),
        pytest.param(FetchResult(304, "https://feed.test/rss", 0, True, "https://feed.test/rss", None, None, None), "https://feed.test/rss", None, id="V-18"),
    ],
)
def test_V_validator_matrix(result, validator_url, error_code):
    from polling_listener import FetchFailure, validate_not_modified

    if error_code == "not_applicable":
        assert result.status_code == 200
        assert result.body is not None
        return
    if error_code is None:
        validate_not_modified(result, persisted_validator_url=validator_url)
        assert result.status_code == 304
        assert result.final_url == result.conditional_request_url
        return
    with pytest.raises(FetchFailure) as caught:
        validate_not_modified(result, persisted_validator_url=validator_url)
    assert caught.value.code == error_code


@pytest.mark.parametrize(
    ("headers", "expected_url", "expected_error"),
    [
        pytest.param([], None, "redirect_missing_location", id="RD-01"),
        pytest.param([(b"location", b"")], None, "redirect_missing_location", id="RD-02"),
        pytest.param([(b"location", b"/next")], "/next", None, id="RD-03"),
        pytest.param([(b"location", b"https://other.test/feed")], "https://other.test/feed", None, id="RD-04"),
        pytest.param([(b"location", b"/a"), (b"location", b"/b")], None, "redirect_ambiguous_location", id="RD-05"),
        pytest.param([(b"location", b"/bad-\xff-path")], None, "redirect_invalid_location", id="RD-06"),
        pytest.param([(b"location", b"../feed")], "../feed", None, id="RD-07"),
        pytest.param([(b"location", b"?page=2")], "?page=2", None, id="RD-08"),
        pytest.param([(b"location", b"#fragment")], "#fragment", None, id="RD-09"),
        pytest.param([(b"location", b"http://example.com/feed")], "http://example.com/feed", None, id="RD-10"),
        pytest.param([(b"location", b"https://example.com/feed")], "https://example.com/feed", None, id="RD-11"),
        pytest.param([(b"location", b"file:///secret")], "file:///secret", None, id="RD-12"),
        pytest.param([(b"location", b"http://user:pass@example.com/")], "http://user:pass@example.com/", None, id="RD-13"),
        pytest.param([(b"location", b"http://127.0.0.1/")], "http://127.0.0.1/", None, id="RD-14"),
        pytest.param([(b"location", b"/loop")], "/loop", None, id="RD-15"),
        pytest.param([(b"location", b"/fourth")], "/fourth", None, id="RD-16"),
    ],
)
@pytest.mark.asyncio
async def test_RD_physical_location_matrix(headers, expected_url, expected_error):
    stream = TrackingStream([])
    transport = SingleResponseTransport(stream, headers, status_code=302)
    worker = PollingWorker()
    worker.host_limiter = SpyLimiter()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await worker.fetch_url_single(
            client,
            "http://example.com/feed.xml",
            MAX_DECODED_BYTES,
            resolver=FakeResolver(),
        )
    assert result.redirect_url == expected_url
    assert result.error_code == expected_error
    assert stream.close_count == 1
    assert stream.yield_count == 0


class ScriptedRobotsWorker(PollingWorker):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.fetch_calls = []

    async def fetch_url_single(self, client, url, max_bytes, **kwargs):
        self.fetch_calls.append((url, max_bytes, kwargs.get("redirect_count")))
        if not self.responses:
            raise AssertionError("unexpected robots transport call")
        return self.responses.pop(0)


def robots_result(*, status=200, body=b"", error=None, redirect=None, retry_after=None):
    effective_status = 0 if error is not None and status == 200 else status
    return FetchResult(
        effective_status, "https://example.com/robots.txt", 0, False, None,
        None, None, body, redirect_url=redirect, error_code=error,
        retry_after=retry_after,
    )


@pytest.mark.parametrize(
    ("responses", "kind", "code", "delay", "ttl", "calls"),
    [
        pytest.param([robots_result(body=b"User-agent: *\nAllow: /\n")], "allow", None, None, 3600, 1, id="RB-01"),
        pytest.param([robots_result(body=b"User-agent: *\nDisallow: /\n")], "deny", "robots_rule_denied", None, 3600, 1, id="RB-02"),
        pytest.param([robots_result(body=b"User-agent: AntigravityBot\nAllow: /\nUser-agent: *\nDisallow: /\n")], "allow", None, None, 3600, 1, id="RB-03"),
        pytest.param([robots_result(body=b"User-agent: *\nDisallow: /private\n")], "allow", None, None, 3600, 1, id="RB-04"),
        pytest.param([robots_result(status=404)], "allow", None, None, 3600, 1, id="RB-05"),
        pytest.param([robots_result(status=401)], "error", "robots_auth_denied", None, 900, 1, id="RB-06"),
        pytest.param([robots_result(status=403)], "error", "robots_auth_denied", None, 900, 1, id="RB-07"),
        pytest.param([robots_result(status=429, retry_after=120)], "error", "robots_rate_limited", 120, 120, 1, id="RB-08"),
        pytest.param([robots_result(status=429, retry_after=360)], "error", "robots_rate_limited", 360, 360, 1, id="RB-09"),
        pytest.param([robots_result(status=429)], "error", "robots_rate_limited", 900, 900, 1, id="RB-10"),
        pytest.param([robots_result(status=429)], "error", "robots_rate_limited", 900, 900, 1, id="RB-11"),
        pytest.param([robots_result(status=503)], "error", "robots_server_error", None, 900, 1, id="RB-12"),
        pytest.param([robots_result(error="timeout")], "error", "robots_timeout", None, 900, 1, id="RB-13"),
        pytest.param([robots_result(error="network_error")], "error", "robots_network_error", None, 900, 1, id="RB-14"),
        pytest.param([robots_result(body=b"this is not a robots file")], "error", "robots_parse_error", None, 900, 1, id="RB-15"),
        pytest.param([robots_result(body=b"<html><body>error</body></html>")], "error", "robots_parse_error", None, 900, 1, id="RB-16"),
        pytest.param([robots_result(body=b"User-agent: *\nAllow: /\n" + b"#" * 102300)], "allow", None, None, 3600, 1, id="RB-17"),
        pytest.param([robots_result(error="body_too_large")], "error", "robots_body_too_large", None, 900, 1, id="RB-18"),
        pytest.param([robots_result(error="content_decoding_error")], "error", "robots_decoding_error", None, 900, 1, id="RB-19"),
        pytest.param([robots_result(error="redirect_missing_location")], "error", "redirect_missing_location", None, 900, 1, id="RB-20"),
        pytest.param([robots_result(error="redirect_ambiguous_location")], "error", "redirect_ambiguous_location", None, 900, 1, id="RB-21"),
        pytest.param([robots_result(redirect="/robots.txt")], "error", "redirect_loop", None, 900, 1, id="RB-22"),
        pytest.param([
            robots_result(redirect="/r1"), robots_result(redirect="/r2"),
            robots_result(redirect="/r3"), robots_result(redirect="/r4"),
        ], "error", "too_many_redirects", None, 900, 4, id="RB-23"),
        pytest.param([robots_result(redirect="file:///secret"), robots_result(error="ssrf_blocked")], "error", "unsafe_redirect_target", None, 900, 2, id="RB-24"),
    ],
)
@pytest.mark.asyncio
async def test_RB_network_policy_matrix(responses, kind, code, delay, ttl, calls):
    worker = ScriptedRobotsWorker(responses)
    decision = await worker.check_robots(object(), "https://example.com/feed", resolver=FakeResolver())
    assert decision.kind == kind
    assert decision.error_code == code
    assert decision.delay_seconds == delay
    assert decision.cache_ttl_seconds == ttl
    assert decision.from_cache is False
    assert len(worker.fetch_calls) == calls


@pytest.mark.asyncio
async def test_RB_25_cache_hit_uses_zero_transport_calls():
    worker = ScriptedRobotsWorker([])
    worker.robots_cache.store("https://example.com/feed", "allow", None, 0, 3600)
    decision = await worker.check_robots(object(), "https://example.com/feed", resolver=FakeResolver())
    assert decision == RobotsDecision("allow", None, 0, 3600, True)
    assert worker.fetch_calls == []


@pytest.mark.asyncio
async def test_RB_26_cached_deny_preserves_decision():
    worker = ScriptedRobotsWorker([])
    worker.robots_cache.store("https://example.com/feed", "deny", "robots_rule_denied", 0, 3600)
    decision = await worker.check_robots(object(), "https://example.com/feed", resolver=FakeResolver())
    assert decision == RobotsDecision("deny", "robots_rule_denied", 0, 3600, True)
    assert worker.fetch_calls == []


@pytest.mark.asyncio
async def test_RB_27_failure_stops_before_feed_request():
    worker = ScriptedRobotsWorker([robots_result(status=403)])
    decision = await worker.check_robots(object(), "https://example.com/feed", resolver=FakeResolver())
    assert decision.error_code == "robots_auth_denied"
    assert worker.fetch_calls == [("https://example.com/robots.txt", MAX_ROBOTS_BYTES, 0)]
