import os
import sys
import uuid
import asyncio
import datetime
import hashlib
import json
import logging
import random
import urllib.parse
from html.parser import HTMLParser
from typing import Optional, List, Dict, Any, Tuple
import urllib.robotparser
import time
import re
import unicodedata
from enum import Enum
from dataclasses import dataclass

import httpx
import feedparser
from feedparser import CharacterEncodingOverride, CharacterEncodingUnknown, NonXMLContentType
from xml.sax import SAXException
from xml.parsers.expat import ExpatError

RECOVERABLE_BOZO_TYPES = (CharacterEncodingOverride, CharacterEncodingUnknown, NonXMLContentType)
FATAL_BOZO_TYPES = (SAXException, ExpatError)

from database import db
from config import settings
from ssrf_validator import validate_url_and_dns, SSRFError, URLValidationError

logger = logging.getLogger(__name__)

class FetchFailure(Exception):
    def __init__(self, code: str):
        self.code = code

def parse_content_encoding(headers) -> str:
    values = headers.get_list("content-encoding")
    if len(values) == 0:
        return "identity"
    if len(values) != 1:
        raise FetchFailure("ambiguous_content_encoding")
    value = values[0].strip().lower()
    if not value:
        raise FetchFailure("invalid_content_encoding")
    if "," in value:
        raise FetchFailure("ambiguous_content_encoding")
    if value not in {"identity", "gzip", "deflate"}:
        raise FetchFailure("unsupported_content_encoding")
    return value


def parse_redirect_location(raw_headers: list[tuple[bytes, bytes]]) -> str:
    values = [value.decode("utf-8", errors="strict") for key, value in raw_headers if key.lower() == b"location"]
    if not values or not values[0].strip():
        raise FetchFailure("redirect_missing_location")
    if len(values) != 1:
        raise FetchFailure("redirect_ambiguous_location")
    location = values[0].strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in location):
        raise FetchFailure("redirect_invalid_location")
    return location

async def read_bounded_body(response, *, max_bytes: int) -> bytes:
    """Read decoded response bytes without taking ownership of the response.

    The surrounding ``client.stream`` context owns closure. Keeping ownership in
    one place guarantees that success, decoding failures, size failures, and
    cancellation all close the transport stream exactly once.
    """
    chunks = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise FetchFailure("body_too_large")
            chunks.append(chunk)
        return b"".join(chunks)
    except httpx.DecodingError:
        raise FetchFailure("content_decoding_error") from None


class CancellationReason(str, Enum):
    GLOBAL_LEASE_LOST = "global_lease_lost"
    SOURCE_LEASE_LOST = "source_lease_lost"
    SHUTDOWN = "shutdown"
    TRANSIENT_INTERNAL = "transient_internal"

CANCELLATION_PRIORITY = {
    CancellationReason.GLOBAL_LEASE_LOST: 4,
    CancellationReason.SOURCE_LEASE_LOST: 3,
    CancellationReason.SHUTDOWN: 2,
    CancellationReason.TRANSIENT_INTERNAL: 1,
}

ROBOTS_PRODUCT_TOKEN = "AntigravityBot"

MAX_REDIRECTS = 3
MAX_DECODED_BYTES = 5 * 1024 * 1024  # 5 MiB
MAX_ENTRY_HTML_BYTES = 500 * 1024  # 500 KB max for any single entry description
MAX_ROBOTS_BYTES = 100 * 1024        # 100 KiB
USER_AGENT = "AntigravityBot/1.0"

GLOBAL_LEASE_DURATION_SEC = 45
HEARTBEAT_INTERVAL_SEC = 15
POLL_DEADLINE_SEC = 20
SOURCE_LEASE_SEC = 60
HOST_INTERVAL_SEC = 10
ROBOTS_TTL_SEC = 86400
ROBOTS_DENY_TTL_SEC = 900
HOST_CLEANUP_TTL_SEC = 300

class StartupConfigError(Exception):
    pass

def check_startup_config():
    if os.environ.get("RSS_EGRESS_SANDBOX_CONFIRMED") != "true":
        raise StartupConfigError("RSS_EGRESS_SANDBOX_CONFIRMED is not exact 'true'. Worker disabled for safety.")
    if HEARTBEAT_INTERVAL_SEC >= GLOBAL_LEASE_DURATION_SEC:
        raise StartupConfigError("Heartbeat interval must be strictly less than global lease duration")
    if POLL_DEADLINE_SEC >= SOURCE_LEASE_SEC:
        raise StartupConfigError("Poll deadline must be strictly less than source lease duration")

