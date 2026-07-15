"""Approve xAI device-code in Chromium (DrissionPage).

Split implementation:
  - browser_lifecycle.py: standalone browser + cookies + mint pool
  - browser_ui.py: DOM helpers
  - browser_flow.py: approve_device_code + mint_with_browser
"""
from __future__ import annotations

from .browser_flow import approve_device_code, mint_with_browser
from .browser_lifecycle import (
    BrowserConfirmError,
    acquire_mint_browser,
    clear_page_session,
    close_standalone,
    create_standalone_page,
    inject_cookies,
    normalize_cookies,
    release_mint_browser,
    shutdown_mint_browsers,
)

__all__ = [
    "BrowserConfirmError",
    "acquire_mint_browser",
    "approve_device_code",
    "clear_page_session",
    "close_standalone",
    "create_standalone_page",
    "inject_cookies",
    "mint_with_browser",
    "normalize_cookies",
    "release_mint_browser",
    "shutdown_mint_browsers",
]
