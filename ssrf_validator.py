import urllib.parse
from typing import Optional, Protocol
import ipaddress
import socket
import asyncio

class SSRFError(Exception):
    pass

class URLValidationError(Exception):
    pass

class ResolverProtocol(Protocol):
    async def resolve(self, hostname: str, port: int) -> list[str]:
        ...

class SystemResolver(ResolverProtocol):
    async def resolve(self, hostname: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            info = await loop.getaddrinfo(hostname, port, family=socket.AF_INET)
            return [ip[4][0] for ip in info]
        except socket.gaierror:
            return []

system_resolver = SystemResolver()

def validate_url_syntax(url: str) -> str:
    """
    Validates URL syntax and safety:
    - Schemes: http, https only
    - Ports: 80, 443 only
    - No credentials (@ before path)
    - No control characters
    - Returns normalized URL
    """
    url = url.strip()
    if not url:
        raise URLValidationError("Empty URL")

    for ch in url:
        if ord(ch) < 0x20 or ord(ch) == 0x7f:
            raise URLValidationError("Control characters are not allowed in URL")

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        raise URLValidationError(f"Invalid URL format: {e}")

    if parsed.scheme not in ('http', 'https'):
        raise URLValidationError(f"Forbidden scheme: {parsed.scheme}")

    if parsed.port and parsed.port not in (80, 443):
        raise URLValidationError(f"Forbidden port: {parsed.port}")

    if parsed.username or parsed.password:
        raise URLValidationError("Credentials in URL are not allowed")

    hostname = parsed.hostname
    if not hostname:
        raise URLValidationError("Missing hostname")

    if hostname.endswith('.'):
        raise URLValidationError("Trailing dots in hostname are not allowed")
        
    try:
        # Check IDNA validity
        hostname.encode('idna')
    except Exception:
        raise URLValidationError("Invalid IDNA hostname")

    return url

async def validate_dns_resolution(hostname: str, *, resolver: ResolverProtocol = system_resolver):
    """
    Resolves the hostname and validates all returned IPs against SSRF policies.
    Raises SSRFError if ANY resolved IP is in a forbidden range.
    """
    try:
        # Try parsing as IP first to avoid DNS lookup for direct IPs
        ip_obj = ipaddress.ip_address(hostname)
        _validate_ip(ip_obj)
        return
    except ValueError:
        pass # Not a direct IP, need DNS resolution

    ips = await resolver.resolve(hostname, 80)
    if not ips:
        raise SSRFError(f"DNS resolution failed or returned no results for {hostname}")

    for ip_str in ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            _validate_ip(ip_obj)
        except ValueError:
            continue

def _validate_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """
    Validates a single IP against a strict allowlist/denylist approach.
    Raises SSRFError if forbidden.
    """
    # 1. Check global restrictions
    if ip.is_private:
        raise SSRFError(f"IP {ip} is private")
    if ip.is_loopback:
        raise SSRFError(f"IP {ip} is loopback")
    if ip.is_link_local:
        raise SSRFError(f"IP {ip} is link-local")
    if ip.is_multicast:
        raise SSRFError(f"IP {ip} is multicast")
    if ip.is_reserved:
        raise SSRFError(f"IP {ip} is reserved")
    if ip.is_unspecified:
        raise SSRFError(f"IP {ip} is unspecified")
        
    # 2. Specific IPv4 checks
    if isinstance(ip, ipaddress.IPv4Address):
        # 169.254.169.254 AWS/Cloud metadata
        if str(ip) == "169.254.169.254":
            raise SSRFError(f"IP {ip} is metadata service")
        # 100.64.0.0/10 Carrier-grade NAT
        cgnat = ipaddress.ip_network("100.64.0.0/10")
        if ip in cgnat:
            raise SSRFError(f"IP {ip} is CGNAT")
        # 192.0.0.0/24 IETF Protocol Assignments
        ietf = ipaddress.ip_network("192.0.0.0/24")
        if ip in ietf:
            raise SSRFError(f"IP {ip} is IETF assignment")
        # 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24 Documentation/Benchmark
        doc1 = ipaddress.ip_network("192.0.2.0/24")
        doc2 = ipaddress.ip_network("198.51.100.0/24")
        doc3 = ipaddress.ip_network("203.0.113.0/24")
        if ip in doc1 or ip in doc2 or ip in doc3:
            raise SSRFError(f"IP {ip} is documentation/benchmark")

    # 3. Specific IPv6 checks
    elif isinstance(ip, ipaddress.IPv6Address):
        # IPv4-mapped IPv6 addresses (::ffff:0:0/96)
        if ip.ipv4_mapped is not None:
            raise SSRFError(f"IP {ip} is IPv4-mapped IPv6")
        
        # Documentation/Benchmark 2001:db8::/32
        doc_ipv6 = ipaddress.ip_network("2001:db8::/32")
        if ip in doc_ipv6:
            raise SSRFError(f"IP {ip} is documentation/benchmark")

    # If it passed all checks, it's considered safe (public routable IP)
    return True

async def validate_url_and_dns(url: str, *, resolver: ResolverProtocol = system_resolver) -> str:
    """
    Combined validation for URL syntax, policy, and DNS SSRF.
    Returns the normalized URL on success, or raises URLValidationError / SSRFError.
    """
    normalized_url = validate_url_syntax(url)
    parsed = urllib.parse.urlparse(normalized_url)
    await validate_dns_resolution(parsed.hostname, resolver=resolver)
    return normalized_url
