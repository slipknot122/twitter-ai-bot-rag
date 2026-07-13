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
import re

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

def check_egress_firewall():
    if os.environ.get("RSS_EGRESS_SANDBOX_CONFIRMED", "").lower() != "true":
        logger.error("RSS_EGRESS_SANDBOX_CONFIRMED is not true. Worker disabled.")
        sys.exit(1)

class HostLimiter:
    def __init__(self):
        self.locks = {}
        self.next_allowed = {}

    async def wait_for_host(self, url: str):
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname.lower()
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        key = f"{parsed.scheme}://{host}:{port}"
        
        if key not in self.locks:
            self.locks[key] = asyncio.Lock()
            
        async with self.locks[key]:
            now = datetime.datetime.now(datetime.timezone.utc).timestamp()
            allowed = self.next_allowed.get(key, 0)
            if now < allowed:
                await asyncio.sleep(allowed - now)
            self.next_allowed[key] = datetime.datetime.now(datetime.timezone.utc).timestamp() + HOST_INTERVAL_SEC

class RobotsCache:
    def __init__(self):
        self.cache = {}

    def _get_key(self, url: str):
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname.lower()
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return f"{parsed.scheme}://{host}:{port}|{USER_AGENT}"

    def check_hit(self, url: str) -> Optional[bool]:
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        key = self._get_key(url)
        if key in self.cache:
            allowed, expires = self.cache[key]
            if now < expires:
                return allowed
        return None

    def store(self, url: str, allowed: bool, ttl: int):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        key = self._get_key(url)
        self.cache[key] = (allowed, now + ttl)


class SafeHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip_depth = 0
        self.skip_tags = {'script', 'style', 'noscript', 'iframe', 'object', 'embed'}

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.skip_tags:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self.skip_tags:
            self.skip_depth = max(0, self.skip_depth - 1)

    def handle_data(self, data):
        if self.skip_depth == 0:
            self.text.append(data)
            
    def get_text(self):
        text = " ".join(self.text).strip()
        text = "".join(ch for ch in text if ord(ch) >= 0x20 or ch in ('\t', '\n', '\r'))
        return text

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
    qs = urllib.parse.parse_qsl(parsed.query)
    blocked = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'fbclid', 'gclid', 'mc_cid', 'mc_eid'}
    filtered = sorted([(k, v) for k, v in qs if k.lower() not in blocked])
    new_query = urllib.parse.urlencode(filtered)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

def compute_entry_identity(entry: dict) -> str:
    if 'id' in entry and entry['id']:
        guid = entry['id']
        digest = hashlib.sha256(guid.encode('utf-8')).hexdigest()
        return f"guid:{digest}"
    if 'link' in entry and entry['link']:
        link = strip_tracking_params(entry['link'])
        digest = hashlib.sha256(link.encode('utf-8')).hexdigest()
        return f"link:{digest}"
    
    title = entry.get('title', '')
    published = entry.get('published', '')
    content = entry.get('description', '')
    text_to_hash = f"{title}|{published}|{content}"
    digest = hashlib.sha256(text_to_hash.encode('utf-8')).hexdigest()
    return f"hash:{digest}"

class FetchResult:
    def __init__(self):
        self.status_code = None
        self.content = b""
        self.etag = None
        self.last_modified = None
        self.error_code = None
        self.redirect_url = None
        self.retry_after = None

