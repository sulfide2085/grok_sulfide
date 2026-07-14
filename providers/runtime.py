"""Runtime dependency injection for mail providers.

Providers call into the host registrar (http_get/post, config, cancel helpers)
via this module to avoid circular imports.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

_host: Any = None


def bind(host: Any) -> None:
    """Bind the registrar module (typically grok_register_ttk)."""
    global _host
    _host = host


def host() -> Any:
    if _host is None:
        raise RuntimeError("providers.runtime not bound; import grok_register_ttk first")
    return _host


def config() -> dict:
    return host().config


def http_get(url, **kwargs):
    return host().http_get(url, **kwargs)


def http_post(url, **kwargs):
    return host().http_post(url, **kwargs)


def raise_if_cancelled(cancel_callback=None):
    return host().raise_if_cancelled(cancel_callback)


def sleep_with_cancel(seconds, cancel_callback=None):
    return host().sleep_with_cancel(seconds, cancel_callback)


def extract_verification_code(text, subject=""):
    return host().extract_verification_code(text, subject)


def is_email_used(email: str) -> bool:
    return host().is_email_used(email)


def sync_store_hooks() -> None:
    fn = getattr(host(), "_sync_store_hooks", None)
    if callable(fn):
        fn()
