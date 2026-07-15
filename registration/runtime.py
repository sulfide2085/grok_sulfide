"""Host binding for registration step modules."""
from __future__ import annotations
from typing import Any

_host = None


def bind(host: Any) -> None:
    global _host
    _host = host


def host():
    if _host is None:
        raise RuntimeError("registration.runtime not bound")
    return _host
