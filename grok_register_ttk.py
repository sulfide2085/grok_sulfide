#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import atexit
import base64
import threading
import datetime
import time
import os
import select
import socket
import socketserver
import sys
import queue
import secrets
import struct
import random
import re
import string
import json

from DrissionPage import ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests

import store as _store
from store import mark_used, mark_error, is_email_used

import config_runtime as _cfg
from config_runtime import (
    CONFIG_FILE,
    DEFAULT_CONFIG,
    ConfigError,
    load_env,
    load_config as _load_config_impl,
    save_config as _save_config_impl,
    normalize_runtime_config as _normalize_runtime_config,
)


# Config lives in config_runtime; keep mutable `config` identity for legacy callers.
config = _cfg.config
_cf_domain_index = 0
# CloudMail 公开 token 单例（多线程共享，避免并发覆盖）
_cloudmail_public_token = None
_cloudmail_public_token_lock = threading.Lock()
_hotmail_bridge_module = None



# ── 邮箱追踪（实现见 store.py） ──

_EMAILS_USED_FILE = _store._EMAILS_USED_FILE
_EMAILS_ERROR_FILE = _store._EMAILS_ERROR_FILE
_email_track_lock = _store._email_track_lock


def _sync_store_hooks():
    """Point store side-channel hooks at hotmail bridge when loaded."""
    def _used(email, password=""):
        if _hotmail_bridge_module is not None and hasattr(_hotmail_bridge_module, "mark_used"):
            _hotmail_bridge_module.mark_used(email, password)

    def _err(email, password="", reason=""):
        if _hotmail_bridge_module is not None and hasattr(_hotmail_bridge_module, "mark_error"):
            _hotmail_bridge_module.mark_error(email, password, reason)

    _store.set_ledger_hooks(mark_used_hook=_used, mark_error_hook=_err)


def _collect_local_consumed_emails() -> set:
    return _store.collect_local_consumed_emails(config)


class EmailAlreadyRegisteredError(Exception):
    """xAI 侧该邮箱已有账户，不可再注册。"""

    def __init__(self, email: str = "", detail: str = ""):
        self.email = (email or "").strip()
        msg = detail or f"xAI 已存在账户: {self.email}"
        super().__init__(msg)


class EmailOtpRateLimitedError(Exception):
    """xAI 对该邮箱发送验证码过于频繁，需冷却后才能再试。"""

    def __init__(self, email: str = "", detail: str = ""):
        self.email = (email or "").strip()
        msg = detail or f"xAI 验证码发送过多: {self.email}"
        super().__init__(msg)


_EXISTING_ACCOUNT_MARKERS = (
    "已存在与此邮箱地址关联的账户",
    "找到现有账户",
    "请使用下方显示的登录方法登录",
    "an account already exists",
    "account already exists",
    "already associated with an account",
    "use one of the login methods",
    "existing account",
    "find your existing account",
)

# xAI / ICU: 「发送到此邮箱的验证码过多。请在 {count, plural, ...} 后重试」
_OTP_RATE_LIMIT_MARKERS = (
    "发送到此邮箱的验证码过多",
    "验证码过多",
    "发送验证码过多",
    "too many verification codes",
    "too many codes sent",
    "too many codes",
    "codes sent to this email",
    "try again later",
    "please try again in",
)


def _page_body_text(page=None, limit: int = 8000) -> str:
    page = page or _get_page()
    if page is None:
        return ""
    try:
        text = page.run_js(
            r"""
const body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
const title = document.title || '';
return (title + '\n' + body).slice(0, arguments[0]);
            """,
            int(limit),
        )
    except Exception:
        try:
            text = (page.html or "")[:limit]
        except Exception:
            text = ""
    return str(text or "")


def page_signals_existing_account(page=None) -> str:
    """若当前页是 xAI「邮箱已注册」提示，返回匹配片段；否则空串。"""
    blob = _page_body_text(page)
    if not blob:
        return ""
    lower = blob.lower()
    for marker in _EXISTING_ACCOUNT_MARKERS:
        if marker.lower() in lower or marker in blob:
            return marker
    return ""


def page_signals_otp_rate_limit(page=None) -> str:
    """若当前页是「验证码发送过多 / 请稍后重试」，返回匹配片段。"""
    blob = _page_body_text(page)
    if not blob:
        return ""
    lower = blob.lower()
    # Prefer strong Chinese markers first
    for marker in _OTP_RATE_LIMIT_MARKERS:
        if marker.lower() in lower or marker in blob:
            # Avoid false positive: generic "try again later" only with OTP context
            if marker in ("try again later", "please try again in"):
                if not any(
                    k in lower or k in blob
                    for k in (
                        "验证码",
                        "verification",
                        "code",
                        "otp",
                        "邮件",
                        "email",
                        "minute",
                        "分钟",
                    )
                ):
                    continue
            return marker
    # ICU residual in UI (unresolved plural placeholder)
    if "验证码" in blob and ("后重试" in blob or "{count" in blob or "plural" in lower):
        if "过多" in blob or "too many" in lower:
            return "otp-rate-limit-icu"
    return ""


def raise_if_existing_account(email: str = "", page=None, log_callback=None):
    """提交邮箱后检测到「已存在账户」则抛 EmailAlreadyRegisteredError。"""
    hit = page_signals_existing_account(page)
    if not hit:
        return
    if log_callback:
        log_callback(f"[!] xAI 提示邮箱已注册 ({hit}): {email}")
    raise EmailAlreadyRegisteredError(email, f"xAI 已存在账户 ({hit}): {email}")


def raise_if_otp_rate_limited(email: str = "", page=None, log_callback=None):
    """提交邮箱后检测到验证码发送限流则标记并抛 EmailOtpRateLimitedError。"""
    hit = page_signals_otp_rate_limit(page)
    if not hit:
        return
    if log_callback:
        log_callback(f"[!] xAI 验证码发送过多/限流 ({hit}): {email}")
    try:
        mark_error(email, reason=f"otp_send_rate_limit:{hit[:80]}")
    except Exception:
        pass
    raise EmailOtpRateLimitedError(
        email, f"xAI 验证码发送过多，请稍后重试 ({hit}): {email}"
    )


# ── 页面状态快照 ──

# ── CLI / batch performance knobs (register_cli may mutate) ──
PERF_FLAGS = {
    "fast": False,           # scale down human_sleep
    "sleep_scale": 1.0,      # multiply all human_sleep means
    "skip_debug_io": False,  # skip dump_state / take_screenshot
    "cookie_snapshot": True, # save_cookies_snapshot
    "async_side_effects": True,  # grok2api / cookie snapshot in background
    "browser_reuse": True,   # clear_session instead of quit between accounts
    "browser_recycle_every": 25,  # full quit+recreate after N successful reuses
}

_side_effect_pool = None


def _get_side_effect_pool():
    global _side_effect_pool
    if _side_effect_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _side_effect_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sidefx")
    return _side_effect_pool


def configure_perf(**kwargs):
    """Update PERF_FLAGS from CLI. Unknown keys ignored."""
    for k, v in kwargs.items():
        if k in PERF_FLAGS:
            PERF_FLAGS[k] = v


_SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")


def dump_state(page, tag: str = ""):
    """打印当前页面状态：URL、可见按钮文本、输入框类型。"""
    if PERF_FLAGS.get("skip_debug_io"):
        return
    try:
        info = page.run_js("""() => {
            const btns = [...document.querySelectorAll('button')]
                .map(b => b.innerText.trim())
                .filter(t => t)
                .slice(0, 20);
            const inputs = [...document.querySelectorAll('input,textarea')]
                .map(i => (i.type || 'text') + '/' + (i.placeholder || i.name || ''))
                .slice(0, 15);
            return {url: location.href, btns: btns, inputs: inputs};
        }""")
        if not info:
            print(f"  [state:{tag}] page context not ready (None)")
            return
        print(f"  [state:{tag}] url: {info.get('url', '?')}")
        print(f"  [state:{tag}] btns: {info.get('btns', [])}")
        print(f"  [state:{tag}] inputs: {info.get('inputs', [])}")
    except Exception as e:
        print(f"  [state:{tag}] dump_state err: {e}")


def take_screenshot(page, tag: str = ""):
    """捕获当前页面截图并保存到 screenshots/ 目录。"""
    if PERF_FLAGS.get("skip_debug_io"):
        return
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%H%M%S")
        path = os.path.join(_SCREENSHOT_DIR, f"{ts}_{tag}.png")
        page.get_screenshot(path=path)
        print(f"  [screenshot] saved: {path}")
    except Exception as e:
        print(f"  [screenshot] err: {e}")


# ── 超时守卫 ──

REGISTER_TIMEOUT = 180  # 单次注册总超时（秒）


class TimeoutError(Exception):
    pass


def check_timeout(start_time: float):
    """检查是否超过总超时时间。"""
    elapsed = time.time() - start_time
    if elapsed > REGISTER_TIMEOUT:
        raise TimeoutError(f"注册超时 ({REGISTER_TIMEOUT}s, 已用 {elapsed:.0f}s)")


# ── 全量 cookie 保存 ──

_COOKIE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies", "grok")


def save_cookies_snapshot(page, tag: str = "", email: str = ""):
    """保存当前浏览器上下文的全量 cookie 快照。"""
    if not PERF_FLAGS.get("cookie_snapshot", True):
        return
    try:
        browser = _get_browser()
        if not browser:
            return
        os.makedirs(_COOKIE_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        cookies = browser.cookies()
        data = {
            "ts": ts,
            "tag": tag,
            "email": email,
            "url": page.url if page else "",
            "cookies": cookies,
        }
        path = os.path.join(_COOKIE_DIR, f"full_{ts}_{tag}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  [cookies] saved: {path} ({len(cookies)} cookies)")
    except Exception as e:
        print(f"  [cookies] save err: {e}")


# ── .env 加载 ──


def load_env():
    _cfg.load_env()


class RegistrationCancelled(Exception):
    pass


def load_config():
    """Load into the shared config dict (same object identity as config_runtime.config)."""
    loaded = _load_config_impl()
    # Ensure module-level `config` name stays bound to the shared dict.
    global config
    config = _cfg.config
    # Also re-export so `from grok_register_ttk import config` remains live.
    return config


def save_config():
    global config
    config = _cfg.config
    _save_config_impl()


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


ensure_stable_python_runtime()
warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "turnstilePatch")
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_proxies():
    proxy = get_registration_proxy()
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


_registration_proxy_tls = threading.local()


def get_registration_proxy():
    runtime = getattr(_registration_proxy_tls, "proxy", None)
    if runtime:
        return runtime
    return str(config.get("proxy", "") or "").strip()


def get_email_proxy():
    """Return the email API proxy; empty inherits the registration proxy."""
    value = str(config.get("email_proxy", "") or "").strip()
    if value.lower() in {"direct", "none", "off", "disabled"}:
        return ""
    return value or get_registration_proxy()


def _build_resin_sticky_proxy(proxy_url, account):
    from urllib.parse import quote, unquote, urlparse, urlunparse

    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    if not parsed.hostname or parsed.username is None:
        return proxy_url
    platform = unquote(parsed.username).split(".", 1)[0].strip() or "Default"
    password = unquote(parsed.password or "")
    sticky_username = f"{platform}.{account}"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    credentials = f"{quote(sticky_username, safe='')}:{quote(password, safe='')}@"
    return urlunparse((parsed.scheme or "http", f"{credentials}{host}{port}", "", "", "", ""))


