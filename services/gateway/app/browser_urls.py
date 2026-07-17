from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


NEXUS_SHORT_HOST_ALIASES = frozenset(
    {
        "stackrot",
        "ai2",
        "migraine",
        "ada2",
        "meltdown",
        "copyfail",
        "adada",
    }
)


def _resolved_ipv4(hostname: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except (OSError, socket.gaierror):
        return ""
    for info in infos:
        try:
            address = str(info[4][0]).strip()
            return str(ipaddress.IPv4Address(address))
        except (IndexError, TypeError, ValueError):
            continue
    return ""


def browser_accessible_url(raw_url: str) -> str:
    """Resolve Nexus-only HTTP host aliases for links opened by a user's browser.

    Gateway containers can resolve short Nexus aliases through Compose
    ``extra_hosts``, while the operator's browser may not have matching DNS.
    HTTPS and non-Nexus names are deliberately unchanged because substituting an
    IP address would commonly invalidate TLS certificates or alter public URLs.
    """

    value = str(raw_url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").strip().lower()
        if (
            parsed.scheme.lower() != "http"
            or hostname not in NEXUS_SHORT_HOST_ALIASES
            or parsed.username is not None
            or parsed.password is not None
        ):
            return value
        port = parsed.port or 80
    except ValueError:
        return value

    address = _resolved_ipv4(hostname, port)
    if not address:
        return value
    netloc = f"{address}:{parsed.port}" if parsed.port is not None else address
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
