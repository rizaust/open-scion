"""HTTP limits and loopback address classification."""

from __future__ import annotations

import ipaddress
import re
import socket


MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_INFLIGHT_BYTES = 64 * 1024 * 1024
MAX_URL_BYTES = 8192
REQUEST_TIMEOUT = 15
MAX_RESULT_LIMIT = 1000
_OBJECT_ID = re.compile(r"[0-9a-f]{64}\Z")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        if host.lower() != "localhost":
            return False
        try:
            addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return False
        return bool(addresses) and all(
            ipaddress.ip_address(item[4][0]).is_loopback for item in addresses
        )
