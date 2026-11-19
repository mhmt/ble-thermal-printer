"""Request checks that protect the local web interface from other websites.

- Host allow-list: blocks DNS rebinding (an attacker domain resolving to this machine).
- POST must be `Content-Type: application/json`: a cross-site form or no-cors fetch can't send
  that without a CORS preflight, which this server never approves.
- Origin / Sec-Fetch-Site: when a browser sends them, the request must come from this page.

Non-browser clients (curl, scripts) don't send Origin or Sec-Fetch-Site and only need a valid Host.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
from urllib.parse import urlsplit

log = logging.getLogger("ble_thermal_printer.httpguard")

LOOPBACK = ("localhost", "127.0.0.1", "::1")
WILDCARD = ("", "0.0.0.0", "::")
MAX_BODY_BYTES = 8 * 1024 * 1024  # base64 images; the UI downsizes large photos before upload


class Rejected(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def split_host(value: str) -> str:
    """'Example.local:8765' -> 'example.local', '[::1]:8765' -> '::1'."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value  # bare name or bare IPv6


def _local_addresses() -> set[str]:
    names: set[str] = set()
    try:
        hostname = socket.gethostname().lower()
        names |= {hostname, hostname.split(".")[0] + ".local"}
        for info in socket.getaddrinfo(hostname, None):
            names.add(str(info[4][0]).lower())
    except OSError:
        pass
    # primary outbound address (no packet is sent for a UDP connect)
    for family, probe in ((socket.AF_INET, ("192.0.2.1", 9)), (socket.AF_INET6, ("2001:db8::1", 9))):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect(probe)
                names.add(s.getsockname()[0].lower())
        except OSError:
            pass
    return names


def allowed_hosts(bind_host: str, extra: list[str] | tuple[str, ...] = ()) -> set[str] | None:
    """Host names accepted in the Host header; None means any (explicit '*')."""
    extra = [split_host(h) for h in extra if h.strip()]
    if "*" in extra:
        return None
    hosts = set(LOOPBACK) | set(extra)
    bind = split_host(bind_host)
    if bind in WILDCARD:
        hosts |= _local_addresses()
    else:
        hosts.add(bind)
    return hosts


def is_loopback(bind_host: str) -> bool:
    h = split_host(bind_host)
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def check(method: str, headers, hosts: set[str] | None) -> None:
    """Raise Rejected if the request must not be served."""
    host_header = headers.get("Host") or ""
    if hosts is not None and split_host(host_header) not in hosts:
        log.warning("Rejected Host header %r; add it with --allowed-host if it is legitimate", host_header)
        raise Rejected(403, "host_not_allowed", "Host not allowed")

    site = (headers.get("Sec-Fetch-Site") or "").lower()
    navigation = method == "GET" and (headers.get("Sec-Fetch-Mode") or "").lower() == "navigate"
    if site and site not in ("same-origin", "none") and not navigation:  # links to the UI still work
        raise Rejected(403, "cross_site", "Cross-site request blocked")

    if method != "POST":
        return
    origin = headers.get("Origin")
    if origin is not None:
        if urlsplit(origin).netloc.lower() != host_header.strip().lower():
            raise Rejected(403, "cross_site", "Cross-origin request blocked")
    ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        raise Rejected(415, "bad_content_type", "Content-Type must be application/json")


def parse_json(raw: bytes):
    try:
        return json.loads(raw or b"{}")
    except (UnicodeDecodeError, ValueError):
        raise Rejected(400, "bad_json", "Request body is not valid JSON") from None


def body_length(headers) -> int:
    if headers.get("Transfer-Encoding"):
        raise Rejected(411, "length_required", "Chunked requests are not supported")
    raw = headers.get("Content-Length") or "0"
    try:
        n = int(raw)
    except ValueError:
        raise Rejected(400, "bad_request", "Invalid Content-Length") from None
    if n < 0:
        raise Rejected(400, "bad_request", "Invalid Content-Length")
    if n > MAX_BODY_BYTES:
        raise Rejected(413, "too_large", "Request body too large")
    return n


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
}
