"""Local WebUI for the standalone Grok registrar."""

from __future__ import annotations

import argparse
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
    "protocol_email_provider": {"outlook", "moemail", "duckmail", "yyds", "cloudflare", "cloudmail"},
    "email_provider": {"hotmail", "outlook", "duckmail", "yyds", "cloudmail", "cloudflare"},
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


class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._logs: deque[dict[str, Any]] = deque(maxlen=5000)
        self._next_log_id = 1
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._exit_code: int | None = None
        self._command: list[str] = []
        self._accounts_file = ROOT / "accounts_cli.txt"

    def _append_log(self, text: str, kind: str = "output") -> None:
        clean = redact_log_text(text.rstrip("\r\n"))
        if not clean:
            return
        if kind == "output" and clean.startswith("[CPA]"):
            if "[FAIL]" in clean:
                kind = "error"
            elif "[OK]" in clean:
                kind = "success"
            elif "[START]" in clean or "[WARN]" in clean:
                kind = "system"
        with self._lock:
            item = {
                "id": self._next_log_id,
                "time": utc_iso(),
                "kind": kind,
                "text": clean,
            }
            self._next_log_id += 1
            self._logs.append(item)

    def _refresh_locked(self) -> None:
        process = self._process
        if process is None:
            return
        code = process.poll()
        if code is not None and self._exit_code is None:
            self._exit_code = code
            self._ended_at = time.time()

    def start(self, options: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A registration task is already running")

            mode = str(options.get("mode", "extra")).strip().lower()
            if mode not in {"extra", "count"}:
                raise ValueError("mode must be extra or count")
            amount = int(options.get("amount", 1))
            if mode == "extra" and not 1 <= amount <= 10000:
                raise ValueError("extra amount must be between 1 and 10000")
            if mode == "count" and not 0 <= amount <= 100000:
                raise ValueError("target count must be between 0 and 100000")

            threads = max(1, min(int(options.get("threads", 1)), 10))
            alias_enabled = bool(options.get("alias_enabled", False))
            alias_limit = max(1, min(int(options.get("alias_limit", 1)), 1000))
            preset_id = str(options.get("preset_id") or "").strip()
            runtime_config = load_runtime_config()
            preset_values: dict[str, Any] = {}
            if preset_id:
                presets = runtime_config.get("registration_presets") or {}
                preset = presets.get(preset_id) if isinstance(presets, dict) else None
                if preset_id == "default" and not presets:
                    preset_values = runtime_config
                elif not isinstance(preset, dict):
                    raise ValueError("Selected registration preset does not exist")
                else:
                    preset_values = preset.get("values") if isinstance(preset.get("values"), dict) else {}
            registration_method = str(
                preset_values.get("registration_method")
                or options.get("registration_method", "browser")
            ).strip().lower()
            if registration_method not in {"browser", "protocol"}:
                raise ValueError("registration_method must be browser or protocol")
            mint_workers = max(-1, min(int(options.get("mint_workers", -1)), 10))
            recycle_every = max(1, min(int(options.get("browser_recycle_every", 25)), 1000))
            accounts_path = local_path(
                str(options.get("accounts_file", "accounts_cli.txt")),
                default_name="accounts_cli.txt",
            )
            self._accounts_file = accounts_path

            command = [
                sys.executable,
                "-u",
                str(CLI_FILE),
                f"--{mode}",
                str(amount),
                "--threads",
                str(threads),
                "--alias-mode",
                "random" if alias_enabled else "primary",
                "--alias-limit",
                str(alias_limit),
                "--registration-method",
                registration_method,
                "--mint-workers",
                str(mint_workers),
                "--browser-recycle-every",
                str(recycle_every),
                "--accounts-file",
                str(accounts_path),
            ]
            if preset_id:
                command.extend(["--preset", preset_id])
            if not bool(options.get("fast", True)):
                command.append("--no-fast")
            if bool(options.get("no_browser_reuse", False)):
                command.append("--no-browser-reuse")
            if bool(options.get("cookie_snapshot", False)):
                command.append("--cookie-snapshot")
            if bool(options.get("inline_mint", False)):
                command.append("--inline-mint")

            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            creationflags = 0
            start_new_session = False
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                start_new_session = True

            self._append_log("Starting registration task", "system")
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
            self._process = process
            self._started_at = time.time()
            self._ended_at = None
            self._exit_code = None
            self._command = command
            self._reader_thread = threading.Thread(
                target=self._read_output,
                args=(process,),
                daemon=True,
                name="webui-cli-output",
            )
            self._reader_thread.start()
            return self.status()

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        try:
            stream = process.stdout
            if stream is not None:
                for line in stream:
                    self._append_log(line)
        except Exception as exc:
            self._append_log(f"Output reader failed: {exc}", "error")
        finally:
            code = process.wait()
            with self._lock:
                if self._process is process:
                    self._exit_code = code
                    self._ended_at = time.time()
            kind = "success" if code == 0 else "error"
            self._append_log(f"Registration task exited with code {code}", kind)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            process = self._process
            if process is None or process.poll() is not None:
                return self.status()
            self._append_log("Stopping registration task", "system")

        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=5)
        except Exception:
            pass

        if process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        with self._lock:
            self._refresh_locked()
            return self.status()

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()

    def logs_after(self, after: int) -> dict[str, Any]:
        with self._lock:
            items = [item for item in self._logs if int(item["id"]) > after]
            cursor = self._logs[-1]["id"] if self._logs else after
            return {"items": items, "cursor": cursor}

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            process = self._process
            running = process is not None and process.poll() is None
            return {
                "running": running,
                "pid": process.pid if running and process is not None else None,
                "started_at": utc_iso(self._started_at) if self._started_at else None,
                "ended_at": utc_iso(self._ended_at) if self._ended_at else None,
                "exit_code": None if running else self._exit_code,
                "command": list(self._command),
                "accounts_file": self._accounts_file.relative_to(ROOT).as_posix(),
            }

    @property
    def accounts_file(self) -> Path:
        with self._lock:
            return self._accounts_file


