"""Email / account ledger with SQLite (WAL) backend + optional text dual-write."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("grok_sulfide.store")


def _resolve_ledger_root() -> str:
    """Prefer explicit GROK_LEDGER_ROOT, else package dir, with worktree-friendly fallback.

    When running inside a git worktree that has no local emails_used.txt but the
    main checkout does, reuse the main checkout ledger so historical used/error
    state is not lost.
    """
    env = (os.environ.get("GROK_LEDGER_ROOT") or "").strip()
    if env:
        return os.path.abspath(env)

    here = os.path.dirname(os.path.abspath(__file__))
    local_used = os.path.join(here, "emails_used.txt")
    if os.path.exists(local_used):
        return here

    # Common layout: <repo>/.claude/worktrees/<name>/...
    parts = os.path.normpath(here).split(os.sep)
    if ".claude" in parts and "worktrees" in parts:
        try:
            idx = parts.index(".claude")
            main_root = os.sep.join(parts[:idx]) if idx > 0 else here
            # On Windows drive parts may be like 'D:', join carefully
            if os.name == "nt" and parts and parts[0].endswith(":"):
                main_root = parts[0] + os.sep + os.sep.join(parts[1:idx])
            candidate = os.path.join(main_root, "emails_used.txt")
            if os.path.exists(candidate):
                logger.info("ledger root fallback to main checkout: %s", main_root)
                return main_root
        except Exception:
            logger.debug("ledger root fallback failed", exc_info=True)
    return here


_ROOT = _resolve_ledger_root()
_EMAILS_USED_FILE = os.path.join(_ROOT, "emails_used.txt")
_EMAILS_ERROR_FILE = os.path.join(_ROOT, "emails_error.txt")
_ACCOUNTS_FILE = os.path.join(_ROOT, "accounts_cli.txt")
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.db")
_email_track_lock = threading.Lock()
# RLock: _upsert_usage/_conn both take the lock (schema init + writes).
_db_lock = threading.RLock()
_db_initialized = False

# Optional side-channel sync (hotmail bridge / protocol tracker).
_mark_used_hook: Callable[[str, str], Any] | None = None
_mark_error_hook: Callable[[str, str, str], Any] | None = None

# Keep writing legacy text files during transition (safe for external tools).
DUAL_WRITE_TEXT = True

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS email_usage (
    email      TEXT PRIMARY KEY,
    password   TEXT,
    status     TEXT NOT NULL CHECK (status IN ('used','error')),
    reason     TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    email      TEXT PRIMARY KEY,
    password   TEXT,
    sso        TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_usage_status ON email_usage(status);
"""


def set_db_path(path: str | Path) -> None:
    """Override DB path (tests). Resets init flag."""
    global _DB_PATH, _db_initialized
    _DB_PATH = str(path)
    _db_initialized = False


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


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _conn() -> sqlite3.Connection:
    global _db_initialized
    # Serialize first-time schema init to avoid multi-thread CREATE races.
    with _db_lock:
        conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not _db_initialized:
            conn.executescript(_SCHEMA)
            _db_initialized = True
        return conn


