from __future__ import annotations

import os
import re
import secrets
import string
import threading
import time

from . import runtime
from .common import generate_username, pick_list_payload

_hotmail_bridge_module = None


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



class HotmailProvider:
    name = "hotmail"
    aliases = ("hotmail", "outlook", "outlookmail", "microsoft")

    def get_email_and_token(self, api_key=None):
        return _hotmail_bridge().hotmail_get_email_and_token()

    def get_oai_code(self, dev_token, email, *, timeout=180, poll_interval=3,
                     log_callback=None, cancel_callback=None, resend_callback=None):
        return _hotmail_bridge().hotmail_get_oai_code(
            dev_token, email, timeout=timeout, poll_interval=poll_interval,
            log_callback=log_callback, cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
