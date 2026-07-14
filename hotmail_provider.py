"""Standalone Hotmail/Outlook OAuth2 + IMAP provider.

The registrar injects its request, cancellation, and code-extraction helpers via
``configure()``. This keeps the provider reusable without importing another
registrar project or duplicating the browser registration flow.
"""

from __future__ import annotations

import email
import logging
import html
import imaplib
import os
import re
import secrets
import string
import threading
import time
from datetime import timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

logger = logging.getLogger("grok_sulfide.hotmail")
from typing import Any, Callable


TOKEN_ENDPOINTS = [
    (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        {"scope": "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"},
    ),
    (
        "https://login.live.com/oauth20_token.srf",
        {"scope": "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"},
    ),
    ("https://login.live.com/oauth20_token.srf", {}),
    (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        {
            "scope": (
                "offline_access https://graph.microsoft.com/Mail.Read "
                "https://graph.microsoft.com/User.Read"
            )
        },
    ),
]

config: dict[str, Any] = {}
_project_dir = os.path.dirname(os.path.abspath(__file__))
_is_email_used: Callable[[str], bool] = lambda _email: False
_http_post: Callable[..., Any] | None = None
_extract_code: Callable[[str, str], str | None] | None = None
_raise_if_cancelled: Callable[[Callable[[], bool] | None], None] = lambda _cb: None
_sleep_with_cancel: Callable[[float, Callable[[], bool] | None], None] = (
    lambda seconds, _cb: time.sleep(max(0.0, seconds))
)

_accounts_cache: list[dict[str, Any]] | None = None
_accounts_mtime: float | None = None
_accounts_lock = threading.Lock()
_selection_lock = threading.Lock()
_reserved_aliases: set[str] = set()
_token_map: dict[str, dict[str, Any]] = {}
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_lock = threading.Lock()


def configure(
    runtime_config: dict[str, Any],
    *,
    is_email_used: Callable[[str], bool],
    http_post: Callable[..., Any],
    extract_verification_code: Callable[[str, str], str | None],
    raise_if_cancelled: Callable[[Callable[[], bool] | None], None],
    sleep_with_cancel: Callable[[float, Callable[[], bool] | None], None],
) -> None:
    """Bind the provider to helpers owned by the current registrar process."""
    global config, _is_email_used, _http_post, _extract_code
    global _raise_if_cancelled, _sleep_with_cancel
    config = runtime_config
    _is_email_used = is_email_used
    _http_post = http_post
    _extract_code = extract_verification_code
    _raise_if_cancelled = raise_if_cancelled
    _sleep_with_cancel = sleep_with_cancel


def mark_used(email_address: str, _password: str = "") -> None:
    _release_alias(email_address)


def mark_error(email_address: str, _password: str = "", _reason: str = "") -> None:
    _release_alias(email_address)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _resolve_path(value: Any, default_name: str = "mail_credentials.txt") -> str:
    raw = str(value or default_name).strip() or default_name
    return raw if os.path.isabs(raw) else os.path.join(_project_dir, raw)


def get_hotmail_accounts_file() -> str:
    return _resolve_path(config.get("hotmail_accounts_file"))


def _release_alias(email_address: str) -> None:
    if email_address:
        with _selection_lock:
            _reserved_aliases.discard(email_address.strip().lower())


def _parse_credential_line(line: str) -> dict[str, str] | None:
    parts = line.rstrip("\n").split("----", 3)
    if len(parts) < 4:
        return None
    email_address, password, client_id, refresh_token = (part.strip() for part in parts)
    if "@" not in email_address or not client_id or not refresh_token:
        return None
    return {
        "email": email_address,
        "password": password,
        "client_id": client_id,
        "refresh_token": refresh_token,
    }


def _load_accounts(force: bool = False) -> list[dict[str, Any]]:
    global _accounts_cache, _accounts_mtime
    path = get_hotmail_accounts_file()
    if not os.path.exists(path):
        raise RuntimeError(f"Hotmail credential file does not exist: {path}")
    mtime = os.path.getmtime(path)
    with _accounts_lock:
        if not force and _accounts_cache is not None and _accounts_mtime == mtime:
            return _accounts_cache
        accounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        with open(path, encoding="utf-8-sig") as handle:
            for line_number, raw in enumerate(handle, 1):
                stripped = raw.strip()
                if not stripped or stripped.startswith(("#", "//")):
                    continue
                item = _parse_credential_line(raw)
                if item is None:
                    print(f"[Hotmail] Skipping invalid credential line {line_number}")
                    continue
                key = item["email"].lower()
                if key in seen:
                    print(f"[Hotmail] Skipping duplicate mailbox: {item['email']}")
                    continue
                seen.add(key)
                item["line_no"] = line_number
                accounts.append(item)
        if not accounts:
            raise RuntimeError(f"No valid Hotmail credentials found in: {path}")
        _accounts_cache = accounts
        _accounts_mtime = mtime
        return accounts