def begin_registration_proxy_session(label=""):
    base_proxy = str(config.get("proxy", "") or "").strip()
    old_proxy = getattr(_registration_proxy_tls, "proxy", "")
    if old_proxy and old_proxy != base_proxy:
        _stop_authenticated_proxy_bridge(old_proxy)

    if not base_proxy or not config.get("resin_sticky_enabled", False):
        _registration_proxy_tls.proxy = base_proxy
        _registration_proxy_tls.account = ""
        return ""

    prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", str(config.get("resin_account_prefix", "grok") or "grok"))
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "_", str(label or "").strip())
    random_part = secrets.token_hex(4)
    account = "_".join(part for part in (prefix, suffix, random_part) if part)
    runtime_proxy = _build_resin_sticky_proxy(base_proxy, account)
    _registration_proxy_tls.proxy = runtime_proxy
    _registration_proxy_tls.account = account
    return account


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "bearer") or "bearer").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-custom-auth":
            headers["x-custom-auth"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email: POST new_address -> {address,jwt}."""
    global _cf_domain_index
    path = get_cloudflare_path("cloudflare_path_accounts", "/api/new_address")
    url = f"{api_base}{path}"
    payload = {}
    try:
        # 在多个域名之间轮换，降低单域偶发不收件导致的失败率
        domains = [x.strip() for x in re.split(r"[,，\s]+", str(config.get("defaultDomains", "") or "")) if x.strip()]
        if domains:
            payload["domain"] = domains[_cf_domain_index % len(domains)]
            _cf_domain_index += 1
            if path.startswith("/admin/"):
                payload["name"] = generate_username(10)
    except Exception:
        pass
    if path.startswith("/admin/") and not payload.get("domain"):
        raise Exception("Cloudflare 管理员创建邮箱需要在 defaultDomains 中配置可用域名")
    resp = http_post(
        url,
        json=payload,
        headers=cloudflare_build_headers(content_type=True),
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare /api/new_address 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare /api/new_address 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def resolve_grok2api_local_token_file():
    configured = str(config.get("grok2api_local_token_file", "") or "").strip()
    if configured:
        return configured
    return r"D:\注册机\3255d5ee6e702db9220a897df64635a1ec9df644\vendor\grok2api\data\token.json"


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip()
    if not pool_name:
        pool_name = "ssoBasic"
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    data = {}
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    pool = data.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token in existing:
        if log_callback:
            log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
        return True
    entry = {"token": token, "tags": ["auto-register"], "note": email}
    pool.append(entry)
    data[pool_name] = pool
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


def add_token_to_grok2api_remote_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    base = str(config.get("grok2api_remote_base", "") or "").strip().rstrip("/")
    app_key = str(os.environ.get("GROK2API_APP_KEY") or config.get("grok2api_remote_app_key", "") or "").strip()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    if not base or not app_key:
        if log_callback:
            log_callback("[Debug] grok2api 远端未配置 base/app_key，跳过")
        return False
    headers = {"Content-Type": "application/json"}
    query = {"app_key": app_key, "auto_nsfw": "true"}
    pool_map = {"ssoBasic": "basic", "ssoSuper": "super"}
    remote_pool = pool_map.get(pool_name, "basic")
    # 优先使用 add 接口，避免全量覆盖远端池
    try:
        add_payload = {"tokens": [token], "pool": remote_pool, "tags": ["auto-register"]}
        resp_add = http_post(
            f"{base}/tokens/add",
            headers=headers,
            params=query,
            json=add_payload,
            timeout=8,
            proxies={},
        )
        resp_add.raise_for_status()
        if log_callback:
            log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({base}/tokens/add)")
        return True
    except Exception as add_exc:
        if log_callback:
            log_callback(f"[Debug] /tokens/add 写入失败，尝试 /tokens 全量模式: {add_exc}")

    # 兜底：旧版全量保存接口
    current = {}
    try:
        resp = http_get(f"{base}/tokens", headers=headers, params=query, timeout=6, proxies={})
        if resp.status_code == 200:
            payload = resp.json()
            current = payload.get("tokens", {}) if isinstance(payload, dict) else {}
    except Exception:
        current = {}
    if not isinstance(current, dict):
        current = {}
    pool = current.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token not in existing:
        pool.append({"token": token, "tags": ["auto-register"], "note": email})
    current[pool_name] = pool
    resp2 = http_post(f"{base}/tokens", headers=headers, params=query, json=current, timeout=8, proxies={})
    resp2.raise_for_status()
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({base}/tokens)")
    return True


def _add_token_to_grok2api_pools_sync(raw_token, email="", log_callback=None):
    # SSO 账本只写 accounts_cli.txt；不再本地备份 tokens/grok/
    if config.get("grok2api_auto_add_local", True):
        try:
            add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
    if config.get("grok2api_auto_add_remote", False):
        try:
            add_token_to_grok2api_remote_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 远端池失败: {exc}")


def add_token_to_grok2api_pools(raw_token, email="", log_callback=None):
    """Push SSO into grok2api pools. Async by default so register path never blocks on dead :8000."""
    if PERF_FLAGS.get("async_side_effects", True):
        def _job():
            try:
                _add_token_to_grok2api_pools_sync(raw_token, email=email, log_callback=log_callback)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] grok2api side-effect 异常: {exc}")
        try:
            _get_side_effect_pool().submit(_job)
            if log_callback:
                log_callback("[*] grok2api 池写入已异步提交")
            return
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 异步提交失败，同步写入: {exc}")
    _add_token_to_grok2api_pools_sync(raw_token, email=email, log_callback=log_callback)


CHROMIUM_SLIM_FLAGS = [
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-images",
    "--mute-audio",
    "--disable-background-networking",
    "--no-first-run",
]


_BROWSER_PROXY_UNSET = object()
_proxy_bridge_cache = {}
_proxy_bridge_lock = threading.Lock()


class _AuthenticatedProxyBridge(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _AuthenticatedProxyHandler(socketserver.BaseRequestHandler):
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


def _cleanup_proxy_bridges():
    for server, _thread in list(_proxy_bridge_cache.values()):
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


atexit.register(_cleanup_proxy_bridges)


def _stop_authenticated_proxy_bridge(proxy_url):
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


def _start_authenticated_proxy_bridge(proxy_url):
    from urllib.parse import unquote, urlparse

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

        server = _AuthenticatedProxyBridge(("127.0.0.1", 0), _AuthenticatedProxyHandler)
        server.upstream_address = (parsed.hostname, port)
        credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        server.proxy_auth_header = f"Proxy-Authorization: Basic {credentials}".encode("ascii")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _proxy_bridge_cache[proxy_url] = (server, thread)
        return f"http://127.0.0.1:{server.server_address[1]}"


def create_browser_options(proxy_override=_BROWSER_PROXY_UNSET):
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    for flag in CHROMIUM_SLIM_FLAGS:
        options.set_argument(flag)
    if os.path.exists(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)
    # Apply config.json "proxy" to Chromium. Without this, only HTTP helpers
    # used get_proxies(); the browser itself fell through to system/env proxy.
    if proxy_override is _BROWSER_PROXY_UNSET:
        proxy = get_registration_proxy()
    else:
        proxy = str(proxy_override or "").strip()
    if proxy:
        try:
            from urllib.parse import urlparse

            u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
            host = u.hostname or ""
            if host:
                port = u.port or (443 if (u.scheme or "http") == "https" else 80)
                scheme = u.scheme or "http"
                if u.username is not None:
                    browser_proxy = _start_authenticated_proxy_bridge(proxy)
                    options.set_argument(f"--proxy-server={browser_proxy}")
                    print(f"  [proxy] Chromium auth bridge -> {host}:{port}")
                else:
                    options.set_argument(f"--proxy-server={scheme}://{host}:{port}")
        except Exception as e:
            print(f"  [proxy] set browser proxy failed: {e}")
    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxy = get_email_proxy()
        proxies = {"http": proxy, "https": proxy} if proxy else {}
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def http_get(url, **kwargs):
    try:
        return requests.get(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        # 代理不可用时自动回退为直连，避免整个流程直接失败
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.get(url, **_build_request_kwargs(**retry_kwargs))
        raise


def http_post(url, **kwargs):
    try:
        return requests.post(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.post(url, **_build_request_kwargs(**retry_kwargs))
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("鐢ㄦ埛鍋滄娉ㄥ唽")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def human_sleep(mean_seconds, cancel_callback=None):
    """高斯分布人类化延迟，sigma=mean*0.3，clamp [mean*0.5, mean*2.0]。

    PERF_FLAGS sleep_scale / fast 可压缩批量注册等待。
    """
    scale = float(PERF_FLAGS.get("sleep_scale", 1.0) or 1.0)
    if PERF_FLAGS.get("fast"):
        scale = min(scale, 0.15)
    mean_seconds = max(0.0, float(mean_seconds) * scale)
    if mean_seconds <= 0.01:
        raise_if_cancelled(cancel_callback)
        return
    try:
        delay = random.gauss(mean_seconds, mean_seconds * 0.3)
    except Exception:
        delay = mean_seconds
    delay = max(mean_seconds * 0.5, min(mean_seconds * 2.0, delay))
    sleep_with_cancel(delay, cancel_callback)



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


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 创建邮箱失败: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 获取token失败: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 获取邮件详情失败: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("获取 YYDS token 失败")
    print(f"[*] 宸插垱寤?YYDS 閭: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 获取邮件详情失败: {exc}")
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
                log_callback(f"[Debug] YYDS 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 鏃犲凡楠岃瘉鍩熷悕鍙敤")


# ──────────────────────── CloudMail (maillab/cloud-mail) ────────────────────────
# API 前缀: /api/（所有接口均挂载在 /api/ 下）
# 认证格式: Authorization: <token>（不带 Bearer 前缀）
# 公开 token 通过 /api/public/genToken 获取（需管理员账号）

def get_cloudmail_url():
    return str(os.environ.get("CLOUDMAIL_URL") or config.get("cloudmail_url", "") or "").rstrip("/")


def get_cloudmail_password():
    return os.environ.get("CLOUDMAIL_PASSWORD") or config.get("cloudmail_password", "")


def get_cloudmail_admin_email():
    return str(os.environ.get("CLOUDMAIL_ADMIN_EMAIL") or config.get("cloudmail_admin_email", "") or "").strip()


def cloudmail_login(url, email, password):
    """POST /api/login -> JWT string"""
    resp = http_post(
        f"{url}/api/login",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") == 200:
        token_data = data.get("data", {})
        if isinstance(token_data, dict):
            jwt = token_data.get("token")
            if jwt:
                return jwt
    raise Exception(f"CloudMail 登录失败: {str(data)[:200]}")


def cloudmail_register(url, email, password, turnstile_token=""):
    """POST /api/register -> 注册用户+账号"""
    payload = {"email": email, "password": password}
    if turnstile_token:
        payload["token"] = turnstile_token
    resp = http_post(
        f"{url}/api/register",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") != 200:
        raise Exception(f"CloudMail 注册失败: {data.get('message', str(data))}")
    return data


def cloudmail_gen_public_token(url, admin_email, admin_password):
    """POST /api/public/genToken -> 公开 API token (UUID)"""
    resp = http_post(
        f"{url}/api/public/genToken",
        json={"email": admin_email, "password": admin_password},
        headers={"Content-Type": "application/json"},
        proxies={},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") == 200:
        token_data = data.get("data", {})
        if isinstance(token_data, dict):
            return token_data.get("token")
    raise Exception(f"CloudMail 获取公开 token 失败: {str(data)[:200]}")


def cloudmail_public_email_list(url, public_token, to_email="", size=20):
    """POST /api/public/emailList -> 公开邮件查询（需公开 token，Authorization: <token>）"""
    payload = {"size": size}
    if to_email:
        payload["toEmail"] = to_email
    resp = http_post(
        f"{url}/api/public/emailList",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": public_token,
        },
        proxies={},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("code") == 200:
            return data.get("data", [])
        raise Exception(f"CloudMail 邮件查询失败: {data.get('message', str(data))}")
    return []


def _cloudmail_get_shared_token(force_refresh=False):
    """获取或刷新共享的公开 token（线程安全单例）"""
    global _cloudmail_public_token
    with _cloudmail_public_token_lock:
        if _cloudmail_public_token and not force_refresh:
            return _cloudmail_public_token
        url = get_cloudmail_url()
        admin_email = get_cloudmail_admin_email()
        admin_password = get_cloudmail_password()
        if not url or not admin_email or not admin_password:
            raise Exception("CloudMail 配置不完整")
        token = cloudmail_gen_public_token(url, admin_email, admin_password)
        if not token:
            raise Exception("CloudMail 公开 token 为空")
        _cloudmail_public_token = token
        return token


def cloudmail_get_oai_code(
    dev_token,
    email,
    timeout=300,
    poll_interval=None,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    # 使用配置的 mail_poll_interval，默认 0.3s
    if poll_interval is None:
        poll_interval = max(0.1, float(config.get("mail_poll_interval", 0.3) or 0.3))
    url = get_cloudmail_url()
    if not url:
        raise Exception("CloudMail URL 未配置")
    # 获取共享公开 token（所有线程共用同一个，避免并发覆盖）
    try:
        public_token = _cloudmail_get_shared_token()
    except Exception as exc:
        raise Exception(f"CloudMail 获取公开 token 失败: {exc}")
    if log_callback:
        log_callback("[Debug] CloudMail 公开 token 获取成功")
    deadline = time.time() + timeout
    seen_attempts = {}
    next_resend_at = time.time() + 60
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 60
        # 统一使用 poll_interval（0.3s 短轮询，无需前加速）
        current_interval = poll_interval
        # 用完整邮箱地址查询（公开 API 的 toEmail 需要完整地址）
        try:
            messages = cloudmail_public_email_list(url, public_token, to_email=email, size=20)
        except Exception as exc:
            err_msg = str(exc)
            if log_callback:
                log_callback(f"[Debug] CloudMail 邮件查询失败: {err_msg}")
            # token 失效时，刷新共享 token（加锁，多线程只刷新一次）
            if "token" in err_msg.lower() or "401" in err_msg:
                try:
                    public_token = _cloudmail_get_shared_token(force_refresh=True)
                    if log_callback:
                        log_callback("[Debug] CloudMail 公开 token 已刷新")
                except Exception:
                    pass
            sleep_with_cancel(current_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] CloudMail 本轮邮件数量: {len(messages)}")
        for msg in messages:
            msg_id = msg.get("emailId") or msg.get("id") or msg.get("messageId")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            # 提取邮件内容（公开接口返回 content 字段，为完整 HTML）
            parts = []
            for field in ("content", "text", "textContent", "text_content", "body", "snippet", "intro"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_val = msg.get("html") or msg.get("htmlContent") or msg.get("html_content")
            if isinstance(html_val, str):
                parts.append(re.sub(r"<[^>]+>", " ", html_val))
            elif isinstance(html_val, list):
                for h in html_val:
                    if isinstance(h, str):
                        parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            if log_callback:
                log_callback(f"[Debug] CloudMail 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] CloudMail 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(current_interval, cancel_callback)
    raise Exception(f"CloudMail 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── 公共邮箱工具 ────────────────────────

def get_email_provider():
    return config.get("email_provider", "hotmail")


def _hotmail_bridge():
    """Load the Hotmail provider bundled with this project."""
    global _hotmail_bridge_module
    if _hotmail_bridge_module is None:
        import hotmail_provider

        _hotmail_bridge_module = hotmail_provider
        _sync_store_hooks()
    _hotmail_bridge_module.configure(
        config,
        is_email_used=is_email_used,
        http_post=http_post,
        extract_verification_code=extract_verification_code,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
    )
    return _hotmail_bridge_module


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider in ("hotmail", "outlook", "outlookmail", "microsoft"):
        return _hotmail_bridge().hotmail_get_email_and_token()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudmail":
        # CloudMail catch-all 模式：直接生成随机邮箱，无需注册
        # Cloudflare Email Routing 会自动将所有该域名的邮件路由到 Worker
        # 支持英文逗号、中文逗号、空格分隔
        raw = str(config.get("defaultDomains", "") or "")
        domains = [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]
        if not domains:
            raise Exception("CloudMail 需要在 defaultDomains 中配置可用域名")
        global _cf_domain_index
        domain = domains[_cf_domain_index % len(domains)]
        _cf_domain_index += 1
        username = generate_username(10)
        address = f"{username}@{domain}"
        # 返回占位 token（实际不用于邮件查询，邮件查询走公开 API）
        return address, "cloudmail_catch_all"
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
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


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider in ("hotmail", "outlook", "outlookmail", "microsoft"):
        return _hotmail_bridge().hotmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudmail":
        return cloudmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


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
                log_callback(f"[Debug] 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def enable_nsfw_for_token(token, cf_clearance="", log_callback=None):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": f"sso={token}; sso-rw={token}; cf_clearance={cf_clearance}",
                }
            )
            ok, msg = set_tos_accepted(session, log_callback)
            if not ok:
                return False, msg or "set_tos_accepted failed"
            ok, msg = set_birth_date(session, log_callback)
            if not ok:
                return False, msg or "set_birth_date failed"
            ok, msg = update_nsfw_settings(session, log_callback)
            if not ok:
                return False, msg or "update_nsfw_settings failed"
            return True, "NSFW enabled"
    except Exception as e:
        return False, f"exception: {e}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_thread_ctx = threading.local()

from tab_pool import TabPool


def _get_browser():
    return TabPool.get_browser()


def _set_browser(value):
    pass  # TabPool 管理 browser，外部 setter 为 no-op


def _get_page():
    if TabPool.get_browser() is None:
        return None
    return TabPool.get_tab()


def _set_page(value):
    pass  # TabPool 管理 tab，外部 setter 为 no-op


def start_browser(log_callback=None):
    last_exc = None
    for attempt in range(1, 5):
        try:
            TabPool.init(create_browser_options, log_callback=log_callback)
            page = TabPool.get_tab()
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return TabPool.get_browser(), page
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            # 每线程独立浏览器，shutdown 只影响当前线程
            try:
                TabPool.release_tab()
            except Exception:
                pass
            human_sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    """Quit current-thread Chromium (full process exit + del_data)."""
    TabPool.release_tab()


def prepare_browser_for_next_account(log_callback=None, force_recycle: bool = False):
    """Between accounts: clear session (reuse) or full recycle.

    Returns (browser, page).
    """
    reuse = bool(PERF_FLAGS.get("browser_reuse", True)) and not force_recycle
    every = int(PERF_FLAGS.get("browser_recycle_every", 25) or 25)
    served = TabPool.served_count()
    if reuse and TabPool.get_browser() is not None and (every <= 0 or served < every):
        if TabPool.clear_session(log_callback=log_callback):
            TabPool.mark_served()
            return TabPool.get_browser(), _get_page()
    # full recycle
    if log_callback:
        log_callback(f"[*] 浏览器完整回收（reuse={reuse}, served={served}, every={every}）")
    TabPool.release_tab()
    return start_browser(log_callback=log_callback)


def shutdown_browser():
    """Quit all tracked Chromium instances."""
    TabPool.shutdown()


def restart_browser(log_callback=None):
    TabPool.release_tab()
    return start_browser(log_callback=log_callback)


def sync_active_page():
    """Re-bind the active tab handle without reloading (safe mid OTP/profile)."""
    if TabPool.get_browser() is None:
        restart_browser()
        return _get_page()
    try:
        browser = TabPool.get_browser()
        tabs = browser.tab_ids
        if tabs:
            browser.get_tab(tabs[-1])
        else:
            browser.new_tab()
        TabPool.sync_tab()
    except Exception:
        pass
    return _get_page()


def refresh_active_page():
    """Hard reload current tab. Avoid during OTP/profile — use sync_active_page()."""
    if TabPool.get_browser() is None:
        restart_browser()
    try:
        browser = TabPool.get_browser()
        tabs = browser.tab_ids
        if tabs:
            page = browser.get_tab(tabs[-1])
        else:
            page = browser.new_tab()
        page.refresh()
        TabPool.sync_tab()
    except Exception:
        restart_browser()
    return _get_page()


def dismiss_cookie_banner(page=None, log_callback=None) -> str:
    """Close xAI cookie consent if present. Returns which button was clicked or ''."""
    page = page or _get_page()
    if page is None:
        return ""
    try:
        hit = page.run_js(
            r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const labels = [
  '接受所有 Cookie', '接受所有Cookie', '接受全部', '全部允许', '全部接受',
  'Accept all', 'Accept All', 'Allow all', 'Allow All', 'Accept all cookies'
];
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(isVisible);
for (const label of labels) {
  const target = nodes.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    return t === label || t.includes(label);
  });
  if (target && !target.disabled) {
    target.click();
    return label;
  }
}
// close (X) on cookie dialog as fallback
const closeBtn = nodes.find((node) => {
  const aria = (node.getAttribute('aria-label') || '').toLowerCase();
  const t = (node.innerText || node.textContent || '').trim();
  return aria.includes('close') || aria.includes('关闭') || t === '×' || t === 'x' || t === 'X';
});
if (closeBtn) { closeBtn.click(); return 'close'; }
return '';
            """
        )
    except Exception:
        hit = ""
    hit = str(hit or "").strip()
    if hit and log_callback:
        log_callback(f"[*] 已关闭 Cookie 弹窗: {hit}")
    return hit


