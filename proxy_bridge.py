"""Local authenticated HTTP proxy bridge for Chromium."""
from __future__ import annotations

import atexit
import base64
import select
import socket
import socketserver
import threading
from urllib.parse import unquote, urlparse

_proxy_bridge_cache: dict = {}
_proxy_bridge_lock = threading.Lock()


class AuthenticatedProxyBridge(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class AuthenticatedProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(20)
        initial = b""
        while b"\r\n\r\n" not in initial and len(initial) < 65536:
            chunk = client.recv(8192)
            if not chunk:
                return
            initial += chunk
        if b"\r\n\r\n" not in initial:
            return

        header, body = initial.split(b"\r\n\r\n", 1)
        lines = header.split(b"\r\n")
        filtered = [
            line for line in lines if not line.lower().startswith(b"proxy-authorization:")
        ]
        filtered.insert(1, self.server.proxy_auth_header)

        upstream = socket.create_connection(self.server.upstream_address, timeout=15)
        try:
            upstream.settimeout(None)
            client.settimeout(None)
            upstream.sendall(b"\r\n".join(filtered) + b"\r\n\r\n" + body)
            sockets = [client, upstream]
            while True:
                readable, _, exceptional = select.select(sockets, [], sockets, 60)
                if exceptional or not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is client else client
                    target.sendall(data)
        finally:
            upstream.close()


def cleanup_proxy_bridges() -> None:
    for server, _thread in list(_proxy_bridge_cache.values()):
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


atexit.register(cleanup_proxy_bridges)


def stop_authenticated_proxy_bridge(proxy_url: str) -> None:
    with _proxy_bridge_lock:
        cached = _proxy_bridge_cache.pop(proxy_url, None)
    if not cached:
        return
    server, _thread = cached
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass


def start_authenticated_proxy_bridge(proxy_url: str) -> str:
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    if not parsed.hostname or parsed.username is None:
        return proxy_url
    if (parsed.scheme or "http").lower() != "http":
        raise ValueError("Chromium 认证代理转发目前仅支持 http:// 上游")
    username = unquote(parsed.username)
    password = unquote(parsed.password or "")
    port = parsed.port or 80
    with _proxy_bridge_lock:
        cached = _proxy_bridge_cache.get(proxy_url)
        if cached:
            server, thread = cached
            if thread.is_alive():
                return f"http://127.0.0.1:{server.server_address[1]}"

        server = AuthenticatedProxyBridge(("127.0.0.1", 0), AuthenticatedProxyHandler)
        server.upstream_address = (parsed.hostname, port)
        credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        server.proxy_auth_header = f"Proxy-Authorization: Basic {credentials}".encode("ascii")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _proxy_bridge_cache[proxy_url] = (server, thread)
        return f"http://127.0.0.1:{server.server_address[1]}"


# Back-compat private names used by ttk historically.
_AuthenticatedProxyBridge = AuthenticatedProxyBridge
_AuthenticatedProxyHandler = AuthenticatedProxyHandler
_cleanup_proxy_bridges = cleanup_proxy_bridges
_stop_authenticated_proxy_bridge = stop_authenticated_proxy_bridge
_start_authenticated_proxy_bridge = start_authenticated_proxy_bridge
