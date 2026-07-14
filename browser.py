"""Browser lifecycle helpers (TabPool wrappers + Chromium options).

Concurrency contract
--------------------
- One Chromium process per worker thread (see ``tab_pool.TabPool``).
- ``BrowserSession`` is a thin object facade over that thread-local pool.
- Registration fill_* helpers still accept an optional page; when omitted they
  read the current thread session via ``get_page()``. Multi-thread browser
  registration is supported only as multi-session (one thread → one session).
- CPA mint uses its own standalone browsers in ``cpa_xai.browser_confirm``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from DrissionPage import ChromiumOptions

from proxy_bridge import start_authenticated_proxy_bridge
from tab_pool import TabPool

logger = logging.getLogger("grok_sulfide.browser")

CHROMIUM_SLIM_FLAGS = [
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-images",
    "--mute-audio",
    "--disable-background-networking",
    "--no-first-run",
]

_BROWSER_PROXY_UNSET = object()
_ROOT = os.path.dirname(os.path.abspath(__file__))
EXTENSION_PATH = os.path.abspath(os.path.join(_ROOT, "turnstilePatch"))

# Injected by host (ttk) to avoid circular imports.
_get_registration_proxy: Callable[[], str] | None = None
_get_perf_flags: Callable[[], dict] | None = None
_human_sleep: Callable[..., None] | None = None


def bind(
    *,
    get_registration_proxy: Callable[[], str],
    get_perf_flags: Callable[[], dict],
    human_sleep: Callable[..., None],
) -> None:
    global _get_registration_proxy, _get_perf_flags, _human_sleep
    _get_registration_proxy = get_registration_proxy
    _get_perf_flags = get_perf_flags
    _human_sleep = human_sleep


def _proxy() -> str:
    if _get_registration_proxy is None:
        return ""
    return str(_get_registration_proxy() or "")


def _perf() -> dict:
    if _get_perf_flags is None:
        return {}
    return _get_perf_flags() or {}


def create_browser_options(proxy_override: Any = _BROWSER_PROXY_UNSET):
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    for flag in CHROMIUM_SLIM_FLAGS:
        options.set_argument(flag)
    if os.path.exists(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)
    if proxy_override is _BROWSER_PROXY_UNSET:
        proxy = _proxy()
    else:
        proxy = str(proxy_override or "").strip()
    if proxy:
        try:
            u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
            host = u.hostname or ""
            if host:
                port = u.port or (443 if (u.scheme or "http") == "https" else 80)
                scheme = u.scheme or "http"
                if u.username is not None:
                    browser_proxy = start_authenticated_proxy_bridge(proxy)
                    options.set_argument(f"--proxy-server={browser_proxy}")
                    print(f"  [proxy] Chromium auth bridge -> {host}:{port}")
                else:
                    options.set_argument(f"--proxy-server={scheme}://{host}:{port}")
        except Exception as e:
            print(f"  [proxy] set browser proxy failed: {e}")
    return options


@dataclass
class BrowserSession:
    """Object facade over the current thread's TabPool Chromium.

    Methods mutate the thread-local browser; construct one session per worker
    thread and do not share a session across threads.
    """

    log_callback: Callable[[str], None] | None = None

    @property
    def browser(self) -> Any:
        return TabPool.get_browser()

    @property
    def page(self) -> Any:
        if TabPool.get_browser() is None:
            return None
        return TabPool.get_tab()

    @property
    def served(self) -> int:
        return TabPool.served_count()

    def start(self) -> tuple[Any, Any]:
        return start_browser(log_callback=self.log_callback)

    def stop(self) -> None:
        stop_browser()

    def restart(self) -> tuple[Any, Any]:
        return restart_browser(log_callback=self.log_callback)

    def prepare_next(self, *, force_recycle: bool = False) -> tuple[Any, Any]:
        return prepare_browser_for_next_account(
            log_callback=self.log_callback,
            force_recycle=force_recycle,
        )

    def sync(self) -> Any:
        return sync_active_page()

    def refresh(self) -> Any:
        return refresh_active_page()

    def clear_session(self) -> bool:
        return TabPool.clear_session(log_callback=self.log_callback)


def current_session(log_callback: Callable[[str], None] | None = None) -> BrowserSession:
    """Return a session bound to the current thread's TabPool state."""
    return BrowserSession(log_callback=log_callback)


def get_browser():
    return TabPool.get_browser()


def set_browser(value) -> None:
    pass


def get_page():
    if TabPool.get_browser() is None:
        return None
    return TabPool.get_tab()


def set_page(value) -> None:
    pass


def start_browser(log_callback=None):
    last_exc = None
    sleeper = _human_sleep or (lambda *a, **k: None)
    for attempt in range(1, 5):
        try:
            TabPool.init(create_browser_options, log_callback=log_callback)
            page = TabPool.get_tab()
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return TabPool.get_browser(), page
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            try:
                TabPool.release_tab()
            except Exception:
                logger.debug("release_tab after start failure failed", exc_info=True)
            sleeper(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser() -> None:
    TabPool.release_tab()


def prepare_browser_for_next_account(log_callback=None, force_recycle: bool = False):
    flags = _perf()
    reuse = bool(flags.get("browser_reuse", True)) and not force_recycle
    every = int(flags.get("browser_recycle_every", 25) or 25)
    served = TabPool.served_count()
    if reuse and TabPool.get_browser() is not None and (every <= 0 or served < every):
        if TabPool.clear_session(log_callback=log_callback):
            TabPool.mark_served()
            return TabPool.get_browser(), get_page()
    if log_callback:
        log_callback(f"[*] 浏览器完整回收（reuse={reuse}, served={served}, every={every}）")
    TabPool.release_tab()
    return start_browser(log_callback=log_callback)


def shutdown_browser() -> None:
    TabPool.shutdown()


def restart_browser(log_callback=None):
    TabPool.release_tab()
    return start_browser(log_callback=log_callback)


def sync_active_page():
    if TabPool.get_browser() is None:
        restart_browser()
        return get_page()
    try:
        browser = TabPool.get_browser()
        tabs = browser.tab_ids
        if tabs:
            browser.get_tab(tabs[-1])
        else:
            browser.new_tab()
        TabPool.sync_tab()
    except Exception:
        logger.debug("sync_active_page failed", exc_info=True)
    return get_page()


def refresh_active_page():
    if TabPool.get_browser() is None:
        restart_browser()
    try:
        browser = TabPool.get_browser()
        tabs = browser.tab_ids
        if tabs:
            page = browser.get_tab(tabs[-1])
        else:
            page = browser.new_tab()
        page.refresh()
        TabPool.sync_tab()
    except Exception:
        restart_browser()
    return get_page()


# Private aliases matching ttk historical names.
_get_browser = get_browser
_set_browser = set_browser
_get_page = get_page
_set_page = set_page