def page_has_code_input(page=None) -> bool:
    """True only when a real OTP input is visible (not chooser text / residual copy)."""
    page = page or _get_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
// Require actual input widgets. Text-only match falsely advances past spinner/chooser.
const selectors = [
  'input[data-input-otp="true"]',
  'input[autocomplete="one-time-code"]',
  'input[name="code"]',
  'input[inputmode="numeric"]',
  'input[inputmode="text"]'
];
const nodes = Array.from(document.querySelectorAll(selectors.join(','))).filter(
  (n) => isVisible(n) && !n.disabled && !n.readOnly
);
if (nodes.some((n) => Number(n.maxLength || 6) > 1 || String(n.autocomplete || '').toLowerCase() === 'one-time-code' || n.getAttribute('data-input-otp') === 'true' || String(n.name || '').toLowerCase() === 'code')) {
  return true;
}
// multi single-digit OTP boxes
const boxes = Array.from(document.querySelectorAll('input')).filter((n) => {
  if (!isVisible(n) || n.disabled || n.readOnly) return false;
  return Number(n.maxLength || 0) === 1;
});
return boxes.length >= 4;
                """
            )
        )
    except Exception:
        return False


def page_still_on_email_form(page=None) -> bool:
    page = page or _get_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const emailInput = Array.from(document.querySelectorAll(
  'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]'
)).find((n) => isVisible(n));
return !!emailInput;
                """
            )
        )
    except Exception:
        return False


def page_on_signup_chooser(page=None) -> bool:
    """True when back on '创建您的账户' method picker (email / X / Apple / Google)."""
    page = page or _get_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const t = ((document.body && (document.body.innerText || document.body.textContent)) || '');
const hasChooserText = t.includes('使用邮箱注册') && (t.includes('创建您的账户') || t.includes('创建您的帐户'));
if (!hasChooserText) return false;
// email form open = not chooser
const emailInput = Array.from(document.querySelectorAll(
  'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]'
)).some((n) => isVisible(n));
return !emailInput;
                """
            )
        )
    except Exception:
        return False


def page_email_submit_loading(page=None) -> bool:
    """Spinner / disabled primary CTA after email submit."""
    page = page or _get_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const t = ((document.body && (document.body.innerText || document.body.textContent)) || '');
const onEmailStep = t.includes('使用您的邮箱注册') || t.includes('Sign up with your email') ||
  !!document.querySelector('input[data-testid="email"], input[name="email"], input[type="email"]');
if (!onEmailStep) return false;
const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible);
const loading = buttons.some((node) => {
  if (node.disabled || node.getAttribute('aria-disabled') === 'true' || node.getAttribute('aria-busy') === 'true') {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    // spinner-only or 注册 while busy
    return text === '' || text.includes('注册') || text.toLowerCase().includes('sign');
  }
  // SVG spinner inside button
  return !!node.querySelector('svg animateTransform, svg [class*="spin"], .animate-spin, [class*="spinner"]');
});
return loading;
                """
            )
        )
    except Exception:
        return False