def _upsert_usage(email: str, password: str, status: str, reason: str = "") -> None:
    email_key = email.strip().lower()
    with _db_lock:
        conn = _conn()
        try:
            conn.execute(
                """
                INSERT INTO email_usage(email, password, status, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    password=excluded.password,
                    status=excluded.status,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (email_key, password or "", status, reason or "", _now()),
            )
            conn.commit()
        finally:
            conn.close()


def mark_used(email: str, password: str = "") -> None:
    """记录成功注册的邮箱，防止重复使用。"""
    if DUAL_WRITE_TEXT:
        with _email_track_lock:
            with open(_EMAILS_USED_FILE, "a", encoding="utf-8") as f:
                f.write(f"{email}----{password}----ok\n")
    try:
        _upsert_usage(email, password, "used", "ok")
    except Exception:
        logger.exception("sqlite mark_used failed for %s", email)
    try:
        if _mark_used_hook is not None:
            _mark_used_hook(email, password)
    except Exception:
        logger.debug("mark_used hook failed", exc_info=True)


def mark_error(email: str, password: str = "", reason: str = "") -> None:
    """记录失败邮箱及原因，避免重试烂邮箱。"""
    email = (email or "").strip()
    if not email or "@" not in email:
        return
    reason = (reason or "").replace("\n", " ").replace("\r", " ").strip()[:200]
    if DUAL_WRITE_TEXT:
        with _email_track_lock:
            with open(_EMAILS_ERROR_FILE, "a", encoding="utf-8") as f:
                f.write(f"{email}----{password}----{reason}\n")
    try:
        _upsert_usage(email, password, "error", reason)
    except Exception:
        logger.exception("sqlite mark_error failed for %s", email)
    try:
        if _mark_error_hook is not None:
            _mark_error_hook(email, password, reason)
    except Exception:
        logger.debug("mark_error hook failed", exc_info=True)


def is_email_used(email: str) -> bool:
    """检查邮箱是否已被使用或标记为失败。"""
    email_lower = (email or "").strip().lower()
    if not email_lower:
        return False
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM email_usage WHERE email = ? LIMIT 1",
                (email_lower,),
            ).fetchone()
            if row:
                return True
        finally:
            conn.close()
    except Exception:
        logger.debug("sqlite is_email_used failed; falling back to text", exc_info=True)

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


def record_account(email: str, password: str = "", sso: str = "") -> None:
    """Upsert a successful account row (SSO)."""
    email_key = (email or "").strip().lower()
    if not email_key or "@" not in email_key:
        return
    with _db_lock:
        conn = _conn()
        try:
            conn.execute(
                """
                INSERT INTO accounts(email, password, sso, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    password=excluded.password,
                    sso=excluded.sso
                """,
                (email_key, password or "", sso or "", _now()),
            )
            conn.commit()
        finally:
            conn.close()


def collect_local_consumed_emails(config: dict | None = None) -> set[str]:
    """Emails already consumed by used/error/accounts/cpa files/db."""
    found: set[str] = set()
    try:
        conn = _conn()
        try:
            for row in conn.execute("SELECT email FROM email_usage"):
                found.add(str(row["email"]).lower())
            for row in conn.execute("SELECT email FROM accounts"):
                email = str(row["email"]).lower()
                found.add(email)
                if "+" in email.split("@", 1)[0]:
                    local, domain = email.split("@", 1)
                    found.add(f"{local.split('+', 1)[0]}@{domain}")
        finally:
            conn.close()
    except Exception:
        logger.debug("sqlite collect failed; using text fallback", exc_info=True)

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
            logger.debug("text ledger read failed: %s", fpath, exc_info=True)

    accounts_file = _ACCOUNTS_FILE
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
            logger.debug("accounts_cli read failed", exc_info=True)

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
            logger.debug("cpa dir scan failed", exc_info=True)
    return found


def migrate_text_ledgers_into_sqlite() -> dict[str, int]:
    """Import emails_used/error + accounts_cli into SQLite (idempotent)."""
    stats = {"used": 0, "error": 0, "accounts": 0}

    def _iter(path: str):
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                yield line.split("----")

    with _db_lock:
        conn = _conn()
        try:
            for parts in _iter(_EMAILS_USED_FILE) or []:
                if not parts or "@" not in parts[0]:
                    continue
                email = parts[0].strip().lower()
                password = parts[1] if len(parts) > 1 else ""
                conn.execute(
                    """
                    INSERT INTO email_usage(email, password, status, reason, updated_at)
                    VALUES (?, ?, 'used', 'ok', ?)
                    ON CONFLICT(email) DO NOTHING
                    """,
                    (email, password, _now()),
                )
                stats["used"] += 1
            for parts in _iter(_EMAILS_ERROR_FILE) or []:
                if not parts or "@" not in parts[0]:
                    continue
                email = parts[0].strip().lower()
                password = parts[1] if len(parts) > 1 else ""
                reason = parts[2] if len(parts) > 2 else ""
                conn.execute(
                    """
                    INSERT INTO email_usage(email, password, status, reason, updated_at)
                    VALUES (?, ?, 'error', ?, ?)
                    ON CONFLICT(email) DO NOTHING
                    """,
                    (email, password, reason[:200], _now()),
                )
                stats["error"] += 1
            for parts in _iter(_ACCOUNTS_FILE) or []:
                if not parts or "@" not in parts[0]:
                    continue
                email = parts[0].strip().lower()
                password = parts[1] if len(parts) > 1 else ""
                sso = parts[2] if len(parts) > 2 else ""
                conn.execute(
                    """
                    INSERT INTO accounts(email, password, sso, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(email) DO NOTHING
                    """,
                    (email, password, sso, _now()),
                )
                stats["accounts"] += 1
            conn.commit()
        finally:
            conn.close()
    return stats


# Back-compat alias used by ttk internals.
_collect_local_consumed_emails = collect_local_consumed_emails  # type: ignore[assignment]
