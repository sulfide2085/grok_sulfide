"""Standalone mint browser lifecycle and cookie injection."""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger("grok_sulfide.cpa.browser_lifecycle")

LogFn = Callable[[str], None]

def _noop_log(_: str) -> None:
    return None


class BrowserConfirmError(RuntimeError):
    pass


def _sleep(sec: float) -> None:
    time.sleep(sec)


def create_standalone_page(
    *,
    proxy: str | None = None,
    headless: bool = False,
    log: LogFn | None = None,
) -> tuple[Any, Any]:
    log = log or _noop_log
    try:
        from DrissionPage import Chromium, ChromiumOptions
    except ImportError as e:
        raise BrowserConfirmError(
            "DrissionPage not installed; run inside grok_reg uv env or pip install DrissionPage"
        ) from e

    opts = None
    # Project root = parent of this package (./cpa_xai → ../)
    _pkg_root = Path(__file__).resolve().parents[1]
    try:
        reg_file = _pkg_root / "grok_register_ttk.py"
        if reg_file.is_file():
            reg_dir = str(_pkg_root)
            if reg_dir not in sys.path:
                sys.path.insert(0, reg_dir)
            try:
                from grok_register_ttk import create_browser_options  # type: ignore

                opts = create_browser_options(proxy_override=proxy or "")
                log("using register create_browser_options (turnstilePatch)")
            except Exception as e:  # noqa: BLE001
                log(f"register browser options unavailable: {e}")
                opts = None
    except Exception as e:  # noqa: BLE001
        log(f"register options probe failed: {e}")
        opts = None

    if opts is None:
        opts = ChromiumOptions()
        opts.auto_port()
        opts.set_timeouts(base=2)
        for flag in (
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--mute-audio",
            "--no-first-run",
            "--disable-background-networking",
            "--window-size=1280,900",
        ):
            opts.set_argument(flag)
        ext = str(_pkg_root / "turnstilePatch")
        if os.path.isdir(ext):
            try:
                opts.add_extension(ext)
                log(f"added extension {ext}")
            except Exception as e:  # noqa: BLE001
                log(f"extension add failed: {e}")

    if headless:
        try:
            opts.headless(True)
        except Exception:
            opts.set_argument("--headless=new")
        log("headless=True (may hit Cloudflare / break real clicks)")
    else:
        try:
            opts.headless(False)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)
        log(f"headed browser DISPLAY={os.environ.get('DISPLAY', '')!r}")

    for cand in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if os.path.isfile(cand):
            try:
                opts.set_browser_path(cand)
            except Exception:
                logger.debug("suppressed exception", exc_info=True)
            break

    from .proxyutil import proxy_for_chromium, proxy_log_label, resolve_proxy

    # explicit / runtime config first; env only as fallback
    proxy = resolve_proxy(proxy)
    chrome_proxy = proxy_for_chromium(proxy)
    if chrome_proxy:
        opts.set_argument(f"--proxy-server={chrome_proxy}")
        log(f"browser proxy={proxy_log_label(proxy)} (chromium {chrome_proxy})")
    else:
        log("browser proxy=(none)")

    browser = Chromium(opts)
    page = browser.latest_tab
    log("standalone chromium started")
    return browser, page


def close_standalone(browser: Any) -> None:
    try:
        browser.quit()
    except Exception:
        logger.debug("suppressed exception", exc_info=True)


# ── mint browser reuse (per-thread) ──
_mint_tls = threading.local()


def _mint_tls_get() -> dict[str, Any]:
    d = getattr(_mint_tls, "state", None)
    if d is None:
        d = {"browser": None, "page": None, "served": 0, "proxy": None, "headless": None}
        _mint_tls.state = d
    return d


def clear_page_session(page: Any, browser: Any | None = None, log: LogFn | None = None) -> None:
    """Blank page + wipe storage/cookies for reuse between mint jobs."""
    log = log or _noop_log
    try:
        if page is not None:
            try:
                page.get("about:blank")
            except Exception:
                logger.debug("suppressed exception", exc_info=True)
            for js in (
                "try{localStorage.clear()}catch(e){}",
                "try{sessionStorage.clear()}catch(e){}",
            ):
                try:
                    page.run_js(js)
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
        for target in (page, browser):
            if target is None:
                continue
            try:
                target.set.cookies.clear()  # type: ignore[attr-defined]
                log("mint session cookies cleared")
                break
            except Exception:
                try:
                    # older API
                    cks = target.cookies()
                    if isinstance(cks, list):
                        for c in cks:
                            try:
                                target.set.cookies.remove(c)  # type: ignore[attr-defined]
                            except Exception:
                                logger.debug("suppressed exception", exc_info=True)
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
    except Exception as e:
        log(f"clear_page_session: {e}")