def wait_for_email_form(timeout=12, log_callback=None, cancel_callback=None) -> bool:
    """After clicking 使用邮箱注册, wait until email input is actually visible."""
    page = _get_page()
    deadline = time.time() + max(3.0, float(timeout))
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        dismiss_cookie_banner(page, log_callback=None)
        if page_still_on_email_form(page):
            return True
        human_sleep(0.4, cancel_callback)
    if log_callback:
        log_callback("[!] 等待邮箱输入框超时")
    return False


def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    page = _get_page()
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        dismiss_cookie_banner(page, log_callback=log_callback)
        # already on email form — no need to click again
        if page_still_on_email_form(page):
            if log_callback:
                log_callback("[*] 已在邮箱注册表单")
            return True
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(isVisible);
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text.includes('使用邮箱注册') ||
        lower.includes('signupwithemail') ||
        lower.includes('continuewithemail') ||
        (lower.includes('email') && (text.includes('注册') || lower.includes('sign')))
    );
});
if (!target) {
    return false;
}
target.scrollIntoView({block: 'center', inline: 'nearest'});
target.click();
return true;
        """)

        if clicked:
            if log_callback:
                log_callback("[*] 已点击「使用邮箱注册」按钮")
            human_sleep(1.2, cancel_callback)
            dismiss_cookie_banner(page, log_callback=log_callback)
            if wait_for_email_form(timeout=10, log_callback=log_callback, cancel_callback=cancel_callback):
                return True
            # click landed but form not painted yet — keep trying within deadline
            continue

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        human_sleep(0.8, cancel_callback)

    if log_callback:
        try:
            page_html = (page.html or "")[:500]
        except Exception:
            page_html = "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(log_callback=None, cancel_callback=None):
    browser = _get_browser()
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    if browser is None:
        browser, page = start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        page = _get_page()
        page.get(SIGNUP_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            TabPool.release_tab()
            page = _get_page()
            page.get(SIGNUP_URL)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 创建新标签页异常: {e2}")
            restart_browser()
            page = _get_page()
            page.get(SIGNUP_URL)
    page.wait.doc_loaded()
    dump_state(page, "signup-loaded")
    take_screenshot(page, "signup")
    human_sleep(1, cancel_callback)
    dismiss_cookie_banner(page, log_callback=log_callback)
    human_sleep(0.5, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )
    dismiss_cookie_banner(page, log_callback=log_callback)
    dump_state(page, "after-email-signup-click")


def has_profile_form(log_callback=None):
    # Do NOT hard-refresh here: reload during OTP/profile bounces to 「使用邮箱注册」.
    page = sync_active_page()
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def fill_email_and_submit(timeout=None, log_callback=None, cancel_callback=None):
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    check_timeout(time.time())
    dismiss_cookie_banner(page, log_callback=log_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    form_timeout = float(
        timeout
        if timeout is not None
        else config.get("email_form_timeout", 45) or 45
    )
    confirm_timeout = float(config.get("email_submit_confirm_timeout", 60) or 60)
    deadline = time.time() + max(25.0, form_timeout)
    submit_attempts = 0
    max_submit_attempts = 6
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        dismiss_cookie_banner(page, log_callback=None)
        # bounced to method chooser mid-loop
        if page_on_signup_chooser(page) and not page_still_on_email_form(page):
            if log_callback:
                log_callback("[!] 当前在注册方式页，重新进入邮箱表单")
            try:
                click_email_signup_button(
                    timeout=12, log_callback=log_callback, cancel_callback=cancel_callback
                )
            except Exception as click_exc:
                if log_callback:
                    log_callback(f"[!] 重进邮箱表单失败: {click_exc}")
                human_sleep(0.8, cancel_callback)
            continue
        if not page_still_on_email_form(page):
            if not wait_for_email_form(
                timeout=8, log_callback=log_callback, cancel_callback=cancel_callback
            ):
                human_sleep(0.5, cancel_callback)
                continue
        filled = page.run_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) return 'not-ready';
input.focus(); input.click();
// 清空并设置值
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
// 完整事件序列，确保 React 受控组件同步
input.dispatchEvent(new Event('focus', { bubbles: true }));
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('blur', { bubbles: true }));
// 验证：值已写入即可（不依赖 checkValidity，部分站点自定义校验会导致误判）
const current = (input.value || '').trim();
if (current === email) return 'filled';
// 兜底：尝试逐字符输入
input.value = '';
input.dispatchEvent(new Event('input', { bubbles: true }));
for (const ch of email) {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
    input.value += ch;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
    input.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
}
input.dispatchEvent(new Event('change', { bubbles: true }));
if ((input.value || '').trim() === email) return 'filled';
return input.value;
            """,
            email,
        )
        if filled == "not-ready":
            human_sleep(0.5, cancel_callback)
            continue
        if filled != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            human_sleep(0.5, cancel_callback)
            continue
        human_sleep(0.8, cancel_callback)
        dismiss_cookie_banner(page, log_callback=log_callback)
        # wait until primary CTA is clickable (not spinner-disabled)
        clicked = None
        for _ready in range(12):
            raise_if_cancelled(cancel_callback)
            dismiss_cookie_banner(page, log_callback=None)
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return 'no-input';
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => isVisible(node));
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        // spinner-only button with empty text near email form
        (text === '' && node.closest('form, [class*="form"], main, body'))
    );
}) || buttons.find((node) => node.type === 'submit') || null;
if (!submitButton) return 'no-button';
if (submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true' || submitButton.getAttribute('aria-busy') === 'true') return 'disabled';
submitButton.scrollIntoView({block: 'center', inline: 'nearest'});
submitButton.click();
return 'clicked';
                """
            )
            if clicked == "clicked":
                break
            if clicked == "disabled":
                dismiss_cookie_banner(page, log_callback=log_callback)
                human_sleep(0.6, cancel_callback)
                continue
            human_sleep(0.4, cancel_callback)
        if clicked != "clicked":
            human_sleep(0.5, cancel_callback)
            continue
        submit_attempts += 1
        if log_callback:
            log_callback(f"[*] 已填写邮箱并点击注册: {email} (第{submit_attempts}次)")
        dump_state(page, "email-submitted")
        take_screenshot(page, "email-submitted")
        # Wait for real navigation: code page / existing account / profile.
        # xAI often shows a long spinner then either OTP or bounces to chooser.
        advanced = False
        wait_deadline = time.time() + max(20.0, confirm_timeout)
        last_status = ""
        while time.time() < wait_deadline:
            raise_if_cancelled(cancel_callback)
            dismiss_cookie_banner(page, log_callback=None)
            try:
                raise_if_existing_account(email, page=page, log_callback=log_callback)
            except EmailAlreadyRegisteredError:
                dump_state(page, "existing-account")
                take_screenshot(page, "existing-account")
                raise
            try:
                raise_if_otp_rate_limited(email, page=page, log_callback=log_callback)
            except EmailOtpRateLimitedError:
                dump_state(page, "otp-rate-limit")
                take_screenshot(page, "otp-rate-limit")
                raise
            if has_profile_form(log_callback=None) or page_has_code_input(page):
                advanced = True
                break
            if page_email_submit_loading(page):
                status = "loading"
            elif page_still_on_email_form(page):
                status = "email-form"
            elif page_on_signup_chooser(page):
                status = "chooser"
                break
            else:
                status = "other"
            if status != last_status and log_callback and status in ("loading", "chooser"):
                log_callback(f"[*] 提交后页面状态: {status}")
                last_status = status
            human_sleep(0.6, cancel_callback)
        if advanced:
            return email, dev_token
        # Final check: rate-limit banner may appear after spinner ends
        raise_if_otp_rate_limited(email, page=page, log_callback=log_callback)
        # still loading after confirm_timeout — soft fail, retry submit if attempts left
        if page_email_submit_loading(page) or page_still_on_email_form(page):
            if submit_attempts < max_submit_attempts and time.time() < deadline:
                if log_callback:
                    log_callback(
                        f"[!] 提交后仍停在邮箱表单/加载中（{confirm_timeout:.0f}s 未进验证码），重试提交"
                    )
                take_screenshot(page, "email-submit-stuck")
                # try soft reload of form: back to chooser then re-enter
                try:
                    page.run_js(
                        r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const back = nodes.find((n) => {
  const t = (n.innerText || n.textContent || '').replace(/\s+/g, '');
  return t === '返回' || t.toLowerCase() === 'back';
});
if (back && !back.disabled) { back.click(); return true; }
return false;
                        """
                    )
                    human_sleep(1.0, cancel_callback)
                except Exception:
                    pass
                if page_on_signup_chooser(page) or not page_still_on_email_form(page):
                    try:
                        click_email_signup_button(
                            timeout=12,
                            log_callback=log_callback,
                            cancel_callback=cancel_callback,
                        )
                    except Exception:
                        pass
                human_sleep(0.5, cancel_callback)
                continue
            raise Exception(f"邮箱提交后未进入验证码页（表单卡住）: {email}")
        if page_on_signup_chooser(page):
            if submit_attempts < max_submit_attempts and time.time() < deadline:
                if log_callback:
                    log_callback("[!] 提交后回到注册方式页，重新进入邮箱表单并重试")
                take_screenshot(page, "email-submit-bounced")
                try:
                    click_email_signup_button(
                        timeout=12, log_callback=log_callback, cancel_callback=cancel_callback
                    )
                except Exception as bounce_exc:
                    if log_callback:
                        log_callback(f"[!] 重新进入邮箱表单失败: {bounce_exc}")
                human_sleep(0.5, cancel_callback)
                continue
            raise Exception(f"邮箱提交后反复回到注册方式页: {email}")
        raise Exception(f"邮箱提交后未进入验证码页: {email}")
    raise Exception("未找到邮箱输入框或注册按钮")


def page_otp_error(page=None) -> str:
    """Return visible OTP error text if present."""
    page = page or _get_page()
    if page is None:
        return ""
    try:
        hit = page.run_js(
            r"""
const t = ((document.body && (document.body.innerText || document.body.textContent)) || '');
const markers = [
  '验证码无效', '验证码错误', '无效的验证码', '代码无效', '代码不正确',
  'invalid code', 'incorrect code', 'wrong code', 'code is invalid',
  'expired', '已过期', '请重试'
];
const lower = t.toLowerCase();
for (const m of markers) {
  if (lower.includes(m.toLowerCase()) || t.includes(m)) return m;
}
return '';
            """
        )
        return str(hit or "").strip()
    except Exception:
        return ""



