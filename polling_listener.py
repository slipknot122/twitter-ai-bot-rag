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

import httpx
import feedparser

from database import db
from config import settings
from ssrf_validator import validate_url_and_dns, SSRFError, URLValidationError

logger = logging.getLogger(__name__)

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
    if os.environ.get("RSS_EGRESS_SANDBOX_CONFIRMED", "").lower() != "true":
        raise StartupConfigError("RSS_EGRESS_SANDBOX_CONFIRMED is not true. Worker disabled for safety.")
    if HEARTBEAT_INTERVAL_SEC >= GLOBAL_LEASE_DURATION_SEC:
        raise StartupConfigError("Heartbeat interval must be strictly less than global lease duration")
    if POLL_DEADLINE_SEC >= SOURCE_LEASE_SEC:
        raise StartupConfigError("Poll deadline must be strictly less than source lease duration")

class HostLimiter:
    def __init__(self):
        self.locks = {}
        self.next_allowed = {}
        self.waiter_counts = {}
        self.last_used = {}
        self._global_lock = asyncio.Lock()

    async def acquire(self, url: str):
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname.lower() if parsed.hostname else ""
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        key = f"{parsed.scheme}://{host}:{port}"
        
        async with self._global_lock:
            if key not in self.locks:
                self.locks[key] = asyncio.Lock()
                self.waiter_counts[key] = 0
                self.next_allowed[key] = 0.0
                self.last_used[key] = time.monotonic()
            
            self.waiter_counts[key] += 1
            lock = self.locks[key]

        await lock.acquire()
        
        try:
            now = time.monotonic()
            allowed = self.next_allowed[key]
            if now < allowed:
                await asyncio.sleep(allowed - now)
        except asyncio.CancelledError:
            self._release(key)
            raise
            
        return key

    def release(self, key: str):
        self.next_allowed[key] = time.monotonic() + HOST_INTERVAL_SEC
        self._release(key)

    def _release(self, key: str):
        self.locks[key].release()
        self.waiter_counts[key] -= 1
        self.last_used[key] = time.monotonic()

    async def cleanup(self):
        now = time.monotonic()
        async with self._global_lock:
            keys_to_delete = []
            for k in self.locks:
                if self.waiter_counts[k] == 0 and not self.locks[k].locked() and (now - self.last_used[k] > HOST_CLEANUP_TTL_SEC):
                    keys_to_delete.append(k)
            for k in keys_to_delete:
                del self.locks[k]
                del self.next_allowed[k]
                del self.waiter_counts[k]
                del self.last_used[k]

class RobotsCache:
    def __init__(self):
        self.cache = {}

    def _get_key(self, url: str):
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname.lower() if parsed.hostname else ""
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return f"{parsed.scheme}://{host}:{port}|{USER_AGENT}"

    def check_hit(self, url: str) -> Optional[bool]:
        now = time.monotonic()
        key = self._get_key(url)
        if key in self.cache:
            allowed, expires = self.cache[key]
            if now < expires:
                return allowed
        return None

    def store(self, url: str, allowed: bool, ttl: int):
        now = time.monotonic()
        key = self._get_key(url)
        self.cache[key] = (allowed, now + ttl)

class SafeHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_chunks = []
        self.skip_depth = 0
        self.skip_tags = {'script', 'style', 'noscript', 'iframe', 'object', 'embed'}
        self.total_len = 0
        self.max_len = 3000

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.skip_tags:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self.skip_tags:
            self.skip_depth = max(0, self.skip_depth - 1)

    def handle_data(self, data):
        if self.total_len >= self.max_len:
            return
        if self.skip_depth == 0:
            cleaned = "".join(" " if c in ('\t', '\n', '\r') else c for c in data if ord(c) >= 32 or c in ('\t', '\n', '\r'))
            if cleaned:
                self.text_chunks.append(cleaned)
                self.total_len += len(cleaned)
            
    def get_text(self) -> str:
        raw = " ".join(self.text_chunks)
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
        self.host_limiter = HostLimiter()
        self.robots_cache = RobotsCache()
        self.active_poll_task = None
        self._rng = random.Random()

    async def fetch_url(self, client: httpx.AsyncClient, url: str, max_bytes: int, etag=None, last_modified=None, website_mode=False) -> FetchResult:
        result = FetchResult()
        try:
            url = await validate_url_and_dns(url)
        except (SSRFError, URLValidationError):
            result.error_code = "validation_error"
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
                        loc = resp.headers.get("Location")
                        if loc:
                            result.redirect_url = loc
                        else:
                            result.error_code = "invalid_redirect"
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

                    encoding = resp.headers.get("Content-Encoding", "identity").lower()
                    if encoding not in ("identity", "gzip", "deflate"):
                        result.error_code = "unsupported_encoding"
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
            except Exception:
                result.error_code = "internal_error"
        finally:
            self.host_limiter.release(host_key)
            
        return result

    async def check_robots(self, client: httpx.AsyncClient, origin_url: str) -> bool:
        cached = self.robots_cache.check_hit(origin_url)
        if cached is not None:
            return cached

        try:
            parsed = urllib.parse.urlparse(origin_url)
            robots_url = f"{parsed.scheme}://{parsed.hostname}{(':' + str(parsed.port)) if parsed.port else ''}/robots.txt"
        except:
            return False

        redirect_count = 0
        url_to_fetch = robots_url
        res = None
        while redirect_count <= MAX_REDIRECTS:
            res = await self.fetch_url(client, url_to_fetch, MAX_ROBOTS_BYTES, website_mode=False)
            if res.redirect_url:
                try:
                    url_to_fetch = urllib.parse.urljoin(url_to_fetch, res.redirect_url)
                except:
                    break
                redirect_count += 1
                continue
            break

        allowed = True
        ttl = ROBOTS_TTL_SEC
        
        if res.error_code:
            allowed = False
            ttl = ROBOTS_DENY_TTL_SEC
        elif res.status_code == 200:
            text = res.content.decode('utf-8', errors='ignore')
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(text.splitlines())
            allowed = rp.can_fetch(USER_AGENT, origin_url)
        elif res.status_code in (401, 403, 429):
            allowed = False
            ttl = ROBOTS_DENY_TTL_SEC
        elif res.status_code in (404, 410):
            allowed = True
        else:
            allowed = False
            ttl = ROBOTS_DENY_TTL_SEC

        self.robots_cache.store(origin_url, allowed, ttl)
        return allowed

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
        
        url_to_fetch = source['resolved_feed_url'] if source['resolved_feed_url'] else source['canonical_url']
        website_mode = (source_type == 'website' and not source['resolved_feed_url'])
        
        if not await self.check_robots(client, url_to_fetch):
            self.fail_poll(source, "blocked_by_robots")
            return

        redirect_count = 0
        res = None
        current_validator_url = source.get('validator_url')
        current_etag = source.get('etag')
        current_last_modified = source.get('last_modified')
        
        while redirect_count <= MAX_REDIRECTS:
            req_etag = current_etag if current_validator_url == url_to_fetch else None
            req_lm = current_last_modified if current_validator_url == url_to_fetch else None
            
            res = await self.fetch_url(
                client, url_to_fetch, MAX_DECODED_BYTES, 
                etag=req_etag, last_modified=req_lm, website_mode=website_mode
            )
            
            if self.cancellation_event.is_set():
                self.requeue_poll(source)
                return

            if res.redirect_url:
                try:
                    next_url = urllib.parse.urljoin(url_to_fetch, res.redirect_url)
                except:
                    self.fail_poll(source, "invalid_redirect")
                    return
                
                prev_origin = urllib.parse.urlparse(url_to_fetch).netloc
                next_origin = urllib.parse.urlparse(next_url).netloc
                if prev_origin != next_origin:
                    if not await self.check_robots(client, next_url):
                        self.fail_poll(source, "blocked_by_robots")
                        return
                        
                url_to_fetch = next_url
                redirect_count += 1
                continue
            break

        if redirect_count > MAX_REDIRECTS:
            self.fail_poll(source, "too_many_redirects")
            return

        if res.error_code:
            self.fail_poll(source, res.error_code, res.retry_after)
            return

        if res.status_code == 304:
            if current_validator_url != res.final_url:
                self.fail_poll(source, "protocol_error")
                return
            self.succeed_poll(source, current_etag, current_last_modified, res.final_url, None, [])
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
                        self.global_token, source_id, source_token,
                        {
                            "resolved_feed_url": resolved,
                            "validator_url": None,
                            "etag": None,
                            "last_modified": None,
                            "collector_status": "queued",
                            "next_poll_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "last_error_code": None,
                            "consecutive_errors": 0
                        }
                    )
                except Exception:
                    self.fail_poll(source, "invalid_feed_url")
            else:
                db.complete_source_poll(
                    self.global_token, source_id, source_token,
                    {
                        "collector_status": "unsupported",
                        "last_error_code": "unsupported_type",
                        "next_poll_at": None,
                        "validator_url": None,
                        "etag": None,
                        "last_modified": None,
                    }
                )
            return

        # Feed Mode
        try:
            feed = feedparser.parse(res.content)
            if not feed.entries:
                if feed.bozo:
                    self.fail_poll(source, "parse_error")
                    return
                else:
                    self.succeed_poll(source, res.etag, res.last_modified, res.final_url, None, [], is_empty=True)
                    return
            if feed.bozo:
                # Deterministic allowlist for recoverable bozo exceptions (e.g. unknown encoding) could go here
                # But for strictness fatal XML errors are rejected
                msg = getattr(feed.bozo_exception, 'getMessage', lambda: str(feed.bozo_exception))()
                if "xml" in msg.lower() or "encoding" in msg.lower():
                    self.fail_poll(source, "parse_error")
                    return
        except Exception:
            self.fail_poll(source, "parse_error")
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

    def fail_poll(self, source: dict, error_code: str, retry_after: int = None):
        delay = self.get_backoff_delay(source['consecutive_errors'], retry_after, is_4xx=(error_code=='http_4xx'))
        next_poll = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delay)
        
        db.complete_source_poll(
            self.global_token, source['source_id'], source['lease_token'],
            {
                "collector_status": error_code if error_code == "blocked_by_robots" else "backoff",
                "consecutive_errors": source['consecutive_errors'] + 1,
                "last_error_code": error_code,
                "last_attempt_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "next_poll_at": next_poll.isoformat()
            }
        )

    def succeed_poll(self, source: dict, etag: str, last_modified: str, validator_url: str, status_code: int, drafts: list, is_initial: bool = False, is_empty: bool = False):
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        next_poll = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)
        
        updates = {
            "collector_status": "healthy",
            "consecutive_errors": 0,
            "last_error_code": None,
            "last_attempt_at": now_str,
            "last_success_at": now_str,
            "next_poll_at": next_poll.isoformat(),
            "validator_url": validator_url,
            "etag": etag,
            "last_modified": last_modified
        }
        
        if is_initial and not is_empty:
            updates["initial_sync_completed_at"] = now_str
            
        db.complete_source_poll(self.global_token, source['source_id'], source['lease_token'], updates, drafts)

    def requeue_poll(self, source: dict):
        db.complete_source_poll(
            self.global_token, source['source_id'], source['lease_token'],
            {
                "collector_status": "queued",
                "last_error_code": "cancelled",
                "next_poll_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
        )

    async def heartbeat_loop(self):
        while not self.cancellation_event.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
            if not db.heartbeat_global_lease(self.global_token, GLOBAL_LEASE_DURATION_SEC):
                self.cancellation_event.set()
                if self.active_poll_task and not self.active_poll_task.done():
                    self.active_poll_task.cancel()
                return

    async def cleanup_loop(self):
        while not self.cancellation_event.is_set():
            await asyncio.sleep(60)
            await self.host_limiter.cleanup()

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
                self.cancellation_event.set()
                if self.active_poll_task and not self.active_poll_task.done():
                    self.active_poll_task.cancel()
                await asyncio.gather(heartbeat_task, cleanup_task, return_exceptions=True)
                if self.global_token:
                    db.release_global_lease(self.global_token)

if __name__ == "__main__":
    worker = PollingWorker()
    asyncio.run(worker.run())
