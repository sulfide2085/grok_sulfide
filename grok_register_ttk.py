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
import logging

from DrissionPage import ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests

import store as _store
from store import mark_used, mark_error, is_email_used

import providers
from providers import get_provider
from providers.common import generate_username as _providers_generate_username

import browser as _browser_mod
from browser import (
    BrowserSession,
    CHROMIUM_SLIM_FLAGS,
    create_browser_options,
    current_session,
    get_browser as _get_browser,
    set_browser as _set_browser,
    get_page as _get_page,
    set_page as _set_page,
    start_browser,
    stop_browser,
    prepare_browser_for_next_account,
    shutdown_browser,
    restart_browser,
    sync_active_page,
    refresh_active_page,
)
from tab_pool import TabPool
from proxy_bridge import (
    cleanup_proxy_bridges as _cleanup_proxy_bridges,
    start_authenticated_proxy_bridge as _start_authenticated_proxy_bridge,
    stop_authenticated_proxy_bridge as _stop_authenticated_proxy_bridge,
)

import config_runtime as _cfg
from config_runtime import (
    CONFIG_FILE,
    DEFAULT_CONFIG,
    ConfigError,
    load_config as _load_config_impl,
    save_config as _save_config_impl,
    normalize_runtime_config as _normalize_runtime_config,
)


# Config lives in config_runtime; keep mutable `config` identity for legacy callers.
config = _cfg.config
logger = logging.getLogger("grok_sulfide.ttk")



# ── 邮箱追踪（实现见 store.py） ──

_EMAILS_USED_FILE = _store._EMAILS_USED_FILE
_EMAILS_ERROR_FILE = _store._EMAILS_ERROR_FILE
_email_track_lock = _store._email_track_lock


def _sync_store_hooks():
    """Point store side-channel hooks at hotmail bridge when loaded."""
    def _bridge_mod():
        try:
            from providers import hotmail as hm
            return getattr(hm, "_hotmail_bridge_module", None)
        except Exception:
            return None

    def _used(email, password=""):
        mod = _bridge_mod()
        if mod is not None and hasattr(mod, "mark_used"):
            mod.mark_used(email, password)

    def _err(email, password="", reason=""):
        mod = _bridge_mod()
        if mod is not None and hasattr(mod, "mark_error"):
            mod.mark_error(email, password, reason)

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
        logger.debug("suppressed exception", exc_info=True)
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




# ── extracted modules (re-exported for backward compatibility) ──
import http_util as _http_util
from http_util import (
    get_proxies,
    get_registration_proxy,
    get_email_proxy,
    _build_resin_sticky_proxy,
    begin_registration_proxy_session,
    _build_request_kwargs,
    http_get,
    http_post,
    raise_if_cancelled,
    sleep_with_cancel,
    human_sleep,
)

import grok2api_pool as _grok2api_pool
from grok2api_pool import (
    get_user_agent,
    resolve_grok2api_local_token_file,
    _normalize_sso_token,
    add_token_to_grok2api_local_pool,
    add_token_to_grok2api_remote_pool,
    _add_token_to_grok2api_pools_sync,
    add_token_to_grok2api_pools,
)

import nsfw_settings as _nsfw_settings
from nsfw_settings import (
    enable_nsfw_for_token,
    generate_random_birthdate,
    set_birth_date,
    set_tos_accepted,
    encode_grpc_nsfw_settings,
    update_nsfw_settings,
    enable_nsfw,
)

import registration as _registration
from registration import (
    SIGNUP_URL,
    dismiss_cookie_banner,
    page_has_code_input,
    page_still_on_email_form,
    page_on_signup_chooser,
    page_email_submit_loading,
    wait_for_email_form,
    click_email_signup_button,
    has_profile_form,
    open_signup_page,
    fill_email_and_submit,
    page_otp_error,
    _fill_otp_via_drission,
    fill_code_and_submit,
    getTurnstileToken,
    build_profile,
    fill_profile_and_submit,
    wait_for_sso_cookie,
    open_login_page,
    fill_login_and_submit,
    login_and_get_sso,
)


def generate_username(length=10):
    return _providers_generate_username(length)


def get_email_provider():
    return config.get("email_provider", "hotmail")


def _hotmail_bridge():
    """Back-compat: hotmail bridge lives in providers.hotmail."""
    from providers.hotmail import _hotmail_bridge as _impl
    return _impl()


def get_email_and_token(api_key=None):
    providers.bind_host(sys.modules[__name__])
    return get_provider(get_email_provider()).get_email_and_token(api_key)


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    providers.bind_host(sys.modules[__name__])
    return get_provider(get_email_provider()).get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=resend_callback,
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


# Bind extracted modules to this host module.
_http_util.bind(get_perf_flags=lambda: PERF_FLAGS, cancelled_exc=RegistrationCancelled)
_grok2api_pool.bind(
    http_get=http_get,
    http_post=http_post,
    get_side_effect_pool=_get_side_effect_pool,
    get_perf_flags=lambda: PERF_FLAGS,
)
_registration.bind_host(sys.modules[__name__])
providers.bind_host(sys.modules[__name__])
try:
    _browser_mod.bind(
        get_registration_proxy=get_registration_proxy,
        get_perf_flags=lambda: PERF_FLAGS,
        human_sleep=human_sleep,
    )
except Exception:
    pass


def main():
    """Desktop GUI entry moved to grok_register_gui.py."""
    from grok_register_gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    main()
