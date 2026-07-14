import asyncio
import ipaddress
import re
import socket
import urllib.parse
from typing import Protocol, Sequence


class SSRFError(Exception):
    """A bounded SSRF policy failure safe to expose to callers."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class URLValidationError(Exception):
    """A bounded URL policy failure safe to expose to callers."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ResolverProtocol(Protocol):
    async def resolve(self, hostname: str, port: int) -> Sequence[str]: ...


class SystemResolver(ResolverProtocol):
    async def resolve(self, hostname: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            info = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return []
        return list(dict.fromkeys(item[4][0] for item in info))


system_resolver = SystemResolver()
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]+|0[0-7]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|0[0-7]+|[0-9]+))*$", re.I)
_ALLOWED_PORTS = {80, 443}


def validate_url_syntax(url: str) -> str:
    if not isinstance(url, str) or not url or url != url.strip():
        raise URLValidationError("invalid_url")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise URLValidationError("url_control_character")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise URLValidationError("invalid_url") from None
    if parsed.scheme.lower() not in {"http", "https"}:
        raise URLValidationError("unsafe_scheme")
    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError("url_credentials")
    if not parsed.hostname:
        raise URLValidationError("missing_hostname")
    if port is not None and port not in _ALLOWED_PORTS:
        raise URLValidationError("unsafe_port")

    hostname = parsed.hostname
    if hostname.endswith("."):
        raise URLValidationError("trailing_dot_hostname")
    if _NUMERIC_HOST.fullmatch(hostname) and not _is_plain_ip_literal(hostname):
        raise URLValidationError("ambiguous_numeric_hostname")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise URLValidationError("invalid_idna_hostname") from None

    host = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    if port is not None:
        host = f"{host}:{port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), host, parsed.path or "/", parsed.query, "")
    )


def _is_plain_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _normalized_address(raw_address: str):
    try:
        address = ipaddress.ip_address(raw_address)
    except ValueError:
        raise SSRFError("dns_invalid_address") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def _validate_ip(address) -> None:
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise SSRFError("unsafe_resolved_address")
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network("100.64.0.0/10"):
        raise SSRFError("unsafe_resolved_address")


async def validate_dns_resolution(
    hostname: str,
    *,
    port: int = 80,
    resolver: ResolverProtocol = system_resolver,
) -> tuple[str, ...]:
    try:
        direct = _normalized_address(hostname)
    except SSRFError as error:
        if error.code != "dns_invalid_address":
            raise
    else:
        _validate_ip(direct)
        return (str(direct),)

    addresses = await resolver.resolve(hostname, port)
    if not addresses:
        raise SSRFError("dns_no_addresses")
    validated = []
    for raw_address in addresses:
        address = _normalized_address(raw_address)
        _validate_ip(address)
        validated.append(str(address))
    return tuple(validated)


async def validate_url_and_dns(
    url: str,
    *,
    resolver: ResolverProtocol = system_resolver,
) -> str:
    normalized = validate_url_syntax(url)
    parsed = urllib.parse.urlsplit(normalized)
    await validate_dns_resolution(
        parsed.hostname or "",
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
        resolver=resolver,
    )
    return normalized
