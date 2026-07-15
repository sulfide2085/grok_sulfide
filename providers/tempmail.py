"""Tempmail.lol disposable inbox provider.

API (https://api.tempmail.lol):
  POST /v2/inbox/create  Authorization: Bearer <key>  -> {address, token} (201)
  GET  /v2/inbox?token=  Authorization: Bearer <key>  -> {emails[], expired}
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from . import runtime

BASE_URL = "https://api.tempmail.lol"


def _cfg() -> dict:
    return runtime.config()


def get_tempmail_api_key(api_key: str | None = None) -> str:
    if api_key and str(api_key).strip():
        return str(api_key).strip()
    env = str(os.environ.get("TEMPMAIL_API_KEY") or "").strip()
    if env:
        return env
    return str(_cfg().get("tempmail_api_key") or "").strip()


def get_tempmail_base_url() -> str:
    raw = str(_cfg().get("tempmail_base_url") or BASE_URL).strip().rstrip("/")
    return raw or BASE_URL


def get_tempmail_prefix() -> str:
    return str(_cfg().get("tempmail_prefix") or "grok").strip()


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _request_proxies() -> dict[str, str] | None:
    """Prefer explicit tempmail_proxy, then registration proxy (not email_proxy=direct).

    Tempmail free tier geo-blocks CN; routing through the register proxy is usually required.
    """
    cfg = _cfg()
    for key in ("tempmail_proxy", "proxy", "protocol_proxy"):
        raw = str(cfg.get(key) or "").strip()
        if raw and raw.lower() not in {"direct", "none", "off", "disabled"}:
            return {"http": raw, "https": raw}
    return None


def create_inbox(api_key: str | None = None, prefix: str | None = None) -> tuple[str, str]:
    """Create inbox. Returns (address, token)."""
    key = get_tempmail_api_key(api_key)
    if not key:
        raise RuntimeError("tempmail_api_key / TEMPMAIL_API_KEY is required")
    base = get_tempmail_base_url()
    payload: dict[str, Any] = {}
    pref = prefix if prefix is not None else get_tempmail_prefix()
    if pref:
        payload["prefix"] = pref
    kwargs: dict[str, Any] = {
        "headers": _auth_headers(key),
        "json": payload,
        "timeout": 20,
    }
    proxies = _request_proxies()
    if proxies is not None:
        kwargs["proxies"] = proxies
    resp = runtime.http_post(f"{base}/v2/inbox/create", **kwargs)
    if int(getattr(resp, "status_code", 0) or 0) not in {200, 201}:
        raise RuntimeError(
            f"Tempmail.lol create failed: HTTP {getattr(resp, 'status_code', '?')} "
            f"{str(getattr(resp, 'text', '') or '')[:300]}"
        )
    data = resp.json() if hasattr(resp, "json") else {}
    address = str((data or {}).get("address") or "").strip()
    token = str((data or {}).get("token") or "").strip()
    if not address or not token:
        raise RuntimeError(f"Tempmail.lol create returned unexpected payload: {data!r}")
    return address, token


def fetch_emails(token: str, api_key: str | None = None) -> list[dict]:
    key = get_tempmail_api_key(api_key)
    if not key:
        raise RuntimeError("tempmail_api_key / TEMPMAIL_API_KEY is required")
    if not token:
        return []
    base = get_tempmail_base_url()
    kwargs: dict[str, Any] = {
        "headers": _auth_headers(key),
        "params": {"token": token},
        "timeout": 20,
    }
    proxies = _request_proxies()
    if proxies is not None:
        kwargs["proxies"] = proxies
    resp = runtime.http_get(f"{base}/v2/inbox", **kwargs)
    if int(getattr(resp, "status_code", 0) or 0) != 200:
        return []
    data = resp.json() if hasattr(resp, "json") else {}
    emails = (data or {}).get("emails") or []
    return emails if isinstance(emails, list) else []


def _message_text(msg: dict) -> tuple[str, str]:
    subject = str(msg.get("subject") or "")
    parts = [
        subject,
        str(msg.get("body") or ""),
        str(msg.get("html") or ""),
        str(msg.get("from") or ""),
        str(msg.get("to") or ""),
    ]
    # strip crude HTML tags from html field
    html = str(msg.get("html") or "")
    if html:
        parts.append(re.sub(r"<[^>]+>", " ", html))
    return subject, "\n".join(p for p in parts if p)


class TempmailProvider:
    name = "tempmail"
    aliases = ("tempmail", "tempmail.lol", "lol", "tempmail_lol")

    def get_email_and_token(self, api_key: str | None = None) -> tuple[str, str]:
        address, token = create_inbox(api_key=api_key)
        return address, token

    def get_oai_code(
        self,
        dev_token: str,
        email: str,
        *,
        timeout: float = 180,
        poll_interval: float = 3,
        log_callback=None,
        cancel_callback=None,
        resend_callback=None,
    ) -> str | None:
        deadline = time.time() + float(timeout or 180)
        interval = max(1.0, float(poll_interval or 3))
        seen: set[str] = set()
        next_resend = time.time() + 35
        while time.time() < deadline:
            runtime.raise_if_cancelled(cancel_callback)
            if resend_callback and time.time() >= next_resend:
                try:
                    resend_callback()
                    if log_callback:
                        log_callback("[*] 已触发重新发送验证码")
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] 触发重发验证码失败: {exc}")
                next_resend = time.time() + 35
            try:
                emails = fetch_emails(dev_token)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Tempmail 拉取邮件失败: {exc}")
                runtime.sleep_with_cancel(interval, cancel_callback)
                continue
            if log_callback:
                log_callback(f"[Debug] Tempmail 本轮邮件数量: {len(emails)}")
            for msg in emails:
                if not isinstance(msg, dict):
                    continue
                eid = (
                    f"{msg.get('from','')}:{msg.get('subject','')}:{msg.get('date','')}"
                )
                if eid in seen:
                    continue
                seen.add(eid)
                # optional recipient filter when 'to' present
                to_field = str(msg.get("to") or "").lower()
                if to_field and email and email.lower() not in to_field:
                    # some payloads use list; tolerate miss
                    if isinstance(msg.get("to"), list):
                        recips = [
                            str((t or {}).get("address") if isinstance(t, dict) else t).lower()
                            for t in msg.get("to") or []
                        ]
                        if recips and email.lower() not in recips:
                            continue
                subject, combined = _message_text(msg)
                if log_callback:
                    log_callback(f"[Debug] Tempmail 收到邮件: {subject}")
                code = runtime.extract_verification_code(combined, subject)
                if code:
                    if log_callback:
                        log_callback(f"[*] Tempmail 从邮件中提取到验证码: {code}")
                    return code
            runtime.sleep_with_cancel(interval, cancel_callback)
        raise Exception(f"Tempmail 在 {timeout}s 内未收到验证码邮件")
