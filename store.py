"""Email / account ledger (text-file backend; SQLite migration later)."""
from __future__ import annotations

import os
import threading
from typing import Any, Callable

_ROOT = os.path.dirname(os.path.abspath(__file__))
_EMAILS_USED_FILE = os.path.join(_ROOT, "emails_used.txt")
_EMAILS_ERROR_FILE = os.path.join(_ROOT, "emails_error.txt")
_email_track_lock = threading.Lock()

# Optional side-channel sync (hotmail bridge / protocol tracker).
_mark_used_hook: Callable[[str, str], Any] | None = None
_mark_error_hook: Callable[[str, str, str], Any] | None = None


def set_ledger_hooks(
    *,
    mark_used_hook: Callable[[str, str], Any] | None = None,
    mark_error_hook: Callable[[str, str, str], Any] | None = None,
) -> None:
    """Register optional post-write hooks (e.g. hotmail_provider.mark_used)."""
    global _mark_used_hook, _mark_error_hook
    if mark_used_hook is not None:
        _mark_used_hook = mark_used_hook
    if mark_error_hook is not None:
        _mark_error_hook = mark_error_hook


def mark_used(email: str, password: str = "") -> None:
    """记录成功注册的邮箱，防止重复使用。"""
    with _email_track_lock:
        with open(_EMAILS_USED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{email}----{password}----ok\n")
    try:
        if _mark_used_hook is not None:
            _mark_used_hook(email, password)
    except Exception:
        pass


def mark_error(email: str, password: str = "", reason: str = "") -> None:
    """记录失败邮箱及原因，避免重试烂邮箱。"""
    email = (email or "").strip()
    if not email or "@" not in email:
        return
    reason = (reason or "").replace("\n", " ").replace("\r", " ").strip()[:200]
    with _email_track_lock:
        with open(_EMAILS_ERROR_FILE, "a", encoding="utf-8") as f:
            f.write(f"{email}----{password}----{reason}\n")
    try:
        if _mark_error_hook is not None:
            _mark_error_hook(email, password, reason)
    except Exception:
        pass


def is_email_used(email: str) -> bool:
    """检查邮箱是否已被使用或标记为失败。"""
    email_lower = email.strip().lower()
    for fpath in (_EMAILS_USED_FILE, _EMAILS_ERROR_FILE):
        if os.path.exists(fpath):
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("----")
                        if parts and parts[0].strip().lower() == email_lower:
                            return True
    return False


def collect_local_consumed_emails(config: dict | None = None) -> set[str]:
    """Emails already consumed by used/error/accounts/cpa files."""
    found: set[str] = set()
    for fpath in (_EMAILS_USED_FILE, _EMAILS_ERROR_FILE):
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("----")
                    if parts and "@" in parts[0]:
                        found.add(parts[0].strip().lower())
        except Exception:
            pass
    accounts_file = os.path.join(_ROOT, "accounts_cli.txt")
    if os.path.exists(accounts_file):
        try:
            with open(accounts_file, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    email = line.split("----", 1)[0].strip().lower()
                    if "@" in email:
                        found.add(email)
                        if "+" in email.split("@", 1)[0]:
                            local, domain = email.split("@", 1)
                            found.add(f"{local.split('+', 1)[0]}@{domain}")
        except Exception:
            pass
    cfg = config or {}
    cpa_dir = cfg.get("cpa_auth_dir") or "./cpa_auths"
    if not os.path.isabs(cpa_dir):
        cpa_dir = os.path.join(_ROOT, cpa_dir)
    if os.path.isdir(cpa_dir):
        try:
            for name in os.listdir(cpa_dir):
                if name.startswith("xai-") and name.endswith(".json"):
                    email = name[4:-5].strip().lower()
                    if "@" in email:
                        found.add(email)
        except Exception:
            pass
    return found


# Back-compat alias used by ttk internals.
_collect_local_consumed_emails = collect_local_consumed_emails  # type: ignore[assignment]
