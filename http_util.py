"""HTTP helpers, proxies, cancel/sleep utilities for registrar."""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse, urlunparse

from curl_cffi import requests

import config_runtime as _cfg

config = _cfg.config
logger = logging.getLogger("grok_sulfide.http_util")

# Optional host hooks (set by grok_register_ttk after PERF_FLAGS exists).
_get_perf_flags: Callable[[], dict] | None = None
RegistrationCancelled = type("RegistrationCancelled", (Exception,), {})

_registration_proxy_tls = threading.local()


def bind(*, get_perf_flags: Callable[[], dict] | None = None, cancelled_exc: type | None = None) -> None:
    global _get_perf_flags, RegistrationCancelled
    if get_perf_flags is not None:
        _get_perf_flags = get_perf_flags
    if cancelled_exc is not None:
        RegistrationCancelled = cancelled_exc


def _perf() -> dict:
    if _get_perf_flags is None:
        return {}
    return _get_perf_flags() or {}


def get_registration_proxy() -> str:
    runtime = getattr(_registration_proxy_tls, "proxy", None)
    if runtime:
        return runtime
    return str(config.get("proxy", "") or "").strip()


def get_proxies():
    proxy = get_registration_proxy()
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def get_email_proxy():
    """Return the email API proxy; empty inherits the registration proxy."""
    value = str(config.get("email_proxy", "") or "").strip()
    if value.lower() in {"direct", "none", "off", "disabled"}:
        return ""
    return value or get_registration_proxy()


def _build_resin_sticky_proxy(proxy_url, account):
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
    """Start a sticky proxy session id for one registration attempt."""
    base_proxy = str(config.get("proxy", "") or "").strip()
    if not base_proxy or not bool(config.get("resin_sticky_enabled", True)):
        _registration_proxy_tls.proxy = base_proxy
        _registration_proxy_tls.account = ""
        return ""
    prefix = str(config.get("resin_account_prefix") or "grok").strip() or "grok"
    tag = str(label or "acc").strip().replace(" ", "_") or "acc"
    # keep short unique suffix
    import secrets

    account = f"{prefix}_{tag}_{secrets.token_hex(4)}"
    runtime_proxy = _build_resin_sticky_proxy(base_proxy, account)
    _registration_proxy_tls.proxy = runtime_proxy
    _registration_proxy_tls.account = account
    return account


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
        raise RegistrationCancelled("用户停止注册")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def human_sleep(mean_seconds, cancel_callback=None):
    """高斯分布人类化延迟，sigma=mean*0.3，clamp [mean*0.5, mean*2.0]."""
    flags = _perf()
    scale = float(flags.get("sleep_scale", 1.0) or 1.0)
    if flags.get("fast"):
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