def _fill_otp_via_drission(page, clean_code, log_callback=None):
    """Prefer real keystrokes so React OTP state updates (JS-only often fakes success)."""
    if page is None or not clean_code:
        return ""
    selectors = [
        'css:input[data-input-otp="true"]',
        'css:input[autocomplete="one-time-code"]',
        'css:input[name="code"]',
        'css:input[inputmode="numeric"]',
        'xpath://input[@maxlength="1"]',
    ]
    try:
        # single aggregate field
        for sel in selectors[:4]:
            try:
                ele = page.ele(sel, timeout=0.6)
            except Exception:
                ele = None
            if not ele:
                continue
            try:
                ml = int(ele.attr("maxlength") or 0)
            except Exception:
                ml = 0
            if ml == 1:
                continue
            try:
                ele.clear()
            except Exception:
                pass
            try:
                ele.click()
            except Exception:
                pass
            try:
                ele.input(clean_code, clear=True)
            except TypeError:
                ele.input(clean_code)
            try:
                page.actions.key_down("ENTER").key_up("ENTER")
            except Exception:
                try:
                    ele.input("\n")
                except Exception:
                    pass
            val = str(ele.value or ele.attr("value") or "").replace(" ", "").strip()
            if val and (clean_code in val or val in clean_code or len(val) >= min(4, len(clean_code))):
                if log_callback:
                    log_callback(f"[*] Drission 写入验证码: aggregate value={val[:8]}")
                return "dp-aggregate"
        # multi-box OTP
        boxes = []
        try:
            boxes = page.eles('css:input[maxlength="1"]', timeout=0.8) or []
        except Exception:
            boxes = []
        boxes = [b for b in boxes if b]
        if len(boxes) >= len(clean_code):
            for i, ch in enumerate(clean_code):
                box = boxes[i]
                try:
                    box.click()
                except Exception:
                    pass
                try:
                    box.clear()
                except Exception:
                    pass
                try:
                    box.input(ch, clear=True)
                except TypeError:
                    box.input(ch)
            try:
                boxes[min(len(clean_code), len(boxes)) - 1].input("\n")
            except Exception:
                pass
            if log_callback:
                log_callback(f"[*] Drission 写入验证码: {len(clean_code)} boxes")
            return "dp-boxes"
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] Drission OTP 填写异常: {exc}")
    return ""