MANAGER = ProcessManager()


def mailbox_status(config: dict[str, Any]) -> dict[str, Any]:
    try:
        path = local_path(
            str(config.get("hotmail_accounts_file", "mail_credentials.txt")),
            default_name="mail_credentials.txt",
        )
        return {
            "path": path.relative_to(ROOT).as_posix(),
            "exists": path.exists(),
            "count": count_nonempty_lines(path),
        }
    except ValueError:
        return {"path": "outside project", "exists": False, "count": 0}


def cpa_status(config: dict[str, Any]) -> dict[str, Any]:
    try:
        directory = local_path(
            str(config.get("cpa_auth_dir", "./cpa_auths")),
            default_name="cpa_auths",
        )
    except ValueError:
        return {"path": "outside project", "count": 0}
    count = sum(1 for _ in directory.glob("xai-*.json")) if directory.exists() else 0
    return {"path": directory.relative_to(ROOT).as_posix(), "count": count}


def resolve_preset_values(config: dict[str, Any], preset_id: str = "") -> dict[str, Any]:
    values = dict(config)
    presets = config.get("registration_presets")
    if preset_id and isinstance(presets, dict):
        preset = presets.get(preset_id)
        if isinstance(preset, dict) and isinstance(preset.get("values"), dict):
            values.update(preset["values"])
    return values


def _primary_email(address: str) -> str:
    value = str(address or "").strip().lower()
    if "@" not in value:
        return value
    local, domain = value.rsplit("@", 1)
    return f"{local.split('+', 1)[0]}@{domain}"


def _tracked_mail_addresses() -> set[str]:
    tracked: set[str] = set()
    for name in ("emails_used.txt", "emails_error.txt"):
        path = ROOT / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            value = line.strip()
            if not value or value.startswith(("#", "//")):
                continue
            email = value.split("----", 1)[0].strip().lower()
            if "@" in email:
                tracked.add(email)
    return tracked


def outlook_inventory(
    config: dict[str, Any],
    *,
    alias_enabled: bool,
    alias_limit: int,
) -> dict[str, Any]:
    path = local_path(
        str(config.get("hotmail_accounts_file") or "mail_credentials.txt"),
        default_name="mail_credentials.txt",
    )
    accounts: list[str] = []
    seen: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            value = line.strip()
            if not value or value.startswith(("#", "//")):
                continue
            email = value.split("----", 1)[0].strip().lower()
            primary = _primary_email(email)
            if "@" in primary and primary not in seen:
                seen.add(primary)
                accounts.append(primary)

    tracked = _tracked_mail_addresses()
    used_primary = {_primary_email(address) for address in tracked if "+" not in address.split("@", 1)[0]}
    alias_counts: dict[str, int] = {}
    for address in tracked:
        local = address.split("@", 1)[0]
        if "+" in local:
            primary = _primary_email(address)
            alias_counts[primary] = alias_counts.get(primary, 0) + 1

    items = []
    primary_available = 0
    alias_capacity = 0
    for primary in accounts:
        aliases_used = alias_counts.get(primary, 0)
        primary_is_used = primary in used_primary
        if alias_enabled:
            remaining = max(0, alias_limit - aliases_used)
            if remaining <= 0:
                continue
            alias_capacity += remaining
            items.append(
                {
                    "email": mask_email(primary),
                    "primary_used": primary_is_used,
                    "aliases_used": aliases_used,
                    "remaining": remaining,
                }
            )
        elif not primary_is_used:
            primary_available += 1
            items.append(
                {
                    "email": mask_email(primary),
                    "primary_used": False,
                    "aliases_used": aliases_used,
                    "remaining": 1,
                }
            )
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "mode": "alias" if alias_enabled else "primary",
        "alias_limit": alias_limit,
        "mailboxes": len(accounts),
        "available_mailboxes": len(items),
        "primary_available": primary_available if not alias_enabled else 0,
        "alias_capacity": alias_capacity if alias_enabled else 0,
        "items": items,
    }