class HostStateMismatchError(Exception):
    pass

@dataclass
class HostState:
    request_lock: asyncio.Lock
    waiter_count: int = 0
    owner_count: int = 0
    next_allowed_monotonic: float = 0.0
    last_used_monotonic: float = 0.0

class HostLimiter:
    def __init__(self, interval: float = 10.0, idle_ttl: float = 300.0) -> None:
        self._registry_lock = asyncio.Lock()
        self._states: dict[str, HostState] = {}
        self._interval = interval
        self._idle_ttl = idle_ttl

    async def acquire(self, url: str) -> tuple[str, HostState]:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname if parsed.hostname else ""
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        key = f"{parsed.scheme}://{host}:{port}"

        async with self._registry_lock:
            if key not in self._states:
                self._states[key] = HostState(request_lock=asyncio.Lock())
            state = self._states[key]
            state.waiter_count += 1
            request_lock = state.request_lock

        try:
            await request_lock.acquire()
            try:
                now = time.monotonic()
                elapsed = now - state.last_used_monotonic
                if elapsed < self._interval:
                    await asyncio.sleep(self._interval - elapsed)

                async with self._registry_lock:
                    state.owner_count += 1
                return key, state
            except asyncio.CancelledError:
                request_lock.release()
                raise
        finally:
            async with self._registry_lock:
                state.waiter_count -= 1

    async def release(self, key: str, state: HostState) -> None:
        async with self._registry_lock:
            current = self._states.get(key)
            if current is not state:
                raise HostStateMismatchError("Bounded internal error: HostLimiter stale-state mismatch")
            if state.owner_count != 1 or not state.request_lock.locked():
                raise HostStateMismatchError("Bounded internal error: HostLimiter invalid release")

            state.last_used_monotonic = time.monotonic()
            state.owner_count = 0
            state.request_lock.release()

    async def gc_idle(self) -> None:
        now = time.monotonic()
        async with self._registry_lock:
            idle_keys = [
                k for k, s in self._states.items()
                if s.waiter_count == 0 and s.owner_count == 0
                and (now - s.last_used_monotonic) > self._idle_ttl
            ]
            for k in idle_keys:
                del self._states[k]

class RobotsCache:
    def __init__(self):
        self.cache = {}

    def _get_key(self, url: str):
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname if parsed.hostname else ""
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return f"{parsed.scheme}://{host}:{port}|{USER_AGENT}"

    def check_hit(self, url: str) -> Optional[dict]:
        now = time.monotonic()
        key = self._get_key(url)
        if key in self.cache:
            entry = self.cache[key]
            if now < entry['expires_at']:
                return entry
        return None

    def store(self, url: str, decision: str, error_code: str, delay: int, ttl: int):
        now = time.monotonic()
        key = self._get_key(url)
        self.cache[key] = {
            'decision': decision,
            'error_code': error_code,
            'delay': delay,
            'ttl': ttl,
            'expires_at': now + ttl,
        }

SUPPRESSED_TAGS = {"script", "style", "noscript"}

class SafeHTMLParser(HTMLParser):
    def __init__(self, max_len: int):
        super().__init__()
        self.max_len = max_len
        self.total_len = 0
        self.parts = []
        self._suppressed_stack = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.casefold()
        if tag_lower in SUPPRESSED_TAGS:
            self._suppressed_stack.append(tag_lower)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.casefold()
        if tag_lower in SUPPRESSED_TAGS:
            # Pop up to matching tag to handle malformed/missing closures
            for i in range(len(self._suppressed_stack) - 1, -1, -1):
                if self._suppressed_stack[i] == tag_lower:
                    del self._suppressed_stack[i:]
                    break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._suppressed_stack:
            return
        remaining = self.max_len - self.total_len
        if remaining <= 0:
            return
        # Keep spacing safe by replacing newlines/tabs with spaces
        cleaned = "".join(" " if c in ('\t', '\n', '\r') else c for c in data if ord(c) >= 32 or c in ('\t', '\n', '\r'))
        if not cleaned:
            return
        bounded = cleaned[:remaining]
        if bounded:
            self.parts.append(bounded)
            self.total_len += len(bounded)

    def get_text(self) -> str:
        raw = " ".join(self.parts)
        norm = unicodedata.normalize('NFC', raw)
        return re.sub(r'\s+', ' ', norm).strip()[:self.max_len]

class AutodiscoveryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.candidates = []
        self.base_href = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag.lower() == 'base' and 'href' in attrs_dict:
            if not self.base_href:
                self.base_href = attrs_dict['href']
        if tag.lower() == 'link':
            rel_str = attrs_dict.get('rel', '').lower()
            rel = set(rel_str.split())
            if 'alternate' in rel:
                mime = attrs_dict.get('type', '').lower().split(';')[0].strip()
                if mime in ('application/rss+xml', 'application/atom+xml'):
                    self.candidates.append({
                        'href': attrs_dict.get('href'),
                        'type': mime,
                        'title': attrs_dict.get('title', '')
                    })

def canonicalize_entry_url(url: str) -> str:
    """Return a stable HTTP(S) identity URL without weakening URL validation."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("entry_url_invalid")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("entry_url_credentials")

    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("entry_url_invalid_port") from exc
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host if port is None or default_port else f"{host}:{port}"

    path = parsed.path or "/"
    trailing_slash = path.endswith("/")
    segments = []
    for segment in path.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    path = "/" + "/".join(segments)
    if trailing_slash and path != "/":
        path += "/"

    blocked = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = sorted(
        (key, value)
        for key, value in query
        if not key.lower().startswith("utm_") and key.lower() not in blocked
    )
    return urllib.parse.urlunsplit(
        (scheme, netloc, path, urllib.parse.urlencode(filtered, doseq=True), "")
    )


def strip_tracking_params(url: str) -> str:
    return canonicalize_entry_url(url)

def compute_entry_identity(entry: dict, fallback_feed_url: str) -> str:
    if 'id' in entry and entry['id']:
        guid = str(entry['id'])[:2048]
        norm = unicodedata.normalize('NFC', guid)
        digest = hashlib.sha256(norm.encode('utf-8')).hexdigest()
        return f"guid:{digest}"

    if 'link' in entry and entry['link']:
        link = str(entry['link'])[:2048]
        base = entry.get('base') or fallback_feed_url
        try:
            absolute = urllib.parse.urljoin(base, link)
            clean = strip_tracking_params(absolute)
            digest = hashlib.sha256(clean.encode('utf-8')).hexdigest()
            return f"link:{digest}"
        except:
            pass

    title = unicodedata.normalize('NFC', str(entry.get('title', ''))[:200])
    published = str(entry.get('published', ''))[:100]
    content_raw = unicodedata.normalize('NFC', str(entry.get('description', ''))[:3000])
    text_to_hash = f"{title}|{published}|{content_raw}"
    digest = hashlib.sha256(text_to_hash.encode('utf-8')).hexdigest()
    return f"hash:{digest}"

def build_entry_drafts(entries, feed_url: str, *, is_initial: bool, now: datetime.datetime) -> list[dict]:
    """Apply deterministic feed ordering, age, timestamp, identity, and batch rules."""
    limit = 10 if is_initial else 50
    cutoff = now - datetime.timedelta(hours=72)
    drafts = []
    seen_identities = set()

    for entry in entries:
        if len(drafts) >= limit:
            break
        entry_id = compute_entry_identity(entry, feed_url)
        if entry_id in seen_identities:
            continue
        seen_identities.add(entry_id)

        published_parsed = entry.get("published_parsed")
        published = None
        if published_parsed:
            published = datetime.datetime(*published_parsed[:6], tzinfo=datetime.timezone.utc)
            if is_initial and published < cutoff:
                continue
            published = min(published, now)

        updated_parsed = entry.get("updated_parsed")
        updated = None
        if updated_parsed:
            updated = datetime.datetime(*updated_parsed[:6], tzinfo=datetime.timezone.utc)
            updated = min(updated, now)

        html_parser = SafeHTMLParser(max_len=MAX_ENTRY_HTML_BYTES)
        html_parser.feed(f"{entry.get('title', '')} {entry.get('description', '')}")
        drafts.append({
            "source_item_id": entry_id,
            "original_text": html_parser.get_text(),
            "source_published_at": published.isoformat() if published else None,
            "source_updated_at": updated.isoformat() if updated else None,
        })
    return drafts


def parse_feed_document(body: bytes):
    """Parse a feed with an explicit bozo and XML entity policy."""
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise FetchFailure("feed_parse_error")

    feed = feedparser.parse(body)
    if feed.bozo:
        exception = feed.bozo_exception
        recoverable = isinstance(exception, RECOVERABLE_BOZO_TYPES)
        if not recoverable or not feed.entries:
            raise FetchFailure("feed_parse_error")
    return feed


def parse_retry_after(header_val: str) -> Optional[int]:
    try:
        return max(10, min(86400, int(header_val)))
    except ValueError:
        pass
    try:
        import email.utils
        import calendar
        parsed_tuple = email.utils.parsedate(header_val)
        if parsed_tuple:
            ts = calendar.timegm(parsed_tuple)
            now = datetime.datetime.now(datetime.timezone.utc).timestamp()
            delay = int(ts - now)
            return max(10, min(86400, delay))
    except:
        pass
    return None

@dataclass(frozen=True)
class RobotsDecision:
    kind: str # Literal["allow", "deny", "error"]
    error_code: str | None
    delay_seconds: int | None
    cache_ttl_seconds: int
    from_cache: bool

from dataclasses import dataclass

@dataclass(frozen=True)
class FetchResult:
    status_code: int
    final_url: str
    redirect_count: int
    conditional_headers_sent: bool
    conditional_request_url: str | None
    candidate_etag: str | None
    candidate_last_modified: str | None
    body: bytes | None
    redirect_url: str | None = None
    error_code: str | None = None
    retry_after: int | None = None


def validate_not_modified(result: FetchResult, *, persisted_validator_url: str | None) -> None:
    """Validate that a 304 belongs to the terminal conditional request."""
    if not result.conditional_headers_sent or result.conditional_request_url is None:
        raise FetchFailure("unsolicited_304")
    if persisted_validator_url is None:
        raise FetchFailure("validator_url_mismatch")
    if result.conditional_request_url != persisted_validator_url:
        raise FetchFailure("validator_url_mismatch")
    if result.final_url != result.conditional_request_url:
        raise FetchFailure("redirected_304")


class PollingWorker:
    def __init__(self):
        self.worker_id = str(uuid.uuid4())
        self.global_token = None
        self.cancellation_event = asyncio.Event()
        self.cancellation_reason: Optional[CancellationReason] = None
        self.host_limiter = HostLimiter(HOST_INTERVAL_SEC, HOST_CLEANUP_TTL_SEC)
        self.robots_cache = RobotsCache()
        self.active_poll_task = None
        self._rng = random.Random()

    async def fetch_url_single(self, client: httpx.AsyncClient, url: str, max_bytes: int, etag=None, last_modified=None, website_mode=False, resolver=None, redirect_count: int = 0) -> FetchResult:
        final_url = url
        status_code = None
        content_bytes = b""
        res_etag = None
        res_last_modified = None
        redirect_url = None
        error_code = None
        retry_after = None
        conditional_headers_sent = False
        conditional_request_url = None

        try:
            from ssrf_validator import validate_url_and_dns, SSRFError, URLValidationError, system_resolver
            final_url = await validate_url_and_dns(url, resolver=resolver or system_resolver)
        except (SSRFError, URLValidationError):
            return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, "ssrf_blocked", retry_after)

        host_key, host_state = await self.host_limiter.acquire(final_url)
        try:
            headers = {"User-Agent": USER_AGENT}
            if etag or last_modified:
                conditional_headers_sent = True
                conditional_request_url = final_url
            if etag: headers["If-None-Match"] = etag
            if last_modified: headers["If-Modified-Since"] = last_modified

            try:
                async with client.stream('GET', final_url, headers=headers) as resp:
                    status_code = resp.status_code
                    if resp.status_code in (301, 302, 303, 307, 308):
                        try:
                            redirect_url = parse_redirect_location(resp.headers.raw)
                        except (FetchFailure, UnicodeDecodeError) as failure:
                            error_code = failure.code if isinstance(failure, FetchFailure) else "redirect_invalid_location"
                        return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, error_code, retry_after)

                    res_etag = resp.headers.get("ETag")
                    res_last_modified = resp.headers.get("Last-Modified")
                    if "retry-after" in resp.headers:
                        retry_after = parse_retry_after(resp.headers["retry-after"])

                    if resp.status_code == 304:
                        return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, error_code, retry_after)

                    if resp.status_code >= 400:
                        if resp.status_code in (429, 503):
                            error_code = f"http_{resp.status_code}"
                        elif resp.status_code in (400, 401, 403, 404, 410):
                            error_code = "http_4xx"
                        else:
                            error_code = f"http_{resp.status_code}"
                        return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, error_code, retry_after)

                    content_type = resp.headers.get("Content-Type", "").lower()
                    if website_mode:
                        if "text/html" not in content_type:
                            error_code = "unsupported_content_type"
                            return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, error_code, retry_after)
                    else:
                        if max_bytes != MAX_ROBOTS_BYTES:
                            if "application/xml" not in content_type and "application/rss+xml" not in content_type and "application/atom+xml" not in content_type:
                                if "text/plain" not in content_type:
                                    error_code = "unsupported_content_type"
                                    return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, error_code, retry_after)

                    try:
                        parse_content_encoding(resp.headers)
                    except FetchFailure as e:
                        return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, e.code, retry_after)

                    try:
                        content_bytes = await read_bounded_body(resp, max_bytes=max_bytes)
                    except FetchFailure as e:
                        return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, e.code, retry_after)

                    if not website_mode and max_bytes != MAX_ROBOTS_BYTES and "text/plain" in content_type:
                        sniff = content_bytes[:50].decode('utf-8', errors='ignore').strip()
                        if not sniff.startswith("<?xml") and not sniff.startswith("<rss") and not sniff.startswith("<feed"):
                            error_code = "unsupported_content_type"
                            return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, error_code, retry_after)

                    # content_bytes is already assigned
            except httpx.TimeoutException:
                error_code = "timeout"
            except httpx.DecodingError:
                error_code = "content_decoding_error"
            except httpx.RequestError:
                error_code = "network_error"
        finally:
            await self.host_limiter.release(host_key, host_state)

        return FetchResult(status_code, final_url, redirect_count, conditional_headers_sent, conditional_request_url, res_etag, res_last_modified, content_bytes, redirect_url, error_code, retry_after)

    async def check_robots(self, client: httpx.AsyncClient, origin_url: str, resolver=None) -> RobotsDecision:
        cached = self.robots_cache.check_hit(origin_url)
        if cached is not None:
            return RobotsDecision(
                kind=cached['decision'],
                error_code=cached['error_code'],
                delay_seconds=cached['delay'],
                cache_ttl_seconds=cached['ttl'],
                from_cache=True
            )

        try:
            parsed = urllib.parse.urlparse(origin_url)
            robots_url = f"{parsed.scheme}://{parsed.hostname}{(':' + str(parsed.port)) if parsed.port else ''}/robots.txt"
        except:
            return RobotsDecision("error", "robots_parse_error", None, 900, False)

        redirect_count = 0
        url_to_fetch = robots_url
        visited = set()
        visited.add(url_to_fetch)
        res = None

        while redirect_count <= MAX_REDIRECTS:
            res = await self.fetch_url_single(client, url_to_fetch, MAX_ROBOTS_BYTES, website_mode=False, resolver=resolver, redirect_count=redirect_count)
            if self.cancellation_event.is_set():
                return RobotsDecision("error", "cancelled", None, 900, False)

            if res.redirect_url:
                try:
                    next_url = urllib.parse.urljoin(url_to_fetch, res.redirect_url)
                    parsed_next = urllib.parse.urlparse(next_url)
                    next_url = urllib.parse.urlunparse((parsed_next.scheme.lower(), parsed_next.netloc.lower(), parsed_next.path, parsed_next.params, parsed_next.query, parsed_next.fragment))
                except:
                    decision = RobotsDecision("error", "unsafe_redirect_target", None, 900, False)
                    self.robots_cache.store(origin_url, decision.kind, decision.error_code, decision.delay_seconds or 0, decision.cache_ttl_seconds)
                    return decision
                if next_url in visited:
                    decision = RobotsDecision("error", "redirect_loop", None, 900, False)
                    self.robots_cache.store(origin_url, decision.kind, decision.error_code, decision.delay_seconds or 0, decision.cache_ttl_seconds)
                    return decision
                visited.add(next_url)
                url_to_fetch = next_url
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS:
                    decision = RobotsDecision("error", "too_many_redirects", None, 900, False)
                    self.robots_cache.store(origin_url, decision.kind, decision.error_code, decision.delay_seconds or 0, decision.cache_ttl_seconds)
                    return decision
                continue
            break

        kind = 'error'
        error_code = None
        ttl = 900
        delay = None

        if res.status_code in (404, 410):
            kind = 'allow'
            ttl = 3600
        elif res.status_code == 200:
            try:
                text = res.body.decode('utf-8')
                lines = text.splitlines()
                has_valid_directive = False
                has_content = False
                is_html = '<html' in text.lower() or '<body' in text.lower() or text.strip().startswith('<!doctype') or text.strip().startswith('<')

                if not is_html:
                    for line in lines:
                        line = line.strip()
                        if not line or line.startswith('#'): continue
                        has_content = True
                        if ':' in line:
                            k = line.split(':', 1)[0].strip().lower()
                            if k in ('user-agent', 'allow', 'disallow', 'sitemap', 'crawl-delay', 'host'):
                                has_valid_directive = True
                                break

                if is_html or (has_content and not has_valid_directive):
                    kind = 'error'
                    error_code = 'robots_parse_error'
                    ttl = 900
                else:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(lines)
                    if rp.can_fetch(ROBOTS_PRODUCT_TOKEN, origin_url):
                        kind = 'allow'
                    else:
                        kind = 'deny'
                        error_code = 'robots_rule_denied'
                    ttl = 3600
            except UnicodeDecodeError:
                kind = 'error'
                error_code = 'robots_parse_error'
                ttl = 900
        elif res.status_code in (401, 403):
            kind = 'error'
            error_code = 'robots_auth_denied'
            ttl = 900
        elif res.status_code == 429:
            kind = 'error'
            error_code = 'robots_rate_limited'
            if res.retry_after is not None:
                delay = res.retry_after
                ttl = res.retry_after
            else:
                delay = 900
                ttl = 900
        elif res.status_code and res.status_code >= 500:
            kind = 'error'
            error_code = 'robots_server_error'
            ttl = 900
        elif res.error_code:
            kind = 'error'
            ttl = 900
            if res.error_code == 'body_too_large':
                error_code = 'robots_body_too_large'
            elif res.error_code == 'content_decoding_error':
                error_code = 'robots_decoding_error'
            elif res.error_code in ('timeout', 'network_error'):
                error_code = f"robots_{res.error_code}"
            elif res.error_code.startswith("redirect_"):
                error_code = res.error_code
            elif res.error_code == 'too_many_redirects':
                error_code = 'too_many_redirects'
            elif res.error_code == 'ssrf_blocked' and redirect_count > 0:
                error_code = 'unsafe_redirect_target'
            else:
                error_code = 'robots_error'
        else:
            kind = 'error'
            error_code = 'robots_error'
            ttl = 900

        self.robots_cache.store(origin_url, kind, error_code, delay or 0, ttl)
        return RobotsDecision(kind, error_code, delay, ttl, False)
    def get_backoff_delay(self, previous_errors: int, retry_after: int = None, is_4xx: bool = False) -> int:
        if is_4xx:
            return 86400
        if retry_after:
            return max(10, min(86400, retry_after))
        errors = previous_errors + 1
        base = min(86400, 900 * (2 ** (errors - 1)))
        delay = max(10, min(86400, base * self._rng.uniform(0.9, 1.1)))
        return int(delay)

    async def process_source(self, client: httpx.AsyncClient, source: dict, resolver=None):
        from ssrf_validator import system_resolver
        resolver = resolver or system_resolver

        source_id = source['source_id']
        source_token = source['lease_token']
        source_type = source['source_type']
        claimed_mode = source['claimed_mode']
        claimed_target = source['claimed_target']

        url_to_fetch = claimed_target
        website_mode = claimed_mode == 'website discovery'

        robots_decision = await self.check_robots(client, url_to_fetch, resolver=resolver)
        if robots_decision.kind != 'allow':
            self.fail_poll(source, robots_decision.error_code or 'blocked_by_robots', robots_decision.delay_seconds)
            return

        redirect_count = 0
        visited = set()
        visited.add(url_to_fetch)
        res = None
        current_validator_url = source.get('validator_url')
        current_etag = source.get('etag')
        current_last_modified = source.get('last_modified')

        while redirect_count <= MAX_REDIRECTS:
            req_etag = current_etag if current_validator_url == url_to_fetch else None
            req_lm = current_last_modified if current_validator_url == url_to_fetch else None

            res = await self.fetch_url_single(
                client, url_to_fetch, MAX_DECODED_BYTES,
                etag=req_etag, last_modified=req_lm, website_mode=website_mode, resolver=resolver, redirect_count=redirect_count
            )

            if self.cancellation_event.is_set():
                self.requeue_poll(source, self.cancellation_reason or CancellationReason.TRANSIENT_INTERNAL)
                return

            if res.redirect_url:
                try:
                    next_url = urllib.parse.urljoin(url_to_fetch, res.redirect_url)
                    parsed_next = urllib.parse.urlparse(next_url)
                    next_url = urllib.parse.urlunparse((parsed_next.scheme.lower(), parsed_next.netloc.lower(), parsed_next.path, parsed_next.params, parsed_next.query, parsed_next.fragment))
                except:
                    self.fail_poll(source, "invalid_redirect", res.retry_after, res.status_code)
                    return

                if next_url in visited:
                    self.fail_poll(source, "redirect_loop", res.retry_after, res.status_code)
                    return
                visited.add(next_url)

                prev_origin = urllib.parse.urlparse(url_to_fetch).netloc
                next_origin = urllib.parse.urlparse(next_url).netloc
                if prev_origin != next_origin:
                    robots_decision2 = await self.check_robots(client, next_url, resolver=resolver)
                    if robots_decision2.kind != 'allow':
                        self.fail_poll(source, robots_decision2.error_code or 'blocked_by_robots', robots_decision2.delay_seconds, res.status_code)
                        return

                url_to_fetch = next_url
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS:
                    self.fail_poll(source, "too_many_redirects", res.retry_after, res.status_code)
                    return
                continue
            break

        if res.error_code:
            self.fail_poll(source, res.error_code, res.retry_after, res.status_code)
            return

        if res.status_code == 304:
            try:
                validate_not_modified(res, persisted_validator_url=current_validator_url)
            except FetchFailure as failure:
                self.fail_poll(source, failure.code, None, res.status_code)
                return
            self.succeed_poll(source, current_etag, current_last_modified, res.final_url, res.status_code, [])
            return

        if website_mode:
            html = res.body.decode('utf-8', errors='ignore')
            parser = AutodiscoveryParser()
            parser.feed(html)

            candidates = parser.candidates
            best = None
            if candidates:
                for mime_pref in ('application/atom+xml', 'application/rss+xml'):
                    matches = [c for c in candidates if c['type'] == mime_pref]
                    if matches:
                        best = matches[0]['href']
                        break
                if not best:
                    best = candidates[0]['href']

            if best:
                try:
                    base = parser.base_href or res.final_url
                    try:
                        from ssrf_validator import validate_url_and_dns
                        await validate_url_and_dns(base, resolver=resolver)
                    except:
                        base = res.final_url

                    resolved = urllib.parse.urljoin(base, best)
                    await validate_url_and_dns(resolved, resolver=resolver)

                    db.complete_source_poll(
                        self.global_token, source_id, source_token, claimed_mode, claimed_target,
                        {
                            "resolved_feed_url": resolved,
                            "validator_url": None,
                            "etag": None,
                            "last_modified": None,
                            "collector_status": "queued",
                            "next_poll_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "last_error_code": None,
                            "consecutive_errors": 0,
                            "last_http_status": res.status_code
                        }
                    )
                except Exception:
                    self.fail_poll(source, "invalid_feed_url", res.retry_after, res.status_code)
            else:
                next_poll = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
                db.complete_source_poll(
                    self.global_token, source_id, source_token, claimed_mode, claimed_target,
                    {
                        "collector_status": "unsupported",
                        "last_error_code": "unsupported_type",
                        "next_poll_at": next_poll.isoformat(),
                        "validator_url": None,
                        "etag": None,
                        "last_modified": None,
                        "last_http_status": res.status_code
                    }
                )
            return

        # Feed Mode
        try:
            feed = parse_feed_document(res.body)
            if not feed.entries:
                self.succeed_poll(source, res.candidate_etag, res.candidate_last_modified, res.final_url, res.status_code, [], is_empty=True)
                return
        except FetchFailure:
            self.fail_poll(source, "feed_parse_error", http_status=res.status_code)
            return

        is_initial = source['initial_sync_completed_at'] is None
        drafts = build_entry_drafts(
            feed.entries,
            res.final_url,
            is_initial=is_initial,
            now=datetime.datetime.now(datetime.timezone.utc),
        )

        self.succeed_poll(source, res.candidate_etag, res.candidate_last_modified, res.final_url, res.status_code, drafts, is_initial=is_initial)
    def fail_poll(self, source: dict, error_code: str, retry_after: int = None, http_status: int = None):
        delay = self.get_backoff_delay(source['consecutive_errors'], retry_after, is_4xx=(error_code=='http_4xx'))
        next_poll = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delay)

        db.complete_source_poll(
            self.global_token, source['source_id'], source['lease_token'], source['claimed_mode'], source['claimed_target'],
            {
                "collector_status": error_code if error_code == "blocked_by_robots" else "backoff",
                "consecutive_errors": source['consecutive_errors'] + 1,
                "last_error_code": error_code,
                "last_attempt_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "next_poll_at": next_poll.isoformat(),
                "last_http_status": http_status
            }
        )

    def succeed_poll(self, source: dict, etag: str, last_modified: str, validator_url: str, status_code: int, drafts: list, is_initial: bool = False, is_empty: bool = False):
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        interval_minutes = source.get('poll_interval_minutes', 30)
        try:
            interval_minutes = max(5, min(1440, int(interval_minutes)))
        except (TypeError, ValueError):
            interval_minutes = 30

        next_poll = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=interval_minutes)

        updates = {
            "collector_status": "healthy",
            "consecutive_errors": 0,
            "last_error_code": None,
            "last_attempt_at": now_str,
            "last_success_at": now_str,
            "next_poll_at": next_poll.isoformat(),
            "validator_url": validator_url,
            "etag": etag,
            "last_modified": last_modified,
            "last_http_status": status_code
        }

        if is_initial and not is_empty:
            updates["initial_sync_completed_at"] = now_str

        db.complete_source_poll(self.global_token, source['source_id'], source['lease_token'], source['claimed_mode'], source['claimed_target'], updates, drafts)

    def requeue_poll(self, source: dict, reason: CancellationReason):
        delay = 60
        next_poll = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delay)
        updates = {
            "collector_status": "queued",
            "next_poll_at": next_poll.isoformat()
        }
        db.complete_source_poll(
            self.global_token, source['source_id'], source['lease_token'], source['claimed_mode'], source['claimed_target'],
            updates
        )

    def trigger_cancellation(self, reason: CancellationReason):
        if not self.cancellation_reason or CANCELLATION_PRIORITY[reason] > CANCELLATION_PRIORITY.get(self.cancellation_reason, 0):
            self.cancellation_reason = reason
        self.cancellation_event.set()
        if self.active_poll_task and not self.active_poll_task.done():
            self.active_poll_task.cancel()

    async def wait_for_cancellation(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.cancellation_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def heartbeat_loop(self):
        while not await self.wait_for_cancellation(HEARTBEAT_INTERVAL_SEC):
            if not db.heartbeat_global_lease(self.global_token, GLOBAL_LEASE_DURATION_SEC):
                self.trigger_cancellation(CancellationReason.GLOBAL_LEASE_LOST)
                return

    async def cleanup_loop(self):
        while not await self.wait_for_cancellation(60):
            await self.host_limiter.gc_idle()

    async def run(self):
        try:
            check_startup_config()
        except StartupConfigError:
            sys.exit(1)

        while True:
            self.global_token = db.acquire_global_lease(self.worker_id, GLOBAL_LEASE_DURATION_SEC)
            if not self.global_token:
                await asyncio.sleep(5)
                continue

            self.cancellation_event.clear()
            self.cancellation_reason = None
            heartbeat_task = asyncio.create_task(self.heartbeat_loop())
            cleanup_task = asyncio.create_task(self.cleanup_loop())

            try:
                limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
                timeout = httpx.Timeout(5.0, read=10.0, connect=5.0)

                async with httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=False) as client:
                    while not self.cancellation_event.is_set():
                        source = db.claim_due_poll_source(self.global_token, SOURCE_LEASE_SEC)
                        if not source:
                            await asyncio.sleep(5)
                            continue

                        self.active_poll_task = asyncio.create_task(self.process_source(client, source))
                        try:
                            await asyncio.wait_for(
                                self.active_poll_task,
                                timeout=POLL_DEADLINE_SEC
                            )
                        except asyncio.TimeoutError:
                            if not self.active_poll_task.done():
                                self.active_poll_task.cancel()
                            self.fail_poll(source, "timeout")
                        except asyncio.CancelledError:
                            break
                        except Exception:
                            self.fail_poll(source, "internal_error")

            finally:
                self.trigger_cancellation(CancellationReason.SHUTDOWN)
                await asyncio.gather(heartbeat_task, cleanup_task, return_exceptions=True)
                if self.global_token:
                    db.release_global_lease(self.global_token)

if __name__ == "__main__":
    worker = PollingWorker()
    asyncio.run(worker.run())
