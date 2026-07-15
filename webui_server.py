"""Local WebUI HTTP server for the standalone Grok registrar."""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import webui_service as svc
from webui_service import (
    CHOICES,
    EDITABLE_CONFIG,
    INT_RANGES,
    MANAGER,
    MAX_BODY_BYTES,
    PRESET_ID_PATTERN,
    ROOT,
    STATIC_ROUTES,
    WEB_ROOT,
    account_summary,
    dashboard_status,
    load_runtime_config,
    outlook_inventory,
    public_config,
    resolve_preset_values,
    resolve_static_path,
    save_config,
)

logger = logging.getLogger("grok_sulfide.webui")

# Auth globals (also mirrored for tests that patch webui_server.*)
WEBUI_TOKEN = ""
WEBUI_HOST = "127.0.0.1"
WEBUI_PORT = 8765
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Re-export service helpers for tests/back-compat
utc_iso = svc.utc_iso
load_json_file = svc.load_json_file
local_path = svc.local_path
mask_email = svc.mask_email
redact_log_text = svc.redact_log_text
mailbox_status = svc.mailbox_status
cpa_status = svc.cpa_status
ProcessManager = svc.ProcessManager

class WebUIHandler(BaseHTTPRequestHandler):
    server_version = "grok-sulfide-webui/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[webui] {self.address_string()} {fmt % args}")

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _serve_static(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".html", ".js", ".css"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        ok, reason = _authorize(self)
        if not ok:
            self._send_error_json(HTTPStatus.FORBIDDEN, reason)
            return
        try:
            static_path = resolve_static_path(parsed.path)
            if static_path is not None:
                self._serve_static(static_path)
                return
            if parsed.path == "/api/status":
                self._send_json({"ok": True, "data": dashboard_status()})
                return
            if parsed.path == "/api/logs":
                query = parse_qs(parsed.query)
                after = int(query.get("after", ["0"])[0])
                self._send_json({"ok": True, "data": MANAGER.logs_after(max(0, after))})
                return
            if parsed.path == "/api/config":
                self._send_json({"ok": True, "data": public_config()})
                return
            if parsed.path == "/api/accounts":
                config = load_runtime_config()
                self._send_json(
                    {"ok": True, "data": account_summary(MANAGER.accounts_file, config, limit=30)}
                )
                return
            if parsed.path == "/api/mail-inventory":
                query = parse_qs(parsed.query)
                preset_id = str(query.get("preset_id", [""])[0]).strip()
                alias_enabled = str(query.get("alias_enabled", ["0"])[0]).lower() in {
                    "1", "true", "yes", "on"
                }
                alias_limit = max(1, min(int(query.get("alias_limit", ["1"])[0]), 1000))
                config = resolve_preset_values(load_runtime_config(), preset_id)
                self._send_json(
                    {
                        "ok": True,
                        "data": outlook_inventory(
                            config,
                            alias_enabled=alias_enabled,
                            alias_limit=alias_limit,
                        ),
                    }
                )
                return
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        ok, reason = _authorize(self)
        if not ok:
            self._send_error_json(HTTPStatus.FORBIDDEN, reason)
            return
        if self.headers.get("X-Grok-WebUI") != "1":
            self._send_error_json(HTTPStatus.FORBIDDEN, "Missing WebUI request header")
            return
        try:
            payload = self._read_json()
            if parsed.path == "/api/start":
                data = MANAGER.start(payload)
            elif parsed.path == "/api/stop":
                data = MANAGER.stop()
            elif parsed.path == "/api/config":
                if MANAGER.status()["running"]:
                    raise RuntimeError("Stop the registration task before saving config")
                data = save_config(payload)
            elif parsed.path == "/api/logs/clear":
                MANAGER.clear_logs()
                data = {"cleared": True}
            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                return
            self._send_json({"ok": True, "data": data})
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))


def create_server(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    last_error: OSError | None = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), WebUIHandler)
            server.daemon_threads = True
            return server, candidate
        except OSError as exc:
            last_error = exc
    raise OSError(f"Could not bind ports {port}-{port + 19}: {last_error}")


def _host_is_allowed(host_header: str) -> bool:
    raw = (host_header or "").strip().lower()
    if not raw:
        return False
    # Strip port: "127.0.0.1:8765" / "[::1]:8765"
    if raw.startswith("["):
        host_only = raw.split("]", 1)[0] + "]"
    else:
        host_only = raw.rsplit(":", 1)[0]
    if host_only in _LOOPBACK_HOSTS:
        return True
    # Allow the exact bind host when non-loopback was intentionally chosen.
    bind = (WEBUI_HOST or "").strip().lower()
    return bool(bind) and host_only == bind


def _token_from_request(handler: BaseHTTPRequestHandler) -> str:
    header = handler.headers.get("X-Grok-WebUI-Token") or handler.headers.get("X-WebUI-Token") or ""
    if header.strip():
        return header.strip()
    parsed = urlparse(handler.path)
    qs = parse_qs(parsed.query)
    for key in ("token", "access_token"):
        vals = qs.get(key) or []
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    return ""


def _authorize(handler: BaseHTTPRequestHandler) -> tuple[bool, str]:
    if not _host_is_allowed(handler.headers.get("Host", "")):
        return False, "Invalid Host header"
    if not WEBUI_TOKEN:
        return True, ""
    # Static assets are allowed without token once Host is valid (token is injected into HTML).
    path = urlparse(handler.path).path
    if path in STATIC_ROUTES or path.startswith("/assets/"):
        return True, ""
    provided = _token_from_request(handler)
    if provided != WEBUI_TOKEN:
        return False, "Missing or invalid WebUI token"
    return True, ""


def main() -> int:
    try:
        import logging_setup

        logging_setup.init()
    except Exception:
        logger.debug("suppressed exception", exc_info=True)

    parser = argparse.ArgumentParser(description="Local WebUI for grok_sulfide")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument(
        "--token",
        default="",
        help="Require this token on API calls (header X-Grok-WebUI-Token or ?token=). "
        "Empty = auto-generate a random token.",
    )
    parser.add_argument(
        "--no-token",
        action="store_true",
        help="Disable token auth (not recommended; still validates Host).",
    )
    args = parser.parse_args()

    global WEBUI_TOKEN, WEBUI_HOST, WEBUI_PORT
    WEBUI_HOST = args.host
    if args.no_token:
        WEBUI_TOKEN = ""
    else:
        import secrets

        WEBUI_TOKEN = (args.token or "").strip() or secrets.token_urlsafe(24)

    server, actual_port = create_server(args.host, max(1, min(args.port, 65516)))
    WEBUI_PORT = actual_port
    url = f"http://{args.host}:{actual_port}/"
    if WEBUI_TOKEN:
        url_with_token = f"{url}?token={WEBUI_TOKEN}"
        print(f"grok_sulfide WebUI: {url_with_token}", flush=True)
        print(f"WebUI token: {WEBUI_TOKEN}", flush=True)
        open_url = url_with_token
    else:
        print(f"grok_sulfide WebUI: {url}", flush=True)
        print("WebUI token auth: DISABLED (--no-token)", flush=True)
        open_url = url
    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(open_url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