class PollingWorker:
    def __init__(self):
        self.worker_id = str(uuid.uuid4())
        self.global_token = None
        self.cancellation_event = asyncio.Event()
        self.host_limiter = HostLimiter()
        self.robots_cache = RobotsCache()

    async def fetch_url(self, client: httpx.AsyncClient, url: str, max_bytes: int, etag=None, last_modified=None, website_mode=False) -> FetchResult:
        result = FetchResult()
        try:
            url = await validate_url_and_dns(url)
        except (SSRFError, URLValidationError) as e:
            result.error_code = "validation_error"
            return result

        await self.host_limiter.wait_for_host(url)

        headers = {"User-Agent": USER_AGENT}
        if etag: headers["If-None-Match"] = etag
        if last_modified: headers["If-Modified-Since"] = last_modified

        try:
            async with client.stream('GET', url, headers=headers) as resp:
                result.status_code = resp.status_code
                if resp.status_code in (301, 302, 303, 307, 308):
                    result.redirect_url = resp.headers.get("Location")
                    return result

                result.etag = resp.headers.get("ETag")
                result.last_modified = resp.headers.get("Last-Modified")
                if "retry-after" in resp.headers:
                    try:
                        result.retry_after = int(resp.headers["retry-after"])
                    except:
                        pass # Ignore HTTP-date for now

                if resp.status_code == 304:
                    return result
                    
                if resp.status_code >= 400:
                    result.error_code = f"http_{resp.status_code}"
                    return result

                content_type = resp.headers.get("Content-Type", "").lower()
                if website_mode:
                    if "text/html" not in content_type:
                        result.error_code = "unsupported_content_type"
                        return result
                else:
                    if "text/plain" in content_type:
                        # Will do sniffing below
                        pass

                encoding = resp.headers.get("Content-Encoding", "identity").lower()
                if encoding not in ("identity", "gzip", "deflate"):
                    result.error_code = "unsupported_encoding"
                    return result

                content = bytearray()
                async for chunk in resp.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        result.error_code = "content_too_large"
                        return result

                # text/plain sniff
                if not website_mode and "text/plain" in content_type:
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

        return result

    async def check_robots(self, client: httpx.AsyncClient, url: str) -> bool:
        cached = self.robots_cache.check_hit(url)
        if cached is not None:
            return cached

        try:
            parsed = urllib.parse.urlparse(url)
            robots_url = f"{parsed.scheme}://{parsed.hostname}{(':' + str(parsed.port)) if parsed.port else ''}/robots.txt"
        except:
            return False

        res = await self.fetch_url(client, robots_url, MAX_ROBOTS_BYTES, website_mode=False)
        
        allowed = True
        ttl = ROBOTS_TTL_SEC
        
        if res.error_code:
            allowed = False
            ttl = ROBOTS_DENY_TTL_SEC
        elif res.status_code == 200:
            text = res.content.decode('utf-8', errors='ignore')
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(text.splitlines())
            allowed = rp.can_fetch(USER_AGENT, url)
        elif res.status_code in (401, 403, 429):
            allowed = False
            ttl = ROBOTS_DENY_TTL_SEC
        elif res.status_code in (404, 410):
            allowed = True
        else:
            allowed = False
            ttl = ROBOTS_DENY_TTL_SEC

        self.robots_cache.store(url, allowed, ttl)
        return allowed

    def get_backoff_delay(self, previous_errors: int, retry_after: int = None) -> int:
        errors = previous_errors + 1
        base = min(86400, 900 * (2 ** (errors - 1)))
        delay = max(10, min(86400, base * random.uniform(0.9, 1.1)))
        if retry_after:
            delay = max(10, min(86400, retry_after))
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
        
        while redirect_count <= MAX_REDIRECTS:
            res = await self.fetch_url(
                client, url_to_fetch, MAX_DECODED_BYTES, 
                etag=source['etag'] if redirect_count == 0 else None,
                last_modified=source['last_modified'] if redirect_count == 0 else None,
                website_mode=website_mode
            )
            
            if self.cancellation_event.is_set():
                return

            if res.redirect_url:
                try:
                    url_to_fetch = urllib.parse.urljoin(url_to_fetch, res.redirect_url)
                except:
                    self.fail_poll(source, "invalid_redirect")
                    return
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
            self.succeed_poll(source, res.etag, res.last_modified, None)
            return

        if website_mode:
            html = res.content.decode('utf-8', errors='ignore')
            parser = AutodiscoveryParser()
            parser.feed(html)
            
            best = None
            for c in parser.candidates:
                if c['type'] == 'application/atom+xml':
                    best = c['href']
                    break
            if not best and parser.candidates:
                best = parser.candidates[0]['href']

            if best:
                try:
                    base = parser.base_href or url_to_fetch
                    resolved = urllib.parse.urljoin(base, best)
                    await validate_url_and_dns(resolved) # Check SSRF
                    
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
                        "next_poll_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)).isoformat()
                    }
                )
            return

        # Feed Mode
        try:
            feed = feedparser.parse(res.content)
            if feed.bozo and getattr(feed.bozo_exception, 'getMessage', lambda: '')() == 'XML parsing failed':
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
        
        for entry in feed.entries:
            if len(drafts) >= limit:
                break
                
            entry_id = compute_entry_identity(entry)
            
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
            raw_text = entry.get('title', '') + " " + entry.get('description', '')
            html_parser.feed(raw_text)
            clean_text = html_parser.get_text()[:3000]

            drafts.append({
                "source_item_id": entry_id,
                "original_text": clean_text,
                "source_published_at": pub_dt.isoformat() if pub_dt else None,
                "source_updated_at": upd_dt.isoformat() if upd_dt else None
            })

        self.succeed_poll(source, res.etag, res.last_modified, drafts, is_initial)

    def fail_poll(self, source: dict, error_code: str, retry_after: int = None):
        delay = self.get_backoff_delay(source['consecutive_errors'], retry_after)
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

    def succeed_poll(self, source: dict, etag: str, last_modified: str, drafts: list, is_initial: bool = False):
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        next_poll = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)
        
        updates = {
            "collector_status": "healthy",
            "consecutive_errors": 0,
            "last_error_code": None,
            "last_attempt_at": now_str,
            "last_success_at": now_str,
            "next_poll_at": next_poll.isoformat()
        }
        
        if etag: updates["etag"] = etag
        if last_modified: updates["last_modified"] = last_modified
        if is_initial: updates["initial_sync_completed_at"] = now_str
        
        db.complete_source_poll(self.global_token, source['source_id'], source['lease_token'], updates, drafts)

    async def heartbeat_loop(self):
        while not self.cancellation_event.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
            if not db.heartbeat_global_lease(self.global_token, GLOBAL_LEASE_DURATION_SEC):
                logger.error("Lost global lease during heartbeat!")
                self.cancellation_event.set()
                return

    async def run(self):
        check_egress_firewall()
        
        while True:
            self.global_token = db.acquire_global_lease(self.worker_id, GLOBAL_LEASE_DURATION_SEC)
            if not self.global_token:
                await asyncio.sleep(5)
                continue
                
            self.cancellation_event.clear()
            heartbeat_task = asyncio.create_task(self.heartbeat_loop())
            
            try:
                # Disable redirects and set strict timeouts
                limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
                timeout = httpx.Timeout(5.0, read=10.0, connect=5.0)
                
                async with httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=False) as client:
                    while not self.cancellation_event.is_set():
                        source = db.claim_due_poll_source(self.global_token, SOURCE_LEASE_SEC)
                        if not source:
                            await asyncio.sleep(5)
                            continue
                            
                        # Process source with wall-clock deadline
                        try:
                            await asyncio.wait_for(
                                self.process_source(client, source),
                                timeout=POLL_DEADLINE_SEC
                            )
                        except asyncio.TimeoutError:
                            self.fail_poll(source, "timeout")
                        except Exception as e:
                            logger.error(f"Error processing source: {e}")
                            self.fail_poll(source, "internal_error")
                            
            except Exception as e:
                logger.error(f"Global worker loop exception: {e}")
            finally:
                self.cancellation_event.set()
                await heartbeat_task
                if self.global_token:
                    db.release_global_lease(self.global_token)

if __name__ == "__main__":
    worker = PollingWorker()
    asyncio.run(worker.run())