def _split_email(email_address: str) -> tuple[str, str]:
    raw = str(email_address or "").strip().lower()
    return tuple(raw.rsplit("@", 1)) if "@" in raw else ("", "")


def _is_alias_of(email_address: str, main_email: str) -> bool:
    local, domain = _split_email(email_address)
    main_local, main_domain = _split_email(main_email)
    return bool(
        local
        and main_local
        and domain == main_domain
        and (local == main_local or local.startswith(main_local + "+"))
    )


def _tracked_emails() -> set[str]:
    result = set(_reserved_aliases)
    for name in ("emails_used.txt", "emails_error.txt"):
        path = os.path.join(_project_dir, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        result.add(stripped.split("----", 1)[0].strip().lower())
        except OSError:
            continue
    return result


def _consumed_count(main_email: str) -> int:
    return sum(1 for address in _tracked_emails() if _is_alias_of(address, main_email))


def _consumed_alias_count(main_email: str) -> int:
    main = main_email.strip().lower()
    return sum(
        1
        for address in _tracked_emails()
        if address.strip().lower() != main and _is_alias_of(address, main_email)
    )


def _available(email_address: str) -> bool:
    key = email_address.strip().lower()
    return bool(key and key not in _reserved_aliases and not _is_email_used(email_address))


def _random_suffix(main_local: str) -> str:
    try:
        configured = int(config.get("hotmail_alias_random_length", 8) or 8)
    except (TypeError, ValueError):
        configured = 8
    length = max(1, min(configured, max(1, 64 - len(main_local) - 1)))
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _make_alias(main_email: str, index: int, randomize: bool = False) -> str:
    if index <= 0:
        return main_email
    local, domain = main_email.split("@", 1)
    suffix = _random_suffix(local) if randomize else str(index)
    return f"{local}+{suffix}@{domain}"


def hotmail_get_email_and_token() -> tuple[str, str]:
    accounts = _load_accounts()
    try:
        alias_limit = max(1, int(config.get("hotmail_max_aliases_per_account", 1) or 1))
    except (TypeError, ValueError):
        max_aliases = 1
    alias_mode = str(config.get("hotmail_alias_mode", "primary") or "primary").lower()
    random_mode = alias_mode in {"random", "rand"}
    try:
        random_attempts = max(
            10, int(config.get("hotmail_alias_random_max_attempts", 200) or 200)
        )
    except (TypeError, ValueError):
        random_attempts = 200

    with _selection_lock:
        for account in accounts:
            main_email = account["email"].strip()
            if "@" not in main_email:
                continue
            candidate = None
            if alias_mode in {"primary", "main", "bare"}:
                candidate = main_email if _available(main_email) else None
            elif _consumed_alias_count(main_email) >= alias_limit:
                continue
            elif random_mode:
                for _ in range(random_attempts):
                    alias = _make_alias(main_email, 1, randomize=True)
                    if _available(alias):
                        candidate = alias
                        break
            else:
                for index in range(1, alias_limit + 1):
                    alias = _make_alias(main_email, index)
                    if _available(alias):
                        candidate = alias
                        break
            if candidate is None:
                continue
            _reserved_aliases.add(candidate.lower())
            token = "hotmail:" + secrets.token_urlsafe(18)
            _token_map[token] = {
                "account": account,
                "email": candidate,
                "created_at": time.time(),
            }
            return candidate, token
    raise RuntimeError(
        "No Hotmail address is available. Add credentials or clear the local email ledgers."
    )


def _refresh_lock(email_address: str) -> threading.Lock:
    key = email_address.strip().lower()
    with _refresh_locks_lock:
        return _refresh_locks.setdefault(key, threading.Lock())


def _update_refresh_token(
    email_address: str, new_refresh_token: str, log_callback: Callable[[str], None] | None
) -> None:
    global _accounts_mtime
    path = get_hotmail_accounts_file()
    if not new_refresh_token or not os.path.exists(path):
        return
    with _accounts_lock:
        try:
            with open(path, encoding="utf-8-sig") as handle:
                lines = handle.readlines()
            changed = False
            output: list[str] = []
            for raw in lines:
                item = _parse_credential_line(raw)
                if item and item["email"].lower() == email_address.lower():
                    output.append(
                        f"{item['email']}----{item['password']}----"
                        f"{item['client_id']}----{new_refresh_token}\n"
                    )
                    changed = True
                else:
                    output.append(raw)
            if changed:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.writelines(output)
                _accounts_mtime = os.path.getmtime(path)
                if log_callback:
                    log_callback(f"[*] Updated Hotmail refresh token: {email_address}")
        except OSError as exc:
            if log_callback:
                log_callback(f"[Debug] Could not update Hotmail refresh token: {exc}")


def _is_permanent_oauth_error(error: Any) -> bool:
    text = str(error or "").lower()
    markers = (
        "aadsts70000",
        "aadsts50076",
        "aadsts50079",
        "aadsts50126",
        "aadsts70008",
        "aadsts700082",
        "aadsts700084",
        "aadsts65001",
        "interaction_required",
        "invalid_grant",
        "consent_required",
        "account is locked",
        "disabled",
        "refresh token has expired",
        "token has been revoked",
    )
    return any(marker in text for marker in markers)


def hotmail_refresh_access_token(
    account: dict[str, Any], log_callback: Callable[[str], None] | None = None
) -> str:
    if _http_post is None:
        raise RuntimeError("Hotmail provider is not configured")
    email_address = account["email"]
    with _refresh_lock(email_address):
        refresh_token = account.get("refresh_token", "")
        last_error: Any = None
        for url, extra in TOKEN_ENDPOINTS:
            try:
                response = _http_post(
                    url,
                    data={
                        "client_id": account["client_id"],
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                        **extra,
                    },
                    timeout=30,
                )
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                access_token = payload.get("access_token")
                if access_token:
                    new_refresh = payload.get("refresh_token") or refresh_token
                    if new_refresh != refresh_token:
                        account["refresh_token"] = new_refresh
                        _update_refresh_token(email_address, new_refresh, log_callback)
                    if log_callback:
                        log_callback(f"[*] Refreshed Hotmail access token: {email_address}")
                    return access_token
                last_error = (
                    payload.get("error_description")
                    or payload.get("error")
                    or response.text[:200]
                )
                if _is_permanent_oauth_error(last_error):
                    break
            except Exception as exc:
                last_error = exc
                if _is_permanent_oauth_error(exc):
                    break
        raise RuntimeError(f"Hotmail OAuth2 refresh failed: {last_error}")


def _decode_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _message_body(message: Any) -> str:
    def decode_part(part: Any) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")

    if message.is_multipart():
        plain = ""
        rich = ""
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not plain:
                plain = decode_part(part)
            elif part.get_content_type() == "text/html" and not rich:
                rich = decode_part(part)
        if plain:
            return plain
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(rich))).strip()
    return decode_part(message)