def fill_code_and_submit(email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
    page = _get_page()
    check_timeout(time.time())
    dismiss_cookie_banner(page, log_callback=log_callback)
    raise_if_existing_account(email, page=page, log_callback=log_callback)
    dump_state(page, "wait-code")
    take_screenshot(page, "wait-code")
    # Don't burn 180s polling IMAP if page never asked for a code.
    if not page_has_code_input(page) and page_still_on_email_form(page):
        raise Exception(f"未进入验证码页（仍在邮箱表单）: {email}")
    if not page_has_code_input(page):
        # brief wait — page may still be transitioning
        for _ in range(10):
            raise_if_cancelled(cancel_callback)
            dismiss_cookie_banner(page, log_callback=None)
            raise_if_existing_account(email, page=page, log_callback=log_callback)
            if page_has_code_input(page) or has_profile_form(log_callback=None):
                break
            human_sleep(0.5, cancel_callback)
        else:
            if not page_has_code_input(page):
                raise Exception(f"未进入验证码页，跳过 IMAP 空等: {email}")

    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    code = get_oai_code(
        dev_token,
        email,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout
    submit_tries = 0
    max_submit_tries = 4

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        dismiss_cookie_banner(page, log_callback=None)
        # already advanced while we were polling mail
        if has_profile_form(log_callback=None):
            if log_callback:
                log_callback("[*] 已在资料页，跳过验证码填写")
            return code
        if page_on_signup_chooser(page):
            raise Exception(f"验证码阶段页面回到注册方式页: {email}")
        if page_still_on_email_form(page) and not page_has_code_input(page):
            raise Exception(f"验证码阶段退回邮箱表单: {email}")
        if not page_has_code_input(page):
            human_sleep(0.5, cancel_callback)
            continue

        # 1) real keystrokes first (ported from gui hardening)
        filled = _fill_otp_via_drission(page, clean_code, log_callback=log_callback)
        # 2) JS fallback for stubborn React OTP widgets
        if not filled:
            filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    // many OTP UIs auto-submit on full length; also fire Enter
    aggregate.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    aggregate.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const last = otpBoxes[Math.min(code.length, otpBoxes.length) - 1];
    if (last) {
        last.focus();
        last.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
        last.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            human_sleep(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            human_sleep(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('验证') ||
        t.includes('提交') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('verify') ||
        t.includes('submit') ||
        t.includes('next')
    );
});

if (btn) {
  btn.focus();
  btn.click();
  return 'clicked';
}
// fallback: form submit or Enter on focused OTP
const form = document.querySelector('form');
if (form && typeof form.requestSubmit === 'function') {
  try { form.requestSubmit(); return 'form-submit'; } catch (e) {}
}
if (form) { try { form.submit(); return 'form-submit'; } catch (e) {} }
return 'no-button';
            """
        )

        submit_tries += 1
        if log_callback:
            log_callback(f"[*] 已填写验证码并提交: {code} ({clicked}, 第{submit_tries}次)")
        dump_state(page, "code-submitted")
        take_screenshot(page, "code-submitted")

        # CRITICAL: wait until profile form (or hard fail). Old code returned after 1.5s
        # even when page bounced back to 「使用邮箱注册」 chooser.
        confirm_deadline = time.time() + float(config.get("code_form_timeout", 45) or 45)
        if confirm_deadline > deadline:
            confirm_deadline = deadline
        advanced = False
        while time.time() < confirm_deadline:
            raise_if_cancelled(cancel_callback)
            dismiss_cookie_banner(page, log_callback=None)
            try:
                raise_if_existing_account(email, page=page, log_callback=log_callback)
            except EmailAlreadyRegisteredError:
                dump_state(page, "existing-account")
                take_screenshot(page, "existing-account")
                raise
            err = page_otp_error(page)
            if err and page_has_code_input(page):
                if log_callback:
                    log_callback(f"[!] 验证码被拒: {err}")
                take_screenshot(page, "code-rejected")
                raise Exception(f"验证码无效/被拒 ({err}): {email}")
            if has_profile_form(log_callback=None):
                advanced = True
                break
            # some flows go straight past profile (rare) — accept leave of OTP page
            if not page_has_code_input(page) and not page_still_on_email_form(page) and not page_on_signup_chooser(page):
                # maybe intermediate loading / CF / redirect
                try:
                    url = str(page.url or "")
                except Exception:
                    url = ""
                if "sign-up" not in url or "complete" in url or "profile" in url or "password" in url:
                    # still wait a bit for profile fields
                    pass
            if page_on_signup_chooser(page):
                take_screenshot(page, "code-bounced-chooser")
                raise Exception(f"验证码提交后回到注册方式页（使用邮箱注册）: {email}")
            if page_still_on_email_form(page) and not page_has_code_input(page):
                take_screenshot(page, "code-bounced-email")
                raise Exception(f"验证码提交后回到邮箱表单: {email}")
            human_sleep(0.6, cancel_callback)

        if advanced:
            if log_callback:
                log_callback("[*] 验证码通过，已进入资料页")
            take_screenshot(page, "after-code-profile")
            return code

        # still on OTP after wait — retry fill/submit a few times
        if page_has_code_input(page) and submit_tries < max_submit_tries:
            if log_callback:
                log_callback("[!] 验证码已填但仍停在验证码页，重试提交")
            human_sleep(0.8, cancel_callback)
            continue
        if page_on_signup_chooser(page):
            raise Exception(f"验证码提交后回到注册方式页（使用邮箱注册）: {email}")
        if has_profile_form(log_callback=None):
            return code
        raise Exception(f"验证码已填写但未进入资料页: {email}")

    raise Exception("验证码已获取，但自动填写/提交失败")


def getTurnstileToken(log_callback=None, cancel_callback=None):
    page = _get_page()
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    try:
        page.run_js(
            "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
        )
    except Exception:
        pass

    for _ in range(0, 20):
        raise_if_cancelled(cancel_callback)
        try:
            token = page.run_js(
                """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            challenge_input = page.ele("@name=cf-turnstile-response")
            if challenge_input:
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            else:
                # 兜底：尝试触发页面上可见的 Turnstile 容器
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
        except Exception:
            pass
        human_sleep(1, cancel_callback)

    raise Exception("Turnstile 获取 token 失败")


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    page = _get_page()
    check_timeout(time.time())
    # Cookie banner often reappears on profile step and blocks 完成注册.
    dismiss_cookie_banner(page, log_callback=log_callback)
    # Wait a bit if OTP just submitted and profile is painting.
    if not has_profile_form(log_callback=None):
        for _ in range(20):
            raise_if_cancelled(cancel_callback)
            dismiss_cookie_banner(page, log_callback=None)
            if has_profile_form(log_callback=None):
                break
            if page_on_signup_chooser(page):
                raise Exception("资料页未出现，页面回到注册方式页")
            human_sleep(0.5, cancel_callback)
    dump_state(page, "profile-form")
    take_screenshot(page, "profile-form")
    given_name, family_name, password = build_profile()
    # 预热 Turnstile：等 2 秒让 iframe 初始化，插件会自动点击 checkbox
    if log_callback:
        log_callback("[*] 预热 Turnstile...")
    human_sleep(2, cancel_callback)
    dismiss_cookie_banner(page, log_callback=log_callback)
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                if log_callback:
                    token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                # 卡住后自动二次复用 Turnstile 组件
                if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                    if log_callback:
                        log_callback("[*] Cloudflare 验证卡住，开始二次复用 Turnstile...")
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                    last_cf_retry_at = now
                human_sleep(0.8, cancel_callback)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                human_sleep(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                human_sleep(0.5, cancel_callback)
                continue

        submit_state = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount');
});
if (!submitBtn) return 'no-submit-button';
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                if log_callback:
                    log_callback("[*] 提交前仍卡住，自动再次复用 Turnstile...")
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                            """,
                            token,
                        )
                        if log_callback:
                            log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                last_cf_retry_at = now
            human_sleep(0.8, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if submit_state == "no-submit-button" and log_callback:
            log_callback("[Debug] 未找到提交按钮，继续等待页面稳定...")

        human_sleep(0.5, cancel_callback)

    raise Exception("最终注册页资料填写失败")


# ── NSFW 自动开启（从 AaronL725 移植） ──


def generate_random_birthdate():
    """生成随机生日（20-40 岁）。"""
    import datetime as dt
    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def set_birth_date(session, log_callback=None):
    """设置生日。"""
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_birth_date status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"set_birth_date HTTP {res.status_code}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None):
    """同意 TOS。"""
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"set_tos_accepted HTTP {res.status_code}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def encode_grpc_nsfw_settings():
    """编码 NSFW 设置 gRPC 请求体。"""
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    """更新 NSFW 设置。"""
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] update_nsfw status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"update_nsfw_settings HTTP {res.status_code}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def enable_nsfw(sso_cookie, log_callback=None):
    """使用 sso cookie 自动开启 NSFW（生日 + TOS + NSFW 设置）。"""
    from curl_cffi import requests as cf_requests
    session = cf_requests.Session()
    session.cookies.set("sso", sso_cookie, domain="grok.com")
    session.cookies.set("sso", sso_cookie, domain="accounts.x.ai")

    results = {}

    # 1. 设置生日
    ok, msg = set_birth_date(session, log_callback=log_callback)
    results["set_birth_date"] = {"ok": ok, "msg": msg}

    # 2. 同意 TOS
    ok, msg = set_tos_accepted(session, log_callback=log_callback)
    results["set_tos_accepted"] = {"ok": ok, "msg": msg}

    # 3. 开启 NSFW
    ok, msg = update_nsfw_settings(session, log_callback=log_callback)
    results["update_nsfw_settings"] = {"ok": ok, "msg": msg}

    if log_callback:
        all_ok = all(r["ok"] for r in results.values())
        log_callback(f"[*] NSFW 设置: {'全部成功' if all_ok else results}")

    return results


# ── wait_for_sso_cookie ──


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            page = _get_page()
            if page is None:
                human_sleep(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    return t.includes('完成注册');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount');
});
if (!submitBtn) return 'final-page-no-submit';
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and retried in ("final-page-no-submit", "final-page-clicked-submit"):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] 最终页 Turnstile 二次复用完成，回填长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass

        human_sleep(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


# ── 登录（非注册）获取 sso ──

LOGIN_URL = "https://accounts.x.ai/login?redirect=grok-com"


def open_login_page(log_callback=None, cancel_callback=None):
    """打开 xAI 登录页，点击「使用邮箱登录」。"""
    browser = _get_browser()
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    if browser is None:
        browser, page = start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        page = _get_page()
        page.get(LOGIN_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        restart_browser()
        page = _get_page()
        page.get(LOGIN_URL)
    page.wait.doc_loaded()
    human_sleep(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    # 点击「使用邮箱登录」
    clicked = page.run_js("""
const btn = document.querySelector('button[data-testid="continue-with-email"]');
if (btn) { btn.click(); return 'clicked'; }
return 'not-found';
""")
    if clicked != 'clicked':
        raise Exception("未找到「使用邮箱登录」按钮")
    human_sleep(2, cancel_callback)
    if log_callback:
        log_callback("[*] 已点击「使用邮箱登录」")


def fill_login_and_submit(email, password, timeout=120, log_callback=None, cancel_callback=None):
    """两步登录：1.填邮箱点下一步 2.填密码处理Turnstile点登录。"""
    page = _get_page()
    deadline = time.time() + timeout
    last_cf_retry = 0.0

    # ── 步骤1：填邮箱，点「下一步」 ──
    email_submitted = False
    while time.time() < deadline and not email_submitted:
        raise_if_cancelled(cancel_callback)
        state = page.run_js("""
const emailInput = document.querySelector('input[data-testid="email"]');
if (!emailInput) return 'not-ready';
const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
const tracker = emailInput._valueTracker;
if (tracker) tracker.setValue('');
if (ns) ns.call(emailInput, arguments[0]); else emailInput.value = arguments[0];
emailInput.dispatchEvent(new InputEvent('input', {bubbles:true, data:arguments[0], inputType:'insertText'}));
emailInput.dispatchEvent(new Event('change', {bubbles:true}));
emailInput.blur();
if (String(emailInput.value||'').trim() !== String(arguments[0]||'').trim()) return 'fill-failed';
const btn = document.querySelector('button[data-testid="sign-in-submit"]');
if (!btn) return 'no-btn';
if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return 'btn-disabled';
btn.click();
return 'submitted';
""", email)
        if state == 'submitted':
            email_submitted = True
            if log_callback:
                log_callback(f"[*] 已填写邮箱并提交: {email}")
        elif state == 'not-ready':
            human_sleep(0.5, cancel_callback)
        elif state == 'btn-disabled':
            human_sleep(0.5, cancel_callback)
        else:
            human_sleep(0.5, cancel_callback)
    if not email_submitted:
        raise Exception("邮箱提交超时")

    # 等密码框出现
    human_sleep(2, cancel_callback)

    # ── 步骤2：填密码，处理 Turnstile，点「登录」 ──
    pw_filled = False
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not pw_filled:
            filled = page.run_js("""
const pwInput = document.querySelector('input[data-testid="password"]');
if (!pwInput) return 'not-ready';
const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
const tracker = pwInput._valueTracker;
if (tracker) tracker.setValue('');
if (ns) ns.call(pwInput, arguments[0]); else pwInput.value = arguments[0];
pwInput.dispatchEvent(new InputEvent('input', {bubbles:true, data:arguments[0], inputType:'insertText'}));
pwInput.dispatchEvent(new Event('change', {bubbles:true}));
pwInput.blur();
if (String(pwInput.value||'').trim() !== String(arguments[0]||'').trim()) return 'fill-failed';
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cf:' + token.length;
}
return 'ready';
""", password)
            if isinstance(filled, str) and filled.startswith('wait-cf'):
                pw_filled = True
                if log_callback:
                    token_len = filled.split(':',1)[1] if ':' in filled else '0'
                    log_callback(f"[*] 已填密码，等待 Turnstile... token长度={token_len}")
                now = time.time()
                if now - last_cf_retry >= 8:
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            page.run_js("""
const token = String(arguments[0]||'').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (cfInput && token) {
    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (ns) ns.call(cfInput, token); else cfInput.value = token;
    cfInput.dispatchEvent(new Event('input', {bubbles:true}));
    cfInput.dispatchEvent(new Event('change', {bubbles:true}));
}
""", token)
                            if log_callback:
                                log_callback("[*] Turnstile 已通过，回填完成")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 复用失败: {cf_exc}")
                    last_cf_retry = now
                human_sleep(1, cancel_callback)
                continue
            elif filled == 'ready':
                pw_filled = True
                if log_callback:
                    log_callback("[*] 密码已填写，准备提交")
            elif filled == 'not-ready':
                human_sleep(0.5, cancel_callback)
                continue
            elif filled == 'fill-failed':
                human_sleep(0.5, cancel_callback)
                continue

        # 提交
        state = page.run_js("""
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cf:' + token.length;
}
const btn = document.querySelector('button[data-testid="sign-in-submit"]');
if (!btn) return 'no-submit';
if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return 'btn-disabled';
btn.click();
return 'submitted';
""")
        if isinstance(state, str) and state.startswith('wait-cf'):
            if log_callback:
                token_len = state.split(':',1)[1] if ':' in state else '0'
                log_callback(f"[*] 等待 Turnstile 通过后再提交... token长度={token_len}")
            now = time.time()
            if now - last_cf_retry >= 8:
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        page.run_js("""
const token = String(arguments[0]||'').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (cfInput && token) {
    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (ns) ns.call(cfInput, token); else cfInput.value = token;
    cfInput.dispatchEvent(new Event('input', {bubbles:true}));
    cfInput.dispatchEvent(new Event('change', {bubbles:true}));
}
""", token)
                        if log_callback:
                            log_callback("[*] Turnstile 二次复用完成")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 复用失败: {cf_exc}")
                last_cf_retry = now
            human_sleep(1, cancel_callback)
            continue
        elif state == 'submitted':
            if log_callback:
                log_callback("[*] 已点击登录，等待 sso cookie...")
            return
        elif state == 'btn-disabled':
            human_sleep(1, cancel_callback)
            continue
        human_sleep(1, cancel_callback)
    raise Exception("登录提交超时")

def login_and_get_sso(email, password, log_callback=None, cancel_callback=None):
    """完整登录流程：打开页 → 填邮箱密码 → Turnstile → 等 sso cookie。"""
    open_login_page(log_callback=log_callback, cancel_callback=cancel_callback)
    fill_login_and_submit(email, password, log_callback=log_callback, cancel_callback=cancel_callback)
    sso = wait_for_sso_cookie(timeout=120, log_callback=log_callback, cancel_callback=cancel_callback)
    return sso


class GrokRegisterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Grok 注册机")
        self.root.geometry("980x860")
        self.root.minsize(900, 760)
        self.is_running = False
        self.batch_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.stop_requested = False
        self.ui_queue = queue.Queue()
        self.accounts_output_file = ""
        self.stats_lock = threading.Lock()
        self._tutorial_window = None
        self.setup_ui()
        self.root.after(200, self._maybe_show_tutorial_on_start)

    def setup_ui(self):
        load_config()
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        config_frame = ttk.LabelFrame(main_frame, text="配置", padding=10)
        config_frame.pack(fill=tk.X, pady=5)
        ttk.Label(config_frame, text="邮箱服务商:").grid(row=0, column=0, sticky=tk.W)
        self.email_provider_var = tk.StringVar(value=config.get("email_provider", "hotmail"))
        self.email_provider_combo = ttk.Combobox(
            config_frame,
            textvariable=self.email_provider_var,
            values=["hotmail", "duckmail", "yyds", "cloudflare", "cloudmail"],
            width=12,
            state="readonly",
        )
        self.email_provider_combo.grid(row=0, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="注册数量:").grid(row=0, column=2, sticky=tk.W, padx=10)
        self.count_var = tk.StringVar(value=str(config.get("register_count", 1)))
        self.count_spinbox = ttk.Spinbox(config_frame, from_=1, to=100, width=8, textvariable=self.count_var)
        self.count_spinbox.grid(row=0, column=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="并发线程:").grid(row=1, column=2, sticky=tk.W, padx=10)
        self.thread_var = tk.StringVar(value=str(config.get("register_threads", 1)))
        self.thread_spinbox = ttk.Spinbox(config_frame, from_=1, to=10, width=8, textvariable=self.thread_var)
        self.thread_spinbox.grid(row=1, column=3, sticky=tk.W, padx=5)
        self.nsfw_var = tk.BooleanVar(value=config.get("enable_nsfw", True))
        self.nsfw_check = ttk.Checkbutton(config_frame, text="注册后开启 NSFW", variable=self.nsfw_var)
        self.nsfw_check.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Label(config_frame, text="代理（可选）:").grid(row=2, column=0, sticky=tk.W)
        self.proxy_var = tk.StringVar(value=config.get("proxy", "http://127.0.0.1:7890"))
        self.proxy_entry = ttk.Entry(config_frame, textvariable=self.proxy_var, width=22)
        self.proxy_entry.grid(row=2, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CPA mint 代理:").grid(row=2, column=2, sticky=tk.W, padx=10)
        self.cpa_proxy_var = tk.StringVar(value=str(config.get("cpa_proxy") or config.get("proxy") or "http://127.0.0.1:7890"))
        self.cpa_proxy_entry = ttk.Entry(config_frame, textvariable=self.cpa_proxy_var, width=18)
        self.cpa_proxy_entry.grid(row=2, column=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="DuckMail API Key:").grid(row=3, column=0, sticky=tk.W)
        self.api_key_var = tk.StringVar(value=config.get("duckmail_api_key", ""))
        self.api_key_entry = ttk.Entry(config_frame, textvariable=self.api_key_var, width=30)
        self.api_key_entry.grid(row=3, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Base:").grid(row=4, column=0, sticky=tk.W)
        self.cloudflare_api_base_var = tk.StringVar(value=config.get("cloudflare_api_base", ""))
        self.cloudflare_api_base_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_base_var, width=30)
        self.cloudflare_api_base_entry.grid(row=4, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Key:").grid(row=5, column=0, sticky=tk.W)
        self.cloudflare_api_key_var = tk.StringVar(value=config.get("cloudflare_api_key", ""))
        self.cloudflare_api_key_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_key_var, width=30)
        self.cloudflare_api_key_entry.grid(row=5, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare 鉴权模式:").grid(row=6, column=0, sticky=tk.W)
        self.cloudflare_auth_mode_var = tk.StringVar(value=config.get("cloudflare_auth_mode", "bearer"))
        self.cloudflare_auth_mode_combo = ttk.Combobox(
            config_frame,
            textvariable=self.cloudflare_auth_mode_var,
            values=["query-key", "bearer", "x-api-key", "none"],
            width=12,
            state="readonly",
        )
        self.cloudflare_auth_mode_combo.grid(row=6, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CF 路径(domains/accounts/token/messages):").grid(row=7, column=0, sticky=tk.W)
        self.cloudflare_paths_var = tk.StringVar(
            value=",".join(
                [
                    config.get("cloudflare_path_domains", "/domains"),
                    config.get("cloudflare_path_accounts", "/accounts"),
                    config.get("cloudflare_path_token", "/token"),
                    config.get("cloudflare_path_messages", "/messages"),
                ]
            )
        )
        self.cloudflare_paths_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_paths_var, width=30)
        self.cloudflare_paths_entry.grid(row=7, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail URL:").grid(row=8, column=0, sticky=tk.W)
        self.cloudmail_url_var = tk.StringVar(value=str(config.get("cloudmail_url", "")))
        self.cloudmail_url_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_url_var, width=30)
        self.cloudmail_url_entry.grid(row=8, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员邮箱:").grid(row=9, column=0, sticky=tk.W)
        self.cloudmail_admin_email_var = tk.StringVar(value=str(config.get("cloudmail_admin_email", "")))
        self.cloudmail_admin_email_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_admin_email_var, width=30)
        self.cloudmail_admin_email_entry.grid(row=9, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员密码:").grid(row=10, column=0, sticky=tk.W)
        self.cloudmail_password_var = tk.StringVar(value=str(config.get("cloudmail_password", "")))
        self.cloudmail_password_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_password_var, width=30, show="*")
        self.cloudmail_password_entry.grid(row=10, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 本地自动入池:").grid(row=11, column=0, sticky=tk.W)
        self.grok2api_local_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_local", True)))
        self.grok2api_local_auto_check = ttk.Checkbutton(config_frame, variable=self.grok2api_local_auto_var)
        self.grok2api_local_auto_check.grid(row=11, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 本地 token.json:").grid(row=12, column=0, sticky=tk.W)
        self.grok2api_local_file_var = tk.StringVar(value=str(config.get("grok2api_local_token_file", "")))
        self.grok2api_local_file_entry = ttk.Entry(config_frame, textvariable=self.grok2api_local_file_var, width=30)
        self.grok2api_local_file_entry.grid(row=12, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 池名:").grid(row=13, column=0, sticky=tk.W)
        self.grok2api_pool_name_var = tk.StringVar(value=str(config.get("grok2api_pool_name", "ssoBasic")))
        self.grok2api_pool_name_combo = ttk.Combobox(
            config_frame,
            textvariable=self.grok2api_pool_name_var,
            values=["ssoBasic", "ssoSuper"],
            width=12,
            state="readonly",
        )
        self.grok2api_pool_name_combo.grid(row=13, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端自动入池:").grid(row=14, column=0, sticky=tk.W)
        self.grok2api_remote_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_remote", False)))
        self.grok2api_remote_auto_check = ttk.Checkbutton(config_frame, variable=self.grok2api_remote_auto_var)
        self.grok2api_remote_auto_check.grid(row=14, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端 Base:").grid(row=15, column=0, sticky=tk.W)
        self.grok2api_remote_base_var = tk.StringVar(value=str(config.get("grok2api_remote_base", "")))
        self.grok2api_remote_base_entry = ttk.Entry(config_frame, textvariable=self.grok2api_remote_base_var, width=30)
        self.grok2api_remote_base_entry.grid(row=15, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端 app_key:").grid(row=16, column=0, sticky=tk.W)
        self.grok2api_remote_key_var = tk.StringVar(value=str(config.get("grok2api_remote_app_key", "")))
        self.grok2api_remote_key_entry = ttk.Entry(config_frame, textvariable=self.grok2api_remote_key_var, width=30)
        self.grok2api_remote_key_entry.grid(row=16, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="默认域名(defaultDomains):").grid(row=17, column=0, sticky=tk.W)
        self.default_domains_var = tk.StringVar(value=str(config.get("defaultDomains", "")))
        self.default_domains_entry = ttk.Entry(config_frame, textvariable=self.default_domains_var, width=30)
        self.default_domains_entry.grid(row=17, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Hotmail 凭证文件:").grid(row=18, column=0, sticky=tk.W)
        self.hotmail_accounts_file_var = tk.StringVar(value=str(config.get("hotmail_accounts_file", "mail_credentials.txt")))
        self.hotmail_accounts_file_entry = ttk.Entry(config_frame, textvariable=self.hotmail_accounts_file_var, width=30)
        self.hotmail_accounts_file_entry.grid(row=18, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Hotmail 别名模式:").grid(row=19, column=0, sticky=tk.W)
        self.hotmail_alias_mode_var = tk.StringVar(value=str(config.get("hotmail_alias_mode", "primary")))
        self.hotmail_alias_mode_combo = ttk.Combobox(
            config_frame,
            textvariable=self.hotmail_alias_mode_var,
            values=["primary", "random", "sequential"],
            width=12,
            state="readonly",
        )
        self.hotmail_alias_mode_combo.grid(row=19, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="每账号最大别名:").grid(row=19, column=2, sticky=tk.W, padx=10)
        self.hotmail_max_aliases_var = tk.StringVar(value=str(config.get("hotmail_max_aliases_per_account", 1)))
        self.hotmail_max_aliases_spin = ttk.Spinbox(
            config_frame, from_=1, to=50, width=8, textvariable=self.hotmail_max_aliases_var
        )
        self.hotmail_max_aliases_spin.grid(row=19, column=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CPA 导出:").grid(row=20, column=0, sticky=tk.W)
        self.cpa_export_var = tk.BooleanVar(value=bool(config.get("cpa_export_enabled", True)))
        self.cpa_export_check = ttk.Checkbutton(config_frame, variable=self.cpa_export_var)
        self.cpa_export_check.grid(row=20, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CPA 目录:").grid(row=20, column=2, sticky=tk.W, padx=10)
        self.cpa_auth_dir_var = tk.StringVar(value=str(config.get("cpa_auth_dir", "./cpa_auths")))
        self.cpa_auth_dir_entry = ttk.Entry(config_frame, textvariable=self.cpa_auth_dir_var, width=18)
        self.cpa_auth_dir_entry.grid(row=20, column=3, sticky=tk.W, padx=5)
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)
        self.start_btn = ttk.Button(btn_frame, text="开始注册", command=self.start_registration)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop_registration, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.clear_btn = ttk.Button(btn_frame, text="清空日志", command=self.clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=5)
        self.help_btn = ttk.Button(btn_frame, text="教程", command=self.show_tutorial)
        self.help_btn.pack(side=tk.LEFT, padx=5)
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=5)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, text="状态: ").pack(side=tk.LEFT)
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, foreground="green")
        self.status_label.pack(side=tk.LEFT)
        self.stats_var = tk.StringVar(value="成功: 0 | 失败: 0")
        ttk.Label(status_frame, textvariable=self.stats_var).pack(side=tk.RIGHT)
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=60)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        # 仅当用户当前就在底部时自动跟随，避免手动上滑后被强制拉回底部
        yview = self.log_text.yview()
        at_bottom = bool(yview) and yview[1] >= 0.999
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        if at_bottom:
            self.log_text.see(tk.END)

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def update_stats(self):
        self.stats_var.set(f"成功: {self.success_count} | 失败: {self.fail_count}")

    def _set_running_ui(self, running):
        self.is_running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("运行中..." if running else "就绪")
        self.status_label.config(foreground="blue" if running else "green")

    def _maybe_show_tutorial_on_start(self):
        if bool(config.get("show_tutorial_on_start", True)):
            self.show_tutorial()

    def _tutorial_text(self):
        return """欢迎使用 Grok 注册机。建议按下面顺序填写（从最关键到可选）：

