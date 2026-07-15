from __future__ import annotations

from .cloudflare import CloudflareProvider
from .cloudmail import CloudMailProvider
from .duckmail import DuckMailProvider
from .hotmail import HotmailProvider
from .tempmail import TempmailProvider
from .yyds import YydsProvider
from . import runtime

_REGISTRY: dict[str, type] = {}
for cls in (
    DuckMailProvider,
    CloudflareProvider,
    YydsProvider,
    CloudMailProvider,
    HotmailProvider,
    TempmailProvider,
):
    for alias in cls.aliases:
        _REGISTRY[alias] = cls


def get_provider(name: str | None = None):
    key = str(name or "").strip().lower() or "duckmail"
    cls = _REGISTRY.get(key, DuckMailProvider)
    return cls()


def bind_host(host) -> None:
    runtime.bind(host)


__all__ = [
    "get_provider",
    "bind_host",
    "DuckMailProvider",
    "CloudflareProvider",
    "YydsProvider",
    "CloudMailProvider",
    "HotmailProvider",
    "TempmailProvider",
]
