"""HTTP requests that try IPv4 before IPv6.

On networks where IPv6 resolves but does not route, the standard library waits out a
full timeout on every IPv6 address before trying IPv4. Trying IPv4 first, with a short
connect timeout per address, keeps calls to Gemini and the brain fast everywhere.
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.parse
from typing import Optional

CONNECT_TIMEOUT = 6.0


class HttpError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def _connect(host: str, port: int, timeout: float) -> socket.socket:
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda info: info[0] != socket.AF_INET)  # IPv4 first
    last: Optional[OSError] = None
    for family, kind, proto, _name, address in infos:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(min(timeout, CONNECT_TIMEOUT))
        try:
            sock.connect(address)
            sock.settimeout(timeout)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise last or OSError(f"could not resolve {host}")


class _V4FirstHTTPS(http.client.HTTPSConnection):
    def connect(self) -> None:
        raw = _connect(self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _V4FirstHTTP(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _connect(self.host, self.port, self.timeout)


def request(method: str, url: str, body: Optional[dict] = None, headers: Optional[dict] = None,
            timeout: float = 60.0):
    """Send JSON, get parsed JSON back (None for an empty reply). Raises HttpError on a 4xx/5xx."""
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme == "https"
    port = parts.port or (443 if secure else 80)
    if secure:
        connection = _V4FirstHTTPS(parts.hostname, port, timeout=timeout, context=ssl.create_default_context())
    else:
        connection = _V4FirstHTTP(parts.hostname, port, timeout=timeout)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    data = None if body is None else json.dumps(body).encode()
    send_headers = {"Content-Type": "application/json", **(headers or {})}
    try:
        connection.request(method, path, body=data, headers=send_headers)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    text = raw.decode("utf-8", errors="replace")
    if response.status >= 400:
        raise HttpError(response.status, text)
    return json.loads(text) if text.strip() else None