def _imap_hosts() -> list[str]:
    raw = config.get(
        "hotmail_imap_hosts", "outlook.office365.com,imap-mail.outlook.com"
    )
    values = raw if isinstance(raw, (list, tuple)) else re.split(r"[,\s]+", str(raw or ""))
    result: list[str] = []
    for host in values or ["outlook.office365.com", "imap-mail.outlook.com"]:
        normalized = str(host).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _imap_get_code(
    mailbox_email: str,
    target_email: str,
    access_token: str,
    log_callback: Callable[[str], None] | None = None,
    host: str = "outlook.office365.com",
) -> str | None:
    if _extract_code is None:
        raise RuntimeError("Hotmail provider is not configured")
    try:
        recent_seconds = int(config.get("hotmail_recent_seconds", 900) or 900)
    except (TypeError, ValueError):
        recent_seconds = 900
    try:
        last_n = int(config.get("hotmail_imap_last_n", 30) or 30)
    except (TypeError, ValueError):
        last_n = 30
    require_recipient = _as_bool(
        config.get("hotmail_require_recipient_match", True), default=True
    )
    cutoff_ms = int((time.time() - max(60, recent_seconds)) * 1000)
    target = (target_email or "").strip().lower()
    keywords = ("x.ai", "xai", "grok", "verification", "code", "confirm")

    if log_callback:
        log_callback(f"[Debug] Connecting Hotmail IMAP: host={host} user={mailbox_email}")
    client = imaplib.IMAP4_SSL(host, 993, timeout=45)
    auth = f"user={mailbox_email}\x01auth=Bearer {access_token}\x01\x01"
    client.authenticate("XOAUTH2", lambda _challenge: auth.encode())
    try:
        client.select("INBOX")
        status, data = client.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return None
        for message_id in reversed(data[0].split()[-max(1, last_n) :]):
            _, message_data = client.fetch(message_id, "(RFC822)")
            if not message_data or not isinstance(message_data[0], tuple):
                continue
            raw_message = message_data[0][1]
            if not isinstance(raw_message, bytes):
                continue
            message = email.message_from_bytes(raw_message)
            date_value = message.get("Date")
            if date_value:
                try:
                    sent_at = parsedate_to_datetime(date_value)
                    if sent_at.tzinfo is None:
                        sent_at = sent_at.replace(tzinfo=timezone.utc)
                    if int(sent_at.timestamp() * 1000) < cutoff_ms:
                        continue
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
            subject = _decode_header(message.get("Subject", ""))
            sender = _decode_header(message.get("From", ""))
            recipients = " ".join(
                _decode_header(message.get(header, ""))
                for header in (
                    "To",
                    "Cc",
                    "Delivered-To",
                    "X-Original-To",
                    "Original-Recipient",
                    "Envelope-To",
                )
            ).lower()
            if require_recipient and target and target not in recipients:
                continue
            combined = f"{subject}\n{sender}\n{recipients}\n{_message_body(message)}"
            if not any(keyword in combined.lower() for keyword in keywords):
                continue
            code = _extract_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Hotmail verification code received: {code}")
                return code
        return None
    finally:
        try:
            client.close()
        except Exception:
            logger.debug("suppressed exception", exc_info=True)
        try:
            client.logout()
        except Exception:
            logger.debug("suppressed exception", exc_info=True)