def normalize_cookies(cookies: Any) -> list[dict[str, Any]]:
    """Normalize DrissionPage / browser cookie list to settable dicts.

    Also clones SSO-like cookies onto accounts.x.ai / auth.x.ai domains so
    device-auth can skip secondary login when possible.
    """
    out: list[dict[str, Any]] = []
    if not cookies:
        return out
    if isinstance(cookies, dict):
        for k, v in cookies.items():
            if k and v is not None:
                out.append({"name": str(k), "value": str(v), "domain": ".x.ai", "path": "/"})
        cookies = out
        out = []
    if not isinstance(cookies, (list, tuple)):
        return out
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("Name")
        value = c.get("value") or c.get("Value")
        if not name or value is None:
            continue
        domain = str(c.get("domain") or c.get("Domain") or ".x.ai")
        path = str(c.get("path") or c.get("Path") or "/")
        item = {
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": path,
        }
        for src, dst in (
            ("expiry", "expiry"),
            ("expires", "expiry"),
            ("secure", "secure"),
            ("httpOnly", "httpOnly"),
            ("sameSite", "sameSite"),
        ):
            if src in c and c[src] is not None:
                item[dst] = c[src]
        out.append(item)

    # Expand SSO cookies to xAI account hosts (register browser is often on grok.com)
    sso_names = {"sso", "sso-rw", "cf_clearance", "sso_jwt", "__cf_bm"}
    extras: list[dict[str, Any]] = []
    seen = {(i["name"], i["domain"], i["path"]) for i in out}
    for item in list(out):
        n = item["name"]
        if n not in sso_names and not n.startswith("sso"):
            continue
        for dom in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai", ".auth.x.ai"):
            key = (n, dom, item["path"])
            if key in seen:
                continue
            clone = dict(item)
            clone["domain"] = dom
            extras.append(clone)
            seen.add(key)
    out.extend(extras)
    return out


def inject_cookies(page: Any, cookies: Any, log: LogFn | None = None) -> int:
    """Inject cookies into page/browser. Returns count attempted."""
    log = log or _noop_log
    items = normalize_cookies(cookies)
    if not items or page is None:
        return 0
    for url in (
        "https://accounts.x.ai/",
        "https://auth.x.ai/",
        "https://grok.com/",
    ):
        try:
            page.get(url)
            _sleep(0.4)
        except Exception:
            continue

    n = 0
    for target_name, target in (("page", page), ("browser", getattr(page, "browser", None))):
        if target is None:
            continue
        try:
            target.set.cookies(items)  # type: ignore[attr-defined]
            n = len(items)
            log(f"injected cookies bulk via {target_name}={n}")
            break
        except Exception as e:
            log(f"bulk set via {target_name} failed: {e}")

    if n == 0:
        for item in items:
            ok = False
            for target in (page, getattr(page, "browser", None)):
                if target is None:
                    continue
                try:
                    target.set.cookies(item)  # type: ignore[attr-defined]
                    ok = True
                    break
                except Exception:
                    continue
            if ok:
                n += 1
        log(f"injected cookies one-by-one={n}/{len(items)}")

    # JS document.cookie for non-httpOnly SSO cookies (best effort)
    try:
        js_items = [
            c
            for c in items
            if (not c.get("httpOnly")) and c.get("name") in {"sso", "sso-rw", "cf_clearance"}
        ]
        if js_items:
            page.get("https://accounts.x.ai/")
            for c in js_items:
                name = str(c["name"])
                val = str(c["value"])
                # avoid quote breakage
                if "'" in name or "'" in val:
                    continue
                page.run_js(
                    "document.cookie='"
                    + name
                    + "="
                    + val
                    + "; path=/; domain=.x.ai; Secure; SameSite=None'"
                )
            log(f"js cookie fallback applied={len(js_items)}")
    except Exception as e:
        log(f"js cookie fallback: {e}")

    return n


def acquire_mint_browser(

    *,
    proxy: str | None = None,
    headless: bool = False,
    reuse: bool = True,
    recycle_every: int = 15,
    log: LogFn | None = None,
) -> tuple[Any, Any, bool]:
    """Return (browser, page, owned). owned=True means caller must close if not reusing.

    When reuse=True, browser is kept in thread-local and cleared between jobs.
    """
    log = log or _noop_log
    st = _mint_tls_get()
    if reuse and st.get("browser") is not None:
        # recycle if proxy/headless changed or served enough
        need_recycle = (
            st.get("proxy") != (proxy or None)
            or st.get("headless") != headless
            or (recycle_every > 0 and int(st.get("served") or 0) >= recycle_every)
        )
        if not need_recycle:
            page = st.get("page")
            browser = st.get("browser")
            clear_page_session(page, browser, log=log)
            log(f"mint browser reused served={st.get('served')}")
            return browser, page, False
        log("mint browser recycle (proxy/headless/served threshold)")
        try:
            close_standalone(st.get("browser"))
        except Exception:
            logger.debug("suppressed exception", exc_info=True)
        st["browser"] = None
        st["page"] = None
        st["served"] = 0

    browser, page = create_standalone_page(proxy=proxy, headless=headless, log=log)
    if reuse:
        st["browser"] = browser
        st["page"] = page
        st["proxy"] = proxy or None
        st["headless"] = headless
        st["served"] = 0
        return browser, page, False
    return browser, page, True


def release_mint_browser(
    *,
    owned: bool,
    success: bool = True,
    force_quit: bool = False,
    log: LogFn | None = None,
) -> None:
    log = log or _noop_log
    st = _mint_tls_get()
    if force_quit or owned:
        browser = st.get("browser") if not owned else None
        # if owned, caller passes via closing create path — handle both
        if owned:
            # owned browser not in tls
            return
        if browser is not None:
            close_standalone(browser)
        st["browser"] = None
        st["page"] = None
        st["served"] = 0
        log("mint browser quit")
        return
    if success:
        st["served"] = int(st.get("served") or 0) + 1
    else:
        # fail: drop browser to avoid dirty state
        if st.get("browser") is not None:
            close_standalone(st.get("browser"))
            st["browser"] = None
            st["page"] = None
            st["served"] = 0
            log("mint browser dropped after failure")


def shutdown_mint_browsers() -> None:
    st = getattr(_mint_tls, "state", None)
    if not st:
        return
    if st.get("browser") is not None:
        close_standalone(st.get("browser"))
    st["browser"] = None
    st["page"] = None
    st["served"] = 0