【第一步：先确定邮箱后端信息从哪里来】
如果你使用 cloudflare 模式（你当前主要是这套），先去你的临时邮箱服务配置接口查信息：
- 常见接口: /open_api/settings、/api/settings、/health_check
- 重点字段:
  - api_base（对应本工具的 Cloudflare API Base）
  - domains / defaultDomains（可用域名）
  - needAuth（是否需要鉴权）
  - admin_password 或 api_key（需要鉴权时使用）
  - provider.type（应为 cloudflare_temp_email）

【第二步：先填最小可运行配置】
1) 邮箱服务商
- duckmail: 需要 DuckMail API Key
- yyds: 需要 YYDS API Key 或 JWT
- cloudflare: 需要 Cloudflare API Base（cloudflare_temp_email 临时邮箱）
- cloudmail: 需要 CloudMail URL + 密码 + defaultDomains（maillab/cloud-mail 完整邮箱）

2) Cloudflare API Base（cloudflare 模式必填）
- 示例: https://xxxx.pages.dev
- 填写规则: 与 settings 接口中的 api_base 保持一致

3) 默认域名(defaultDomains)
- 填写你要优先使用的域名
- 支持单域名或逗号分隔多域名轮换
- 示例: a.com,b.com

4) CF 路径(domains/accounts/token/messages)
- 必须与后端真实路由一致
- 常见新路径:
  - /api/domains,/api/new_address,/api/token,/api/mails
- 常见旧路径:
  - /domains,/accounts,/token,/messages

5) Cloudflare API Key / 鉴权模式
- needAuth=false: 通常鉴权模式选 none，key 可留空
- needAuth=true: 按后端要求填 key，并选择 bearer/x-api-key/query-key

6) CloudMail 模式配置（maillab/cloud-mail 部署）
- CloudMail URL: 你的 Worker 地址，如 https://mail.xxx.workers.dev
- CloudMail 管理员邮箱: 管理员账号，如 admin@yourdomain.com
- CloudMail 管理员密码: 管理员密码（用于获取公开 API token 查询邮件）
- defaultDomains: 必须填写可用域名，如 yourdomain.com
- 前提: CloudMail 管理面板需关闭注册验证码（Turnstile），或确保注册接口可用
- 邮件获取: 通过 /api/public/emailList 公开接口查询，自动刷新 token

【第三步：并发与稳定性】
6) 注册数量
- 本次要注册的总账号数

7) 并发线程
- 建议先 3-6 稳定后再升到 10

8) 代理（可选）
- 不填=直连
- 示例: http://127.0.0.1:7890
- 代理不稳会影响验证码和注册稳定性

9) 注册后开启 NSFW
- 勾选后成功账号会自动调用接口开启对应设置

【第四步：grok2api 入池（可选）】
10) grok2api 本地自动入池
- 开启后把成功 sso 自动写入本地池
- 本地 token.json 填 grok2api 的 token.json 路径

11) grok2api 池名
- ssoBasic 或 ssoSuper

12) grok2api 远端自动入池
- 开启后调用远端管理接口自动加 token
- 远端 Base 示例: https://xxx/admin/api
- app_key 按远端服务配置填写

【最后：快速自检】
1) 先设置: 注册数量=1，并发线程=1
2) 点开始后看日志是否出现：
- 已创建邮箱: xxx@你的域名
- Cloudflare/CloudMail 本轮邮件数量: ...
- 从邮件中提取到验证码: ...
3) 若第一步就失败：
- cloudflare 模式: 检查 API Base / CF 路径 / 鉴权模式
- cloudmail 模式: 检查 URL / 密码 / defaultDomains / 注册接口是否可用

