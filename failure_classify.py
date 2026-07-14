"""Classify registration failures for observability / batch summaries."""
from __future__ import annotations

from collections import Counter
from enum import Enum


class FailureClass(str, Enum):
    CAPTCHA = "CAPTCHA"
    RATE_LIMIT = "RATE_LIMIT"
    DOM_CHANGED = "DOM_CHANGED"
    NETWORK = "NETWORK"
    MAIL_TIMEOUT = "MAIL_TIMEOUT"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    OAUTH_DEAD = "OAUTH_DEAD"
    UNKNOWN = "UNKNOWN"


class FailureStats:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def record(self, exc: BaseException | str) -> FailureClass:
        cls = classify_failure(exc)
        self.counts[cls.value] += 1
        return cls

    def summary(self) -> str:
        if not self.counts:
            return "no failures classified"
        parts = [f"{k}={v}" for k, v in sorted(self.counts.items())]
        return ", ".join(parts)


def classify_failure(exc: BaseException | str) -> FailureClass:
    msg = str(exc or "")
    lower = msg.lower()

    if "already" in lower or "已存在" in msg or "已在 xai 注册" in lower or "account already" in lower:
        return FailureClass.ALREADY_REGISTERED
    if "rate" in lower or "过多" in msg or "限流" in msg or "too many" in lower:
        return FailureClass.RATE_LIMIT
    if (
        "turnstile" in lower
        or "captcha" in lower
        or "cf-turnstile" in lower
        or "人机" in msg
    ):
        return FailureClass.CAPTCHA
    if (
        "oauth" in lower
        or "aadsts" in lower
        or "invalid_grant" in lower
        or "compromised" in lower
        or "security interrupt" in lower
    ):
        return FailureClass.OAUTH_DEAD
    if (
        "未收到验证码" in msg
        or "验证码邮件" in msg
        or "mail timeout" in lower
        or "imap" in lower
        or ("timeout" in lower and ("mail" in lower or "验证码" in msg or "code" in lower))
    ):
        return FailureClass.MAIL_TIMEOUT
    if (
        "未找到" in msg
        or "仍在邮箱表单" in msg
        or "未进入" in msg
        or "selector" in lower
        or "element" in lower
        or "dom" in lower
        or "回到注册方式页" in msg
        or "表单卡住" in msg
    ):
        return FailureClass.DOM_CHANGED
    if (
        "proxy" in lower
        or "connection" in lower
        or "timed out" in lower
        or "network" in lower
        or "errno" in lower
        or "could not connect" in lower
        or "ssl" in lower
    ):
        return FailureClass.NETWORK
    return FailureClass.UNKNOWN
