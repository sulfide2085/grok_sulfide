from __future__ import annotations

import os
import re
import secrets
import string
import threading
import time

from . import runtime
from .common import generate_username, pick_list_payload


def _cfg():
    return runtime.config()


class _ConfigProxy:
    def get(self, *a, **k):
        return _cfg().get(*a, **k)
    def __getitem__(self, k):
        return _cfg()[k]
    def __setitem__(self, k, v):
        _cfg()[k] = v


config = _ConfigProxy()


def http_get(url, **kwargs):
    return runtime.http_get(url, **kwargs)


def http_post(url, **kwargs):
    return runtime.http_post(url, **kwargs)


def raise_if_cancelled(cancel_callback=None):
    return runtime.raise_if_cancelled(cancel_callback)


def sleep_with_cancel(seconds, cancel_callback=None):
    return runtime.sleep_with_cancel(seconds, cancel_callback)


def extract_verification_code(text, subject=""):
    return runtime.extract_verification_code(text, subject)


def is_email_used(email: str) -> bool:
    return runtime.is_email_used(email)


def _sync_store_hooks():
    return runtime.sync_store_hooks()


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"

def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 没有返回任何可用域名")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 无已验证域名可用")

def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 获取邮件详情失败: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")



class DuckMailProvider:
    name = "duckmail"
    aliases = ("duckmail",)

    def get_email_and_token(self, api_key=None):
        key = api_key or get_duckmail_api_key()
        domain = pick_domain(api_key=key)
        username = generate_username(10)
        address = f"{username}@{domain}"
        password = secrets.token_urlsafe(12)
        create_account(address, password, api_key=key, expires_in=0)
        token = get_token(address, password)
        if not token:
            raise Exception("获取 DuckMail token 失败")
        return address, token

    def get_oai_code(self, dev_token, email, *, timeout=180, poll_interval=3,
                     log_callback=None, cancel_callback=None, resend_callback=None):
        return duckmail_get_oai_code(
            dev_token, email, timeout=timeout, poll_interval=poll_interval,
            log_callback=log_callback, cancel_callback=cancel_callback,
        )