提示:
- 点“开始注册”会自动保存当前配置到 config.json。
- 如果关闭了启动教程，可随时点主界面的“教程”按钮重新打开。"""

    def show_tutorial(self):
        if self._tutorial_window is not None and self._tutorial_window.winfo_exists():
            self._tutorial_window.lift()
            self._tutorial_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._tutorial_window = win
        win.title("使用教程")
        win.geometry("760x620")
        win.minsize(680, 520)
        win.transient(self.root)

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        txt = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=26)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert("1.0", self._tutorial_text())
        txt.config(state=tk.DISABLED)

        footer = ttk.Frame(frame)
        footer.pack(fill=tk.X, pady=(8, 0))

        dont_show_var = tk.BooleanVar(value=not bool(config.get("show_tutorial_on_start", True)))
        chk = ttk.Checkbutton(
            footer,
            text="以后不再自动显示本教程",
            variable=dont_show_var,
        )
        chk.pack(side=tk.LEFT)

        def on_close():
            config["show_tutorial_on_start"] = not bool(dont_show_var.get())
            save_config()
            try:
                win.destroy()
            except Exception:
                pass

        close_btn = ttk.Button(footer, text="关闭", command=on_close)
        close_btn.pack(side=tk.RIGHT, padx=5)
        win.protocol("WM_DELETE_WINDOW", on_close)

    def should_stop(self):
        return self.stop_requested or not self.is_running

    def start_registration(self):
        if self.is_running:
            self.log("[!] 当前已有任务在运行")
            return

        config["email_provider"] = self.email_provider_var.get().strip() or "hotmail"
        config["proxy"] = self.proxy_var.get().strip() or "http://127.0.0.1:7890"
        config["cpa_proxy"] = self.cpa_proxy_var.get().strip() or config["proxy"]
        config["duckmail_api_key"] = self.api_key_var.get().strip()
        config["cloudflare_api_base"] = self.cloudflare_api_base_var.get().strip()
        config["cloudflare_api_key"] = self.cloudflare_api_key_var.get().strip()
        config["cloudflare_auth_mode"] = self.cloudflare_auth_mode_var.get().strip() or "bearer"
        config["cloudmail_url"] = self.cloudmail_url_var.get().strip()
        config["cloudmail_admin_email"] = self.cloudmail_admin_email_var.get().strip()
        config["cloudmail_password"] = self.cloudmail_password_var.get().strip()
        config["grok2api_auto_add_local"] = bool(self.grok2api_local_auto_var.get())
        config["grok2api_local_token_file"] = self.grok2api_local_file_var.get().strip()
        config["grok2api_pool_name"] = self.grok2api_pool_name_var.get().strip() or "ssoBasic"
        config["grok2api_auto_add_remote"] = bool(self.grok2api_remote_auto_var.get())
        config["grok2api_remote_base"] = self.grok2api_remote_base_var.get().strip()
        config["grok2api_remote_app_key"] = self.grok2api_remote_key_var.get().strip()
        config["defaultDomains"] = self.default_domains_var.get().strip()
        config["hotmail_accounts_file"] = self.hotmail_accounts_file_var.get().strip() or "mail_credentials.txt"
        config["hotmail_alias_mode"] = self.hotmail_alias_mode_var.get().strip() or "primary"
        try:
            config["hotmail_max_aliases_per_account"] = max(1, int(self.hotmail_max_aliases_var.get()))
        except Exception:
            config["hotmail_max_aliases_per_account"] = 1
        if str(config.get("hotmail_alias_mode") or "").strip().lower() == "primary":
            config["hotmail_max_aliases_per_account"] = 1
        config["cpa_export_enabled"] = bool(self.cpa_export_var.get())
        config["cpa_auth_dir"] = self.cpa_auth_dir_var.get().strip() or "./cpa_auths"
        try:
            config["register_threads"] = max(1, min(10, int(self.thread_var.get())))
        except Exception:
            config["register_threads"] = 1
        raw_paths = [x.strip() for x in self.cloudflare_paths_var.get().split(",") if x.strip()]
        if len(raw_paths) >= 4:
            config["cloudflare_path_domains"] = raw_paths[0] if raw_paths[0].startswith("/") else ("/" + raw_paths[0])
            config["cloudflare_path_accounts"] = raw_paths[1] if raw_paths[1].startswith("/") else ("/" + raw_paths[1])
            config["cloudflare_path_token"] = raw_paths[2] if raw_paths[2].startswith("/") else ("/" + raw_paths[2])
            config["cloudflare_path_messages"] = raw_paths[3] if raw_paths[3].startswith("/") else ("/" + raw_paths[3])
        # Re-apply playbook normalizer before persist (mutate global in place).
        normalized = _normalize_runtime_config(config)
        config.clear()
        config.update(normalized)
        save_config()
        if config["email_provider"] in ("hotmail", "outlook", "outlookmail", "microsoft"):
            mail_file = config.get("hotmail_accounts_file") or "mail_credentials.txt"
            mail_path = mail_file if os.path.isabs(mail_file) else os.path.join(os.path.dirname(__file__), mail_file)
            if not os.path.isfile(mail_path):
                self.log(f"[!] Hotmail 模式需要凭证文件: {mail_path}")
                return
        if config["email_provider"] == "cloudflare" and not config["cloudflare_api_base"]:
            self.log("[!] Cloudflare 模式需要先填写 Cloudflare API Base")
            return
        if config["email_provider"] == "cloudmail":
            if not config.get("cloudmail_url"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail URL")
                return
            if not config.get("cloudmail_admin_email"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail 管理员邮箱")
                return
            if not config.get("cloudmail_password"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail 管理员密码")
                return
        try:
            count = int(self.count_var.get())
        except Exception:
            self.log("[!] 注册数量无效")
            return
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.accounts_output_file = os.path.join(
            os.path.dirname(__file__), f"accounts_{now}.txt"
        )
        self.update_stats()
        self._set_running_ui(True)
        worker_count = max(1, min(config.get("register_threads", 1), count))
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count}，并发线程: {worker_count}")
        self.log(
            f"[*] 策略: provider={config.get('email_provider')} "
            f"alias={config.get('hotmail_alias_mode')}/{config.get('hotmail_max_aliases_per_account')} "
            f"proxy={config.get('proxy')} cpa_proxy={config.get('cpa_proxy')} "
            f"cpa_export={config.get('cpa_export_enabled')}"
        )
        self.log(f"[*] 成功账号将实时保存到: {self.accounts_output_file}")
        threading.Thread(
            target=self.run_registration,
            args=(count, worker_count),
            daemon=True,
        ).start()

    def stop_registration(self):
        self.stop_requested = True
        self.log("[!] 用户停止注册")

    def _run_single_registration(self, idx, total, logf):
        email = ""
        dev_token = ""
        code = ""
        mail_ok = False
        max_mail_retry = 5
        for mail_try in range(1, max_mail_retry + 1):
            logf(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
            open_signup_page(log_callback=logf, cancel_callback=self.should_stop)
            logf("[*] 2. 创建邮箱并提交")
            try:
                email, dev_token = fill_email_and_submit(
                    log_callback=logf, cancel_callback=self.should_stop
                )
            except EmailAlreadyRegisteredError as exist_exc:
                bad = getattr(exist_exc, "email", "") or email
                mark_error(bad, reason="xai_account_already_exists")
                logf(f"[!] 邮箱已在 xAI 注册，已记入 emails_error 并换号: {bad}")
                if mail_try < max_mail_retry:
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            except EmailOtpRateLimitedError as rate_exc:
                bad = getattr(rate_exc, "email", "") or email
                mark_error(bad, reason=f"otp_send_rate_limit:{str(rate_exc)[:100]}")
                logf(f"[!] xAI 验证码发送过多/限流，已标记并换号: {bad}")
                if mail_try < max_mail_retry:
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            except Exception as submit_exc:
                msg = str(submit_exc)
                is_page_stuck = (
                    "未找到邮箱输入框" in msg
                    or "未进入验证码页" in msg
                    or "仍在邮箱表单" in msg
                    or "反复回到注册方式页" in msg
                    or "表单卡住" in msg
                    or "未找到「使用邮箱注册」" in msg
                )
                if is_page_stuck and mail_try < max_mail_retry:
                    logf(f"[!] 邮箱提交页卡住，重开浏览器重试 ({mail_try}/{max_mail_retry}): {msg}")
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            logf(f"[*] 邮箱: {email}")
            logf("[*] 3. 拉取验证码")
            try:
                code = fill_code_and_submit(
                    email, dev_token, log_callback=logf, cancel_callback=self.should_stop
                )
                mail_ok = True
                break
            except EmailAlreadyRegisteredError as exist_exc:
                bad = getattr(exist_exc, "email", "") or email
                mark_error(bad, reason="xai_account_already_exists")
                logf(f"[!] 邮箱已在 xAI 注册，已记入 emails_error 并换号: {bad}")
                if mail_try < max_mail_retry:
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            except EmailOtpRateLimitedError as rate_exc:
                bad = getattr(rate_exc, "email", "") or email
                mark_error(bad, reason=f"otp_send_rate_limit:{str(rate_exc)[:100]}")
                logf(f"[!] xAI 验证码发送过多/限流，已标记并换号: {bad}")
                if mail_try < max_mail_retry:
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            except Exception as mail_exc:
                msg = str(mail_exc)
                msg_l = msg.lower()
                is_oauth_dead = (
                    "oauth 永久失败" in msg
                    or "oauth2 refresh 失败" in msg_l
                    or "aadsts" in msg_l
                    or "compromised" in msg_l
                    or "security interrupt" in msg_l
                    or "invalid_grant" in msg_l
                )
                is_otp_miss = (
                    ("未收到验证码" in msg or "在" in msg and "验证码" in msg)
                    and not is_oauth_dead
                )
                is_page_stuck = (
                    "未进入验证码页" in msg
                    or "仍在邮箱表单" in msg
                    or "未找到邮箱输入框" in msg
                    or "反复回到注册方式页" in msg
                    or "表单卡住" in msg
                    or "回到注册方式页" in msg
                    or "未进入资料页" in msg
                    or "验证码无效" in msg
                    or "验证码已填写但未进入" in msg
                )
                if (is_oauth_dead or is_otp_miss or is_page_stuck) and mail_try < max_mail_retry:
                    if is_oauth_dead or is_otp_miss:
                        reason = (
                            f"hotmail_oauth_dead:{msg[:100]}"
                            if is_oauth_dead
                            else f"otp_timeout:{msg[:80]}"
                        )
                        mark_error(email, reason=reason)
                        logf(
                            f"[!] 邮箱读信失败，已标记并换号"
                            f"{'（微软判定账号异常/OAuth 失效）' if is_oauth_dead else ''}：{msg}"
                        )
                    else:
                        # cookie/submit UI glitch — same mailbox may work next try
                        logf(f"[!] 页面未进入验证码流程，重开浏览器重试: {msg}")
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
        if not mail_ok:
            raise Exception("验证码阶段失败，已达到最大重试次数")
        logf(f"[*] 验证码: {code}")
        logf("[*] 4. 填写资料")
        try:
            profile = fill_profile_and_submit(log_callback=logf, cancel_callback=self.should_stop)
            logf(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
            logf("[*] 5. 等待 sso cookie")
            sso = wait_for_sso_cookie(log_callback=logf, cancel_callback=self.should_stop)
        except Exception as flow_exc:
            mark_error(email, reason=str(flow_exc)[:120])
            raise
        password = profile.get("password", "") or ""
        mark_used(email, password)
        with self.stats_lock:
            self.results.append({"email": email, "sso": sso, "profile": profile})
            self.success_count += 1
            line = f"{email}----{password}----{sso}\n"
            try:
                with open(self.accounts_output_file, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as file_exc:
                logf(f"[Debug] 保存账号文件失败: {file_exc}")
        add_token_to_grok2api_pools(sso, email=email, log_callback=logf)
        logf(f"[+] 注册成功: {email}")

    def _worker_loop(self, worker_id, total, task_queue):
        prefix = f"[T{worker_id}]"
        logf = lambda m: self.log(f"{prefix} {m}")
        try:
            start_browser(log_callback=logf)
            logf("[*] 浏览器已启动")
            while not self.should_stop():
                try:
                    idx = task_queue.get_nowait()
                except queue.Empty:
                    break
                logf(f"--- 开始第 {idx}/{total} 个账号 ---")
                try:
                    self._run_single_registration(idx, total, logf)
                except RegistrationCancelled:
                    logf("[!] 注册被用户停止")
                    break
                except Exception as exc:
                    with self.stats_lock:
                        self.fail_count += 1
                    logf(f"[-] 注册失败: {exc}")
                finally:
                    self.update_stats()
                    if self.should_stop():
                        break
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
        except Exception as exc:
            logf(f"[!] 线程异常: {exc}")
        finally:
            stop_browser()

    def run_registration(self, count, worker_count):
        task_queue = queue.Queue()
        for i in range(1, count + 1):
            task_queue.put(i)
        workers = []
        try:
            start_interval = float(config.get("thread_start_interval", 0.8))
        except Exception:
            start_interval = 0.8
        if start_interval < 0:
            start_interval = 0.0
        for wid in range(1, worker_count + 1):
            t = threading.Thread(target=self._worker_loop, args=(wid, count, task_queue), daemon=True)
            workers.append(t)
            t.start()
            if wid < worker_count and start_interval > 0:
                sleep_with_cancel(start_interval, self.should_stop)
        for t in workers:
            t.join()
        self._set_running_ui(False)
        self.log("[*] 任务结束")

def main():
    root = tk.Tk()
    app = GrokRegisterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
