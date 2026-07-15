"""Shared WebUI constants and pure helpers."""

from __future__ import annotations

import argparse
import logging
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("grok_sulfide.webui")
WEB_ROOT = ROOT / "webui"
CONFIG_FILE = ROOT / "config.json"
CONFIG_EXAMPLE = ROOT / "config.example.json"
CLI_FILE = ROOT / "register_cli.py"
MAX_BODY_BYTES = 1024 * 1024
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")
USER_CODE_PATTERN = re.compile(r"(?i)(user_code(?:=|/))([A-Z0-9-]+)")
SECRET_LOG_PATTERN = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|sso|bearer)\b(\s*[:=]\s*)([^\s,;]+)"
)
LONG_HEX_SECRET_PATTERN = re.compile(r"\b[A-Fa-f0-9]{24,}\b")

# Loopback token + Host validation (DNS-rebinding / open-bind hardening).
WEBUI_TOKEN = ""
WEBUI_HOST = "127.0.0.1"
WEBUI_PORT = 8765
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

STATIC_ROUTES = {
    "/": WEB_ROOT / "index.html",
    "/index.html": WEB_ROOT / "index.html",
    "/assets/app.js": WEB_ROOT / "app.js",
    "/assets/styles.css": WEB_ROOT / "styles.css",
}

EDITABLE_CONFIG = {
    "registration_method": "choice",
    "protocol_email_provider": "choice",
    "email_provider": "choice",
    "hotmail_accounts_file": "local_path",
    "hotmail_alias_mode": "choice",
    "hotmail_alias_random_length": "int",
    "hotmail_alias_random_max_attempts": "int",
    "hotmail_max_aliases_per_account": "int",
    "hotmail_poll_interval": "int",
    "hotmail_recent_seconds": "int",
    "hotmail_imap_last_n": "int",
    "hotmail_require_recipient_match": "bool",
    "duckmail_api_key": "secret",
    "tempmail_api_key": "secret",
    "tempmail_base_url": "str",
    "tempmail_prefix": "str",
    "tempmail_proxy": "str",
    "yyds_api_key": "secret",
    "yyds_jwt": "secret",
    "cloudflare_api_base": "str",
    "cloudflare_api_key": "secret",
    "cloudmail_url": "str",
    "cloudmail_admin_email": "str",
    "cloudmail_password": "secret",
    "proxy": "str",
    "email_proxy": "str",
    "cpa_proxy": "str",
    "register_threads": "int",
    "cpa_export_enabled": "bool",
    "cpa_auth_dir": "local_path",
    "cpa_base_url": "str",
    "cpa_management_upload_enabled": "bool",
    "cpa_management_base": "str",
    "cpa_management_key": "secret",
    "cpa_ssh_upload_enabled": "bool",
    "cpa_ssh_host": "str",
    "cpa_ssh_auth_dir": "str",
    "cpa_ssh_chmod": "choice",
    "cpa_ssh_timeout_sec": "int",
    "cpa_headless": "bool",
    "cpa_force_standalone": "bool",
    "cpa_probe_after_write": "bool",
    "cpa_probe_chat": "bool",
    "cpa_mint_workers": "int",
    "cpa_mint_browser_recycle_every": "int",
    "protocol_moemail_base_url": "str",
    "protocol_moemail_api_key": "secret",
    "protocol_moemail_domain": "str",
    "protocol_moemail_expiry_ms": "int",
    "protocol_yescaptcha_key": "secret",
    "protocol_yescaptcha_endpoint": "str",
    "protocol_yescaptcha_timeout_sec": "int",
    "protocol_mail_timeout_sec": "int",
    "protocol_oauth_timeout_sec": "int",
    "protocol_proxy": "str",
}

CHOICES = {
    "registration_method": {"browser", "protocol"},
    "protocol_email_provider": {
        "outlook",
        "moemail",
        "duckmail",
        "yyds",
        "cloudflare",
        "cloudmail",
        "tempmail",
        "tempmail.lol",
    },
    "email_provider": {
        "hotmail",
        "outlook",
        "duckmail",
        "yyds",
        "cloudmail",
        "cloudflare",
        "tempmail",
        "tempmail.lol",
    },
    "hotmail_alias_mode": {"primary", "random", "sequential"},
    "cpa_ssh_chmod": {"600", "640", "644"},
}

PRESET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

INT_RANGES = {
    "hotmail_alias_random_length": (1, 32),
    "hotmail_alias_random_max_attempts": (10, 5000),
    "hotmail_max_aliases_per_account": (1, 1000),
    "hotmail_poll_interval": (1, 120),
    "hotmail_recent_seconds": (60, 86400),
    "hotmail_imap_last_n": (1, 500),
    "register_threads": (1, 10),
    "cpa_mint_workers": (-1, 10),
    "cpa_mint_browser_recycle_every": (1, 1000),
    "cpa_ssh_timeout_sec": (5, 300),
    "protocol_moemail_expiry_ms": (0, 259200000),
    "protocol_yescaptcha_timeout_sec": (30, 600),
    "protocol_mail_timeout_sec": (30, 600),
    "protocol_oauth_timeout_sec": (30, 600),
}


def utc_iso(timestamp: float | None = None) -> str:
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path.name}")
    return value


def load_runtime_config() -> dict[str, Any]:
    base = load_json_file(CONFIG_EXAMPLE)
    if CONFIG_FILE.exists():
        base.update(load_json_file(CONFIG_FILE))
    return base


def local_path(value: str, *, default_name: str) -> Path:
    raw = str(value or default_name).strip() or default_name
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"Path must stay inside {ROOT.name}: {raw}") from exc
    return resolved


def relative_local_path(value: str, *, default_name: str) -> str:
    return local_path(value, default_name=default_name).relative_to(ROOT).as_posix()


def count_nonempty_lines(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    try:
        with path.open(encoding="utf-8-sig", errors="ignore") as handle:
            return sum(
                1
                for line in handle
                if line.strip() and not line.lstrip().startswith(("#", "//"))
            )
    except OSError:
        return 0


def mask_email(value: str) -> str:
    email = str(value or "").strip()
    if "@" not in email:
        return "***"
    local, domain = email.rsplit("@", 1)
    primary, separator, alias = local.partition("+")

    def mask_part(part: str) -> str:
        if len(part) <= 2:
            return part[:1] + "*"
        if len(part) <= 4:
            return part[:2] + "*" * (len(part) - 2)
        return part[:2] + "***" + part[-2:]

    masked = mask_part(primary)
    if separator:
        masked += "+" + mask_part(alias)
    return f"{masked}@{domain}"


def redact_log_text(value: str) -> str:
    text = EMAIL_PATTERN.sub(lambda match: mask_email(match.group(0)), value)
    text = USER_CODE_PATTERN.sub(r"\1***", text)
    text = SECRET_LOG_PATTERN.sub(r"\1\2***", text)
    return LONG_HEX_SECRET_PATTERN.sub("***", text)