def account_summary(path: Path, config: dict[str, Any], limit: int = 12) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total = 0
    if path.exists():
        try:
            with path.open(encoding="utf-8-sig", errors="ignore") as handle:
                parsed: list[tuple[str, int]] = []
                for index, line in enumerate(handle, 1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    total += 1
                    parsed.append((stripped.split("----", 1)[0].strip(), index))
                for email, index in parsed[-max(1, min(limit, 50)) :]:
                    rows.append(
                        {
                            "index": index,
                            "email": mask_email(email),
                            "has_cpa": False,
                        }
                    )
        except OSError:
            pass
    cpa = cpa_status(config)
    cpa_dir = ROOT / cpa["path"] if cpa["path"] != "outside project" else None
    if cpa_dir is not None and cpa_dir.exists():
        cpa_names: set[str] = set()
        cpa_emails: set[str] = set()
        for item in cpa_dir.glob("xai-*.json"):
            cpa_names.add(item.name.lower())
            try:
                payload = json.loads(item.read_text(encoding="utf-8"))
                email = str(payload.get("email") or "").strip().lower()
                if email:
                    cpa_emails.add(email)
            except (OSError, ValueError, TypeError):
                pass
        if path.exists():
            try:
                with path.open(encoding="utf-8-sig", errors="ignore") as handle:
                    emails = [
                        line.strip().split("----", 1)[0].strip()
                        for line in handle
                        if line.strip() and not line.lstrip().startswith("#")
                    ][-len(rows) :]
                for row, email in zip(rows, emails):
                    normalized = email.strip().lower()
                    row["has_cpa"] = (
                        normalized in cpa_emails
                        or f"xai-{normalized}.json" in cpa_names
                    )
            except OSError:
                pass
    return {
        "total": total,
        "path": path.relative_to(ROOT).as_posix(),
        "items": rows,
    }


def public_config() -> dict[str, Any]:
    config = load_runtime_config()
    presets_raw = config.get("registration_presets")
    presets = presets_raw if isinstance(presets_raw, dict) else {}
    if not presets:
        presets = {
            "default": {
                "name": "默认配置",
                "values": {key: config.get(key) for key in EDITABLE_CONFIG if key in config},
            }
        }
    active_id = str(config.get("active_registration_preset") or "").strip()
    if active_id not in presets and presets:
        active_id = next(iter(presets))
    active_values = dict(config)
    active_preset = presets.get(active_id) if active_id else None
    if isinstance(active_preset, dict) and isinstance(active_preset.get("values"), dict):
        active_values.update(active_preset["values"])
    secret_keys = {key for key, kind in EDITABLE_CONFIG.items() if kind == "secret"}
    values = {key: active_values.get(key) for key in EDITABLE_CONFIG if key not in secret_keys}
    for key in secret_keys:
        values[key] = ""
    public_presets = []
    for preset_id, preset in presets.items():
        if not isinstance(preset, dict):
            continue
        preset_values = dict(config)
        if isinstance(preset.get("values"), dict):
            preset_values.update(preset["values"])
        safe_values = {
            key: preset_values.get(key)
            for key in EDITABLE_CONFIG
            if key not in secret_keys
        }
        for key in secret_keys:
            safe_values[key] = ""
        public_presets.append(
            {
                "id": preset_id,
                "name": str(preset.get("name") or preset_id),
                "values": safe_values,
                "secrets": {key: bool(preset_values.get(key)) for key in secret_keys},
                "mailbox": mailbox_status(preset_values),
            }
        )
    return {
        "exists": CONFIG_FILE.exists(),
        "active_preset_id": active_id,
        "presets": public_presets,
        "values": values,
        "secrets": {key: bool(active_values.get(key)) for key in secret_keys},
        "mailbox": mailbox_status(active_values),
        "cpa": cpa_status(active_values),
    }


def normalize_config_updates(payload: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    updates = payload.get("values", payload)
    if not isinstance(updates, dict):
        raise ValueError("values must be an object")
    result: dict[str, Any] = {}
    for key, value in updates.items():
        kind = EDITABLE_CONFIG.get(key)
        if kind is None:
            continue
        if kind == "bool":
            result[key] = bool(value)
        elif kind == "int":
            parsed = int(value)
            low, high = INT_RANGES.get(key, (-1000000, 1000000))
            if not low <= parsed <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            result[key] = parsed
        elif kind == "choice":
            parsed = str(value or "").strip().lower()
            if parsed not in CHOICES[key]:
                raise ValueError(f"Unsupported value for {key}")
            result[key] = parsed
        elif kind == "local_path":
            default = "mail_credentials.txt" if key == "hotmail_accounts_file" else "cpa_auths"
            result[key] = relative_local_path(str(value or default), default_name=default)
        elif kind == "secret":
            parsed = str(value or "").strip()
            if parsed:
                result[key] = parsed
        else:
            parsed = str(value or "").strip()
            if len(parsed) > 4096:
                raise ValueError(f"{key} is too long")
            result[key] = parsed

    clear_secrets = payload.get("clear_secrets", [])
    if isinstance(clear_secrets, list):
        for key in clear_secrets:
            if EDITABLE_CONFIG.get(str(key)) == "secret":
                result[str(key)] = ""

    merged = dict(current)
    merged.update(result)
    merged["api_reverse_tools"] = ""
    return merged


def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_runtime_config()
    delete_id = str(payload.get("delete_preset_id") or "").strip()
    if delete_id:
        presets = current.get("registration_presets")
        if not isinstance(presets, dict) or delete_id not in presets:
            raise ValueError("Preset does not exist")
        if len(presets) <= 1:
            raise ValueError("At least one registration preset is required")
        presets.pop(delete_id)
        active_id = str(current.get("active_registration_preset") or "")
        if active_id == delete_id:
            active_id = next(reversed(presets))
            current["active_registration_preset"] = active_id
            active = presets[active_id]
            if isinstance(active, dict) and isinstance(active.get("values"), dict):
                current.update(active["values"])
        temp = CONFIG_FILE.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, CONFIG_FILE)
        return public_config()

    preset_id = str(payload.get("preset_id") or "").strip()
    if preset_id:
        if not PRESET_ID_PATTERN.fullmatch(preset_id):
            raise ValueError("Invalid preset id")
        preset_name = str(payload.get("preset_name") or preset_id).strip()[:80] or preset_id
        presets = current.get("registration_presets")
        if not isinstance(presets, dict):
            presets = {}
            current["registration_presets"] = presets
        existing = presets.get(preset_id)
        existing_values = existing.get("values") if isinstance(existing, dict) else {}
        effective = dict(current)
        if isinstance(existing_values, dict):
            effective.update(existing_values)
        normalized = normalize_config_updates(payload, effective)
        preset_values = {
            key: normalized.get(key)
            for key in EDITABLE_CONFIG
            if key in normalized
        }
        presets[preset_id] = {"name": preset_name, "values": preset_values}
        current["active_registration_preset"] = preset_id
        current.update(preset_values)
        current["api_reverse_tools"] = ""
        temp = CONFIG_FILE.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, CONFIG_FILE)
        return public_config()

    merged = normalize_config_updates(payload, current)
    temp = CONFIG_FILE.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, CONFIG_FILE)
    return public_config()


def dashboard_status() -> dict[str, Any]:
    config = load_runtime_config()
    status = MANAGER.status()
    status["accounts"] = account_summary(MANAGER.accounts_file, config, limit=10)
    status["mailbox"] = mailbox_status(config)
    status["cpa"] = cpa_status(config)
    status["config_exists"] = CONFIG_FILE.exists()
    return status


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
        try:
            if parsed.path in STATIC_ROUTES:
                self._serve_static(STATIC_ROUTES[parsed.path])
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Local WebUI for grok_sulfide")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    server, actual_port = create_server(args.host, max(1, min(args.port, 65516)))
    url = f"http://{args.host}:{actual_port}/"
    print(f"grok_sulfide WebUI: {url}", flush=True)
    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
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
