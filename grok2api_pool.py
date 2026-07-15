"""Push SSO tokens into local/remote grok2api pools."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

import config_runtime as _cfg
from http_util import http_get, http_post  # may need host http - use runtime

config = _cfg.config
logger = __import__("logging").getLogger("grok_sulfide.grok2api")

# Prefer host http if bound later
_http_get = None
_http_post = None
_get_side_effect_pool = None
_perf_flags = None


def bind(*, http_get=None, http_post=None, get_side_effect_pool=None, get_perf_flags=None):
    global _http_get, _http_post, _get_side_effect_pool, _perf_flags
    if http_get is not None:
        _http_get = http_get
    if http_post is not None:
        _http_post = http_post
    if get_side_effect_pool is not None:
        _get_side_effect_pool = get_side_effect_pool
    if get_perf_flags is not None:
        _perf_flags = get_perf_flags


def http_get(url, **kwargs):
    if _http_get:
        return _http_get(url, **kwargs)
    from http_util import http_get as h
    return h(url, **kwargs)


def http_post(url, **kwargs):
    if _http_post:
        return _http_post(url, **kwargs)
    from http_util import http_post as h
    return h(url, **kwargs)


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
    flags = _perf_flags() if callable(_perf_flags) else {}
    if not isinstance(flags, dict):
        flags = {}
    if flags.get("async_side_effects", True) and _get_side_effect_pool is not None:
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


