"""NSFW / TOS / birthdate post-registration helpers."""
from __future__ import annotations

import random
import struct
from typing import Any, Callable

from curl_cffi import requests

import config_runtime as _cfg
from grok2api_pool import get_user_agent
from http_util import get_proxies

config = _cfg.config
logger = __import__("logging").getLogger("grok_sulfide.nsfw")


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

