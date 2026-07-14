"""Unified CPA/OIDC mint entrypoints for host code.

Two production paths exist historically:

1. **Browser/device path** (default for browser registration + backfill):
   ``cpa_xai.mint.mint_and_export`` → device code + browser confirm →
   ``cpa_xai.writer.write_cpa_xai_auth``.

2. **Protocol path** (HTTP signup chain):
   vendored ``xconsole_client.xai_oauth.complete_build_oauth`` via
   ``protocol_register.register_one_protocol`` only.

Host modules outside ``protocol_register`` should call this facade (or
``cpa_export.export_cpa_xai_for_account``) and **not** import
``xconsole_client`` directly. See ``protocol_engine/BOUNDARY.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .mint import mint_and_export
from .schema import CLIENT_ID, DEFAULT_BASE_URL, build_cpa_xai_auth, credential_file_name
from .writer import write_cpa_xai_auth

LogFn = Callable[[str], None]


def mint_browser_path(
    *,
    email: str,
    password: str,
    auth_dir: str | Path,
    proxy: str = "",
    cookies: list[dict] | None = None,
    log: LogFn | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Browser/device-code mint used by CLI register + backfill scripts."""
    return mint_and_export(
        email=email,
        password=password,
        auth_dir=auth_dir,
        proxy=proxy,
        cookies=cookies,
        log=log,
        **kwargs,
    )


def write_auth_payload(auth_dir: str | Path, payload: dict[str, Any]) -> Path:
    """Atomic write helper shared by mint paths."""
    return write_cpa_xai_auth(auth_dir, payload)


__all__ = [
    "CLIENT_ID",
    "DEFAULT_BASE_URL",
    "build_cpa_xai_auth",
    "credential_file_name",
    "mint_browser_path",
    "mint_and_export",
    "write_auth_payload",
    "write_cpa_xai_auth",
]
