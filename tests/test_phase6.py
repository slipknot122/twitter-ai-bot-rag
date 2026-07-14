import asyncio
import gzip
import zlib

import httpx
import pytest

from polling_listener import (
    FetchFailure,
    MAX_DECODED_BYTES,
    PollingWorker,
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
    def __init__(self, stream: TrackingStream, headers: list[tuple[bytes, bytes]]) -> None:
        self.stream = stream
        self.headers = headers
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, headers=self.headers, stream=self.stream, request=request)


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
