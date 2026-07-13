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

from database import db
from config import settings
from ssrf_validator import validate_url_and_dns, SSRFError, URLValidationError

logger = logging.getLogger(__name__)

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

    async def acquire(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname.lower() if parsed.hostname else ""
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
        except asyncio.CancelledError:
            async with self._registry_lock:
                state.waiter_count -= 1
            raise
            
        async with self._registry_lock:
            state.waiter_count -= 1
            state.owner_count += 1
            state.last_used_monotonic = time.monotonic()
            
        try:
            now = time.monotonic()
            async with self._registry_lock:
                allowed = state.next_allowed_monotonic
            if now < allowed:
                await asyncio.sleep(allowed - now)
        except asyncio.CancelledError:
            await self.release(key)
            raise
            
        return key

    async def release(self, key: str):
        async with self._registry_lock:
            state = self._states[key]
            state.owner_count -= 1
            state.next_allowed_monotonic = time.monotonic() + self._interval
            state.last_used_monotonic = time.monotonic()
            state.request_lock.release()

    async def cleanup_idle(self):
        now = time.monotonic()
        async with self._registry_lock:
            keys_to_delete = []
            for k, state in self._states.items():
                if state.owner_count == 0 and state.waiter_count == 0 and not state.request_lock.locked() and (now - state.last_used_monotonic > self._idle_ttl):
                    keys_to_delete.append(k)
            for k in keys_to_delete:
                del self._states[k]

class RobotsCache:
    def __init__(self):
        self.cache = {}

    def _get_key(self, url: str):
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname.lower() if parsed.hostname else ""
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
            'expires_at': now + ttl
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
        if tag_lower in SUPPRESSED_TAGS or self._suppressed_stack:
            self._suppressed_stack.append(tag_lower)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.casefold()
        if not self._suppressed_stack:
            return
        # Pop up to matching tag to handle malformed/missing closures
        for i in range(len(self._suppressed_stack) - 1, -1, -1):
            if self._suppressed_stack[i] == tag_lower:
                del self._suppressed_stack[i:]
                break

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

def strip_tracking_params(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    blocked_prefixes = ('utm_', 'fbclid', 'gclid', 'mc_cid', 'mc_eid')
    filtered = sorted([(k, v) for k, v in qs if not any(k.lower().startswith(p) for p in blocked_prefixes)])
    new_query = urllib.parse.urlencode(filtered)
    return urllib.parse.urlunparse((parsed.scheme, parsed.hostname, parsed.path, parsed.params, new_query, ""))

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

class FetchResult:
    def __init__(self):
        self.status_code = None
        self.content = b""
        self.etag = None
        self.last_modified = None
        self.error_code = None
        self.redirect_url = None
        self.retry_after = None
        self.final_url = None

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

    async def fetch_url_single(self, client: httpx.AsyncClient, url: str, max_bytes: int, etag=None, last_modified=None, website_mode=False) -> FetchResult:
        result = FetchResult()
        try:
            url = await validate_url_and_dns(url)
        except (SSRFError, URLValidationError):
            result.error_code = "ssrf_blocked"
            return result

        result.final_url = url
        host_key = await self.host_limiter.acquire(url)
        try:
            headers = {"User-Agent": USER_AGENT}
            if etag: headers["If-None-Match"] = etag
            if last_modified: headers["If-Modified-Since"] = last_modified

            try:
                async with client.stream('GET', url, headers=headers) as resp:
                    result.status_code = resp.status_code
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc_headers = [v.decode('utf-8', errors='ignore') for k, v in resp.headers.raw if k.lower() == b'location']
                        if not loc_headers:
                            result.error_code = "redirect_missing_location"
                            return result
                        
                        if len(loc_headers) > 1:
                            result.error_code = "redirect_ambiguous_location"
                            return result
                            
                        loc = loc_headers[0].strip()
                        if not loc:
                            result.error_code = "redirect_missing_location"
                            return result
                            
                        if any(ord(c) < 32 or ord(c) == 127 for c in loc):
                            result.error_code = "redirect_invalid_location"
                            return result
                            
                        result.redirect_url = loc
                        return result

                    result.etag = resp.headers.get("ETag")
                    result.last_modified = resp.headers.get("Last-Modified")
                    if "retry-after" in resp.headers:
                        result.retry_after = parse_retry_after(resp.headers["retry-after"])

                    if resp.status_code == 304:
                        return result
                        
                    if resp.status_code >= 400:
                        if resp.status_code in (429, 503):
                            result.error_code = f"http_{resp.status_code}"
                        elif resp.status_code in (400, 401, 403, 404, 410):
                            result.error_code = "http_4xx"
                        else:
                            result.error_code = f"http_{resp.status_code}"
                        return result

                    content_type = resp.headers.get("Content-Type", "").lower()
                    if website_mode:
                        if "text/html" not in content_type:
                            result.error_code = "unsupported_content_type"
                            return result
                    else:
                        if max_bytes == MAX_ROBOTS_BYTES:
                            pass # robots.txt is flexible
                        else:
                            if "application/xml" not in content_type and "application/rss+xml" not in content_type and "application/atom+xml" not in content_type:
                                if "text/plain" not in content_type:
                                    result.error_code = "unsupported_content_type"
                                    return result

                    encoding_raw = resp.headers.get("Content-Encoding")
                    if encoding_raw is not None:
                        # Fallback for checking multiple headers if httpx concatenated them
                        raw_list = [v.decode('utf-8', errors='ignore') for k, v in resp.headers.raw if k.lower() == b'content-encoding']
                        if len(raw_list) > 1:
                            result.error_code = "ambiguous_content_encoding"
                            return result
                            
                        encoding = encoding_raw.lower().strip()
                        if encoding == "":
                            result.error_code = "invalid_content_encoding"
                            return result
                        if "," in encoding:
                            result.error_code = "multiple_content_encodings"
                            return result
                        if encoding not in ("identity", "gzip", "deflate"):
                            result.error_code = "unsupported_content_encoding"
                            return result

                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        result.error_code = "content_too_large"
                        return result

                    content = bytearray()
                    async for chunk in resp.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > max_bytes:
                            result.error_code = "content_too_large"
                            return result

                    if not website_mode and max_bytes != MAX_ROBOTS_BYTES and "text/plain" in content_type:
                        sniff = bytes(content[:50]).decode('utf-8', errors='ignore').strip()
                        if not sniff.startswith("<?xml") and not sniff.startswith("<rss") and not sniff.startswith("<feed"):
                            result.error_code = "unsupported_content_type"
                            return result

                    result.content = bytes(content)
            except httpx.TimeoutException:
                result.error_code = "timeout"
            except httpx.RequestError:
                result.error_code = "network_error"
            except httpx.DecodingError:
                result.error_code = "invalid_content_encoding"
            except Exception as e:
                # Raw traceback and exception strings are explicitly forbidden
                result.error_code = "internal_error"
        finally:
            await self.host_limiter.release(host_key)
            
        return result

    async def check_robots(self, client: httpx.AsyncClient, origin_url: str) -> bool:
        cached = self.robots_cache.check_hit(origin_url)
        if cached is not None:
            return cached['decision'] == 'allow'

        try:
            parsed = urllib.parse.urlparse(origin_url)
            robots_url = f"{parsed.scheme}://{parsed.hostname}{(':' + str(parsed.port)) if parsed.port else ''}/robots.txt"
        except:
            return False

        redirect_count = 0
        url_to_fetch = robots_url
        visited = set()
        visited.add(url_to_fetch)
        res = None
        
        while redirect_count <= MAX_REDIRECTS:
            res = await self.fetch_url_single(client, url_to_fetch, MAX_ROBOTS_BYTES, website_mode=False)
            if self.cancellation_event.is_set():
                return False
                
            if res.redirect_url:
                try:
                    next_url = urllib.parse.urljoin(url_to_fetch, res.redirect_url)
                    parsed_next = urllib.parse.urlparse(next_url)
                    next_url = urllib.parse.urlunparse((parsed_next.scheme.lower(), parsed_next.netloc.lower(), parsed_next.path, parsed_next.params, parsed_next.query, parsed_next.fragment))
                except:
                    break
                if next_url in visited:
                    break
                visited.add(next_url)
                url_to_fetch = next_url
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS:
                    break
                continue
            break

        decision = 'error'
        error_code = None
        ttl = 900 # 15m default error TTL
        delay = 0
        
        if res.status_code in (404, 410):
            decision = 'allow'
            ttl = 86400 # 24h
        elif res.status_code == 200:
            try:
                text = res.content.decode('utf-8')
                # Optional check for some extreme malformedness if needed, but strict decoding helps
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(text.splitlines())
                if rp.can_fetch(ROBOTS_PRODUCT_TOKEN, origin_url):
                    decision = 'allow'
                else:
                    decision = 'deny'
                    error_code = 'robots_rule_denied'
                ttl = 86400 # 24h
            except UnicodeDecodeError:
                decision = 'error'
                error_code = 'robots_parse_error'
                ttl = 900
        elif res.status_code in (401, 403):
            decision = 'deny'
            error_code = 'robots_auth_denied'
            ttl = 900 # 15m
        elif res.status_code == 429:
            decision = 'error'
            error_code = 'robots_rate_limited'
            delay = min(86400, max(10, res.retry_after)) if res.retry_after else 0
            if delay > 0:
                ttl = delay
            else:
                ttl = 900
                delay = 0 # Fallback
        elif res.status_code and res.status_code >= 500:
            decision = 'error'
            error_code = 'robots_server_error'
            ttl = 900
        elif res.error_code:
            decision = 'error'
            ttl = 900
            if res.error_code == 'content_too_large':
                error_code = 'robots_body_too_large'
            elif res.error_code == 'invalid_content_encoding':
                error_code = 'robots_decoding_error'
            elif res.error_code in ('timeout', 'network_error'):
                error_code = f"robots_{res.error_code}"
            elif res.error_code.startswith("redirect_"):
                error_code = res.error_code
            elif res.error_code == 'too_many_redirects':
                error_code = 'too_many_redirects'
            else:
                error_code = 'robots_error'
        else:
            decision = 'error'
            error_code = 'robots_error'
            ttl = 900

        self.robots_cache.store(origin_url, decision, error_code, delay, ttl)
        return decision == 'allow'

    def get_backoff_delay(self, previous_errors: int, retry_after: int = None, is_4xx: bool = False) -> int:
        if is_4xx:
            return 86400
        if retry_after:
            return max(10, min(86400, retry_after))
        errors = previous_errors + 1
        base = min(86400, 900 * (2 ** (errors - 1)))
        delay = max(10, min(86400, base * self._rng.uniform(0.9, 1.1)))
        return int(delay)

    async def process_source(self, client: httpx.AsyncClient, source: dict):
        source_id = source['source_id']
        source_token = source['lease_token']
        source_type = source['source_type']
        claimed_mode = source['claimed_mode']
        claimed_target = source['claimed_target']
        
        url_to_fetch = claimed_target
        website_mode = claimed_mode == 'website discovery'
        
        if not await self.check_robots(client, url_to_fetch):
            self.fail_poll(source, "blocked_by_robots")
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
                etag=req_etag, last_modified=req_lm, website_mode=website_mode
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
                    if not await self.check_robots(client, next_url):
                        self.fail_poll(source, "blocked_by_robots", res.retry_after, res.status_code)
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
            if current_validator_url != res.final_url:
                self.fail_poll(source, "protocol_error", None, res.status_code)
                return
            self.succeed_poll(source, current_etag, current_last_modified, res.final_url, res.status_code, [])
            return

        if website_mode:
            html = res.content.decode('utf-8', errors='ignore')
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
                        await validate_url_and_dns(base)
                    except:
                        base = res.final_url
                        
                    resolved = urllib.parse.urljoin(base, best)
                    await validate_url_and_dns(resolved)
                    
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
                db.complete_source_poll(
                    self.global_token, source_id, source_token, claimed_mode, claimed_target,
                    {
                        "collector_status": "unsupported",
                        "last_error_code": "unsupported_type",
                        "next_poll_at": None,
                        "validator_url": None,
                        "etag": None,
                        "last_modified": None,
                        "last_http_status": res.status_code
                    }
                )
            return

        # Feed Mode
        try:
            feed = feedparser.parse(res.content)
            
            if feed.bozo:
                ex_name = type(feed.bozo_exception).__name__
                if ex_name not in ("CharacterEncodingOverride", "CharacterEncodingUnknown", "NonXMLContentType", "ChardetException"):
                    self.fail_poll(source, "parse_error", http_status=res.status_code)
                    return
                    
            if not feed.entries:
                self.succeed_poll(source, res.etag, res.last_modified, res.final_url, res.status_code, [], is_empty=True)
                return
        except Exception:
            self.fail_poll(source, "parse_error", http_status=res.status_code)
            return

        is_initial = source['initial_sync_completed_at'] is None
        limit = 10 if is_initial else 50
        
        drafts = []
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        cutoff_dt = now_dt - datetime.timedelta(hours=72)
        
        seen_identities = set()
        
        for entry in feed.entries:
            if len(drafts) >= limit:
                break
                
            entry_id = compute_entry_identity(entry, res.final_url)
            if entry_id in seen_identities:
                continue
            seen_identities.add(entry_id)
            
            pub_dt = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                pub_dt = datetime.datetime(*entry.published_parsed[:6], tzinfo=datetime.timezone.utc)
                if is_initial and pub_dt < cutoff_dt:
                    continue
                if pub_dt > now_dt:
                    pub_dt = now_dt
            
            upd_dt = None
            if hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                upd_dt = datetime.datetime(*entry.updated_parsed[:6], tzinfo=datetime.timezone.utc)
                if upd_dt > now_dt:
                    upd_dt = now_dt

            html_parser = SafeHTMLParser()
            raw_text = str(entry.get('title', '')) + " " + str(entry.get('description', ''))
            html_parser.feed(raw_text)
            clean_text = html_parser.get_text()

            drafts.append({
                "source_item_id": entry_id,
                "original_text": clean_text,
                "source_published_at": pub_dt.isoformat() if pub_dt else None,
                "source_updated_at": upd_dt.isoformat() if upd_dt else None
            })

        self.succeed_poll(source, res.etag, res.last_modified, res.final_url, res.status_code, drafts, is_initial=is_initial)

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
        
        prio = source.get('priority', 3)
        if prio == 1: delay = 1800
        elif prio == 2: delay = 3600
        elif prio == 3: delay = 7200
        elif prio == 4: delay = 21600
        else: delay = 43200
            
        next_poll = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delay)
        
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

    async def heartbeat_loop(self):
        while not self.cancellation_event.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
            if not db.heartbeat_global_lease(self.global_token, GLOBAL_LEASE_DURATION_SEC):
                self.trigger_cancellation(CancellationReason.GLOBAL_LEASE_LOST)
                return

    async def cleanup_loop(self):
        while not self.cancellation_event.is_set():
            await asyncio.sleep(60)
            await self.host_limiter.cleanup_idle()

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
