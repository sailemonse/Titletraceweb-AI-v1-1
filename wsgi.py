"""Gunicorn WSGI adapter for the TitleTrace HTTP handler.

The application was intentionally kept on Python's standard HTTP server so the
service has very few dependencies. This adapter lets Render run that same
application under Gunicorn without changing the existing routes.
"""
from __future__ import annotations

import io
import socket
from typing import Callable, Iterable

from server import Handler


def application(environ, start_response):
    """Expose the existing BaseHTTPRequestHandler app as a WSGI callable."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"
    query = environ.get("QUERY_STRING", "")
    target = path + ("?" + query if query else "")

    body = environ.get("wsgi.input", io.BytesIO()).read()
    host = environ.get("HTTP_HOST", "localhost")
    http_version = environ.get("SERVER_PROTOCOL", "HTTP/1.1").replace("HTTP/", "")

    headers = []
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-")
            headers.append(f"{name}: {value}")
    if "CONTENT_TYPE" in environ:
        headers.append(f"Content-Type: {environ['CONTENT_TYPE']}")
    if "CONTENT_LENGTH" in environ:
        headers.append(f"Content-Length: {environ['CONTENT_LENGTH']}")
    if not any(h.lower().startswith("host:") for h in headers):
        headers.append(f"Host: {host}")

    raw = (
        f"{method} {target} HTTP/{http_version}\r\n"
        + "\r\n".join(headers)
        + "\r\n\r\n"
    ).encode("latin-1") + body

    left, right = socket.socketpair()
    try:
        left.sendall(raw)
        left.shutdown(socket.SHUT_WR)

        # BaseHTTPRequestHandler writes its complete HTTP response to this socket.
        # A small fake server object supplies the attributes it expects.
        class FakeServer:
            server_name = host.split(":", 1)[0]
            server_port = int(environ.get("SERVER_PORT") or 8000)

        Handler(right, (environ.get("REMOTE_ADDR", "127.0.0.1"), 0), FakeServer())

        chunks = []
        while True:
            chunk = left.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        response = b"".join(chunks)
    finally:
        left.close()
        right.close()

    header_end = response.find(b"\r\n\r\n")
    if header_end < 0:
        start_response("500 Internal Server Error", [("Content-Type", "text/plain")])
        return [b"Invalid upstream response"]

    head = response[:header_end].decode("latin-1")
    payload = response[header_end + 4 :]
    lines = head.split("\r\n")
    status_line = lines[0].split(" ", 2)
    status = f"{status_line[1]} {status_line[2]}" if len(status_line) >= 3 else "500 Internal Server Error"
    response_headers = []
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            response_headers.append((name.strip(), value.strip()))

    start_response(status, response_headers)
    return [payload]


app = application
