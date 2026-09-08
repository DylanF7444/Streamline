"""Network policy helpers for Personal Hub feed collection.

Feed URLs are user configuration, not trusted endpoints.  Every initial URL and
every redirect is checked before the transport is invoked.  DNS is resolved at
the point of use so a host cannot silently redirect a public-looking name to a
loopback or private address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

from personal_hub.feeds.contracts import (
    FeedPolicyError,
    FeedRedirectError,
    FeedSSRFError,
    FeedUnavailableError,
)

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "broadcasthost",
        "ip6-localhost",
        "ip6-loopback",
    }
)
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_MAX_ORIGIN_LENGTH = 512

HostResolver = Callable[[str, int], Sequence[object]]


def _canonical_request_url(value: str, *, base_url: str | None = None) -> str:
    if not isinstance(value, str):
        raise FeedPolicyError("Feed URL must be a string.")
    candidate = urljoin(base_url, value) if base_url is not None else value
    candidate = candidate.strip()
    if not candidate or len(candidate) > 2_048:
        raise FeedPolicyError("Feed URL is empty or too long.")
    parts = urlsplit(candidate)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise FeedPolicyError("Feed URL must use HTTP or HTTPS.")
    if parts.username is not None or parts.password is not None:
        raise FeedPolicyError("Feed URLs must not contain credentials.")
    if any(character in candidate for character in "\r\n\x00"):
        raise FeedPolicyError("Feed URL contains unsafe characters.")
    return urlunsplit((parts.scheme.casefold(), parts.netloc, parts.path, parts.query, ""))


def _host_ascii(host: str) -> str:
    normalized = host.strip().rstrip(".").casefold()
    if not normalized or len(normalized) > 253 or "\x00" in normalized:
        raise FeedPolicyError("Feed host is invalid.")
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise FeedPolicyError("Feed host is invalid.") from error
    if normalized in _BLOCKED_HOSTNAMES or normalized.endswith(_BLOCKED_SUFFIXES):
        raise FeedSSRFError("Local and private feed hosts are not allowed.")
    return normalized


def _port(parts: object) -> int:
    try:
        value = parts.port
    except ValueError as error:
        raise FeedPolicyError("Feed URL contains an invalid port.") from error
    return int(value or (443 if getattr(parts, "scheme", "").casefold() == "https" else 80))


def origin_for_url(url: str) -> str:
    """Return a normalized scheme/host/port origin without path or query."""

    if not isinstance(url, str):
        raise FeedPolicyError("Feed URL must be a string.")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        scheme = parts.scheme.casefold()
        port = _port(parts)
    except (TypeError, ValueError) as error:
        raise FeedPolicyError("Feed URL is invalid.") from error
    if scheme not in {"http", "https"} or not host:
        raise FeedPolicyError("Feed URL must use HTTP or HTTPS.")
    host = _host_ascii(host)
    if not 1 <= port <= 65_535:
        raise FeedPolicyError("Feed URL port is out of range.")
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    value = f"{scheme}://{display_host}"
    default_port = 443 if scheme == "https" else 80
    if port != default_port:
        value += f":{port}"
    if len(value) > _MAX_ORIGIN_LENGTH:
        raise FeedPolicyError("Feed origin is too long.")
    return value


def normalize_origins(origins: Iterable[str]) -> frozenset[str]:
    """Normalize a user-reviewed origin allow-list, rejecting unsafe entries."""

    normalized: set[str] = set()
    for value in origins:
        if not isinstance(value, str):
            raise FeedPolicyError("Reviewed feed origins must be strings.")
        try:
            parts = urlsplit(value.strip())
        except ValueError as error:
            raise FeedPolicyError("Reviewed feed origin is invalid.") from error
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise FeedPolicyError("Reviewed feed origins must not contain a path or query.")
        normalized.add(origin_for_url(value))
    return frozenset(normalized)


def _address_from_result(value: object) -> str | None:
    """Extract an address from common ``getaddrinfo`` or test-resolver shapes."""

    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        # Native getaddrinfo rows place the address at index 4.  A simple
        # ``(family, address)`` test shape is also accepted.
        if len(value) >= 5 and isinstance(value[4], tuple) and value[4]:
            return value[4][0]
        if len(value) >= 2 and isinstance(value[-1], str):
            return value[-1]
        if len(value) >= 2 and isinstance(value[-1], tuple) and value[-1]:
            return value[-1][0]
    return None


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    # ``is_global`` excludes loopback, link-local, multicast, reserved,
    # unspecified, documentation, and RFC1918/CGNAT ranges.
    return bool(address.is_global)


def validate_host_addresses(
    host: str,
    port: int,
    *,
    resolver: HostResolver | None = None,
) -> tuple[str, ...]:
    """Resolve and require globally routable addresses for a feed host."""

    host = _host_ascii(host)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_address(host):
            raise FeedSSRFError("Local and private feed hosts are not allowed.")
        return (host,)

    callback = resolver
    try:
        values = (
            callback(host, port)
            if callback is not None
            else socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        )
    except (socket.gaierror, OSError) as error:
        raise FeedUnavailableError("Feed host could not be resolved.") from error
    addresses = tuple(
        address
        for address in (_address_from_result(value) for value in values)
        if address is not None
    )
    if not addresses:
        raise FeedUnavailableError("Feed host could not be resolved.")
    if any(not _is_public_address(address) for address in addresses):
        raise FeedSSRFError("Feed host resolves to a local or private address.")
    return addresses


async def validate_url_for_request(
    url: str,
    *,
    allow_http: bool = False,
    reviewed_origins: Iterable[str] = (),
    resolver: HostResolver | None = None,
    resolve_host: bool = True,
) -> str:
    """Validate one outbound feed request and return its canonical URL."""

    try:
        normalized = _canonical_request_url(url)
        parts = urlsplit(normalized)
    except (TypeError, ValueError) as error:
        raise FeedPolicyError("Feed URL is invalid.") from error
    scheme = parts.scheme.casefold()
    if scheme == "http" and not allow_http:
        raise FeedPolicyError("HTTP feeds require explicit opt-in.")
    if parts.username is not None or parts.password is not None:
        raise FeedPolicyError("Feed URLs must not contain credentials.")
    origin = origin_for_url(normalized)
    # Reviewed origins are accepted as an explicit additional policy input.  An
    # initial subscription URL is already user-selected and therefore need not
    # appear in this optional list; callers use the list to authorize redirects.
    reviewed = normalize_origins(reviewed_origins)
    if resolve_host:
        validate_host_addresses(parts.hostname or "", _port(parts), resolver=resolver)
    else:
        # Even test/custom transports must not be allowed to target literal
        # loopback, link-local, or private addresses.  Only DNS lookup for a
        # named host is skipped when resolution is explicitly disabled.
        try:
            literal = ipaddress.ip_address(parts.hostname or "")
        except ValueError:
            literal = None
        if literal is not None and not _is_public_address(parts.hostname or ""):
            raise FeedSSRFError("Local and private feed hosts are not allowed.")
    # Keep this variable in the function so policy call sites can inspect the
    # exact normalized origin while this helper remains a small async boundary.
    _ = origin, reviewed
    await asyncio.sleep(0)
    return normalized


def validate_redirect(
    current_url: str,
    target_url: str,
    *,
    allow_http: bool = False,
    approved_origins: Iterable[str] = (),
    resolver: HostResolver | None = None,
    resolve_host: bool = True,
) -> str:
    """Validate a redirect against scheme, origin, and address policy."""

    try:
        normalized_target = _canonical_request_url(target_url, base_url=current_url)
    except FeedPolicyError as error:
        raise FeedRedirectError("Feed redirect target is invalid.") from error
    current_parts = urlsplit(current_url)
    target_parts = urlsplit(normalized_target)
    current_origin = origin_for_url(current_url)
    target_origin = origin_for_url(normalized_target)
    if target_parts.scheme.casefold() == "http" and not allow_http:
        raise FeedRedirectError("Feed redirect downgraded to HTTP.")
    approved = normalize_origins(approved_origins)
    if target_origin != current_origin and target_origin not in approved:
        raise FeedRedirectError("Feed redirect leaves the approved origin.")
    if resolve_host:
        validate_host_addresses(target_parts.hostname or "", _port(target_parts), resolver=resolver)
    else:
        try:
            literal = ipaddress.ip_address(target_parts.hostname or "")
        except ValueError:
            literal = None
        if literal is not None and not _is_public_address(target_parts.hostname or ""):
            raise FeedSSRFError("Local and private feed hosts are not allowed.")
    _ = current_parts
    return normalized_target


__all__ = [
    "HostResolver",
    "normalize_origins",
    "origin_for_url",
    "validate_host_addresses",
    "validate_redirect",
    "validate_url_for_request",
]