def hotmail_get_oai_code(
    dev_token: str,
    email_address: str,
    timeout: int = 180,
    poll_interval: float = 3,
    log_callback: Callable[[str], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    resend_callback: Callable[[], None] | None = None,
) -> str:
    token_info = _token_map.get(dev_token)
    if token_info is None:
        raise RuntimeError("Hotmail dev token is invalid or expired")
    account = token_info["account"]
    mailbox_email = account["email"]
    try:
        configured_interval = float(config.get("hotmail_poll_interval", 5) or 5)
    except (TypeError, ValueError):
        configured_interval = 5.0
    interval = max(1.0, configured_interval or float(poll_interval or 3))
    deadline = time.time() + timeout
    access_token: str | None = None
    next_resend = time.time() + 60

    while time.time() < deadline:
        _raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] Requested a new verification code")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Verification-code resend failed: {exc}")
            next_resend = time.time() + 60
        try:
            if not access_token:
                access_token = hotmail_refresh_access_token(account, log_callback)
            host_errors: list[str] = []
            for host in _imap_hosts():
                try:
                    code = _imap_get_code(
                        mailbox_email,
                        email_address,
                        access_token,
                        log_callback=log_callback,
                        host=host,
                    )
                    if code:
                        return code
                    break
                except Exception as exc:
                    host_errors.append(f"{host}: {exc}")
                    if log_callback:
                        log_callback(f"[Debug] Hotmail IMAP host failed: {host}: {exc}")
            if host_errors and len(host_errors) >= len(_imap_hosts()):
                raise RuntimeError("; ".join(host_errors))
            if log_callback:
                log_callback(f"[Debug] No Hotmail verification code yet: {email_address}")
        except Exception as exc:
            if _is_permanent_oauth_error(exc):
                raise RuntimeError(
                    f"Hotmail OAuth requires account recovery: {mailbox_email}: {exc}"
                ) from exc
            access_token = None
            if log_callback:
                log_callback(f"[Debug] Hotmail polling failed: {exc}")
        _sleep_with_cancel(interval, cancel_callback)
    raise TimeoutError(
        f"Hotmail verification email was not received within {timeout}s: {email_address}"
    )
