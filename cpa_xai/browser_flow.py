"""Device-code approval and browser mint orchestration."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .browser_lifecycle import (
    BrowserConfirmError,
    LogFn,
    _noop_log,
    _sleep,
    acquire_mint_browser,
    clear_page_session,
    close_standalone,
    create_standalone_page,
    inject_cookies,
    normalize_cookies,
    release_mint_browser,
    shutdown_mint_browsers,
)
from .browser_ui import (
    _click_exact,
    _cookie_banner_visible,
    _dismiss_cookie_banner,
    _fill,
    _find_button_exact,
    _norm,
    _page_url,
    _visible_text,
    _wait_turnstile,
)

logger = logging.getLogger("grok_sulfide.cpa.browser_flow")

def approve_device_code(
    page: Any,
    *,
    verification_uri_complete: str,
    email: str,
    password: str,
    user_code: str = "",
    timeout_sec: float = 240.0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> None:
    log = log or _noop_log
    if page is None:
        raise BrowserConfirmError("page is None")
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        raise BrowserConfirmError("email/password required")

    if not user_code and "user_code=" in (verification_uri_complete or ""):
        try:
            user_code = verification_uri_complete.split("user_code=", 1)[1].split("&", 1)[0]
        except Exception:
            user_code = ""

    log(f"open device url: {verification_uri_complete}")
    try:
        page.get(verification_uri_complete, timeout=60)
    except TypeError:
        page.get(verification_uri_complete)
    _sleep(2.0)

    deadline = time.time() + timeout_sec
    phase = "device"
    login_attempts = 0
    last_url = ""

    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            log("stop_event set — leave browser loop")
            return

        url = _page_url(page)
        text = _visible_text(page)
        if url != last_url:
            log(f"url: {url[:180]}")
            last_url = url
            snip = _norm(text)[:160]
            if snip:
                log(f"visible: {snip}")

        # Done page
        if "device/done" in url or "设备已授权" in text or "device authorized" in text.lower():
            log("device done page — waiting for token poll")
            _sleep(1.5)
            continue

        if "Invalid action" in text:
            log("Invalid action — reopen device uri")
            page.get(verification_uri_complete)
            _sleep(2.0)
            phase = "device"
            continue

        # The privacy overlay can cover OAuth Allow on the consent page.
        if _cookie_banner_visible(text):
            if _dismiss_cookie_banner(page, log):
                _sleep(0.6)
                continue

        # Consent page — REAL click exact 允许
        if "/consent" in url or "授权 Grok Build" in text or "Authorize Grok Build" in text:
            phase = "consent"
            if _cookie_banner_visible(_visible_text(page)):
                _dismiss_cookie_banner(page, log)
                _sleep(0.6)
                continue
            # Prefer real click; React needs it to set form action=allow
            if _click_exact(page, ["允许", "Allow", "Authorize", "Approve"], log, real=True):
                _sleep(2.5)
                continue
            # last resort: set action and submit
            try:
                page.run_js(
                    """
                    const forms=Array.from(document.querySelectorAll('form'));
                    const f=forms.find((x)=>{
                      const t=(x.innerText||'');
                      return t.includes('Grok Build') || t.includes('允许') || t.includes('Allow');
                    }) || document.querySelector('form');
                    if(!f) return;
                    const ft=(f.innerText||'');
                    if(ft.includes('隐私偏好') || ft.includes('全部允许') || /cookie/i.test(ft)) return;
                    let a=f.querySelector('input[name=action]');
                    if(!a){a=document.createElement('input');a.type='hidden';a.name='action';f.appendChild(a);}
                    a.value='allow';
                    const btn=[...f.querySelectorAll('button')].find(b=>((b.innerText||'').trim())==='允许'||(b.innerText||'').trim()==='Allow');
                    if(btn) btn.click(); else f.submit();
                    """
                )
                log("consent form submit via JS fallback")
                _sleep(2.5)
            except Exception as e:
                log(f"consent fallback failed: {e}")
            continue

        # Device code entry
        if page.ele("css:input[name='user_code']", timeout=0.3) and "consent" not in url:
            phase = "device"
            if user_code:
                try:
                    uc = page.ele("css:input[name='user_code']")
                    cur = (uc.value or "") if uc else ""
                    if user_code.replace("-", "") not in cur.replace("-", ""):
                        uc.clear()
                        uc.input(user_code)
                        log("filled user_code")
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
            if _click_exact(page, ["继续", "Continue"], log, real=False):
                _sleep(2.0)
                continue
            try:
                el = page.ele("css:button[type='submit']", timeout=0.5)
                if el:
                    el.click(by_js=True)
                    log("clicked device submit")
                    _sleep(2.0)
                    continue
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # Account redirect
        if "正在重定向" in text or ("/account" in url and "sign-in" not in url):
            if _click_exact(page, ["继续", "Continue"], log, real=False):
                _sleep(2.0)
                continue

        # Cookie banner (exact labels only)
        if "全部允许" in text or "隐私偏好" in text:
            _click_exact(page, ["全部允许", "全部拒绝"], log, real=False)
            _sleep(0.5)

        # Sign-in chooser
        if "使用邮箱登录" in text or "Continue with email" in text:
            if _click_exact(page, ["使用邮箱登录", "Continue with email", "Sign in with email"], log, real=False):
                _sleep(1.5)
                phase = "email"
                continue

        # Email only step
        if page.ele("css:input[type='email']", timeout=0.3) and not page.ele(
            "css:input[type='password']", timeout=0.2
        ):
            phase = "email"
            _fill(page, "css:input[type='email']", email, log, "email")
            if _click_exact(page, ["下一步", "Next", "Continue", "继续"], log, real=False):
                _sleep(1.8)
                continue

        # Password login
        if page.ele("css:input[type='password']", timeout=0.3):
            phase = "password"
            if login_attempts >= 5:
                _sleep(1.0)
                continue
            login_attempts += 1
            log(f"login attempt {login_attempts}")
            _fill(page, "css:input[type='email']", email, log, "email")
            _wait_turnstile(page, log, 25)
            _fill(page, "css:input[type='password']", password, log, "password")
            _wait_turnstile(page, log, 12)
            # REAL click login helps form submit
            if not _click_exact(page, ["登录", "Sign in", "Log in"], log, real=True):
                try:
                    el = page.ele("css:button[type='submit']", timeout=0.5) or page.ele(
                        "css:button[data-testid='sign-in-submit']", timeout=0.5
                    )
                    if el:
                        el.click()
                        log("clicked login submit real")
                except Exception as e:
                    log(f"login submit fail: {e}")
            # wait navigation
            for _ in range(24):
                if stop_event is not None and stop_event.is_set():
                    return
                _sleep(0.5)
                if not page.ele("css:input[type='password']", timeout=0.2):
                    break
                if "sign-in" not in _page_url(page):
                    break
            continue

        _sleep(1.0)

    if stop_event is not None and stop_event.is_set():
        log("browser finished via stop_event")
        return
    log(f"browser loop ended phase={phase} login_attempts={login_attempts}")


def mint_with_browser(
    *,
    email: str,
    password: str,
    page: Any | None = None,
    proxy: str | None = None,
    headless: bool = False,
    browser_timeout_sec: float = 240.0,
    poll_log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    force_standalone: bool = True,
    cookies: Any | None = None,
    reuse_browser: bool = True,
    recycle_every: int = 15,
) -> dict[str, Any]:
    """Request device code, approve in browser, poll tokens.

    force_standalone=True (default): do not reuse the *register* tab.
    Mint workers may still reuse their *own* Chromium via reuse_browser.
    cookies: optional register-browser cookie list to skip re-login.
    """
    from .oauth_device import OAuthDeviceError, poll_device_token, request_device_code
    from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy

    log = poll_log or _noop_log
    own_browser = None
    owned = False
    work_page = None if force_standalone else page
    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)
    success = False
    try:
        last_err: BaseException | None = None
        sess = None
        for attempt in range(1, 4):
            try:
                sess = request_device_code(proxy=resolved or None)
                last_err = None
                break
            except BaseException as e:  # noqa: BLE001
                last_err = e
                log(f"request_device_code attempt {attempt}/3 failed: {e}")
                _sleep(1.5 * attempt)
        if sess is None:
            raise last_err or RuntimeError("request_device_code failed")
        log(
            f"device user_code={sess.user_code} expires_in={sess.expires_in} "
            f"proxy={proxy_log_label(resolved) or '(none)'}"
        )

        if work_page is None:
            own_browser, work_page, owned = acquire_mint_browser(
                proxy=resolved or None,
                headless=headless,
                reuse=reuse_browser,
                recycle_every=recycle_every,
                log=log,
            )
            if owned:
                # non-reuse path: track for finally close
                pass

        # Cookie inject before opening device URL (skip secondary login when possible)
        if cookies:
            n = inject_cookies(work_page, cookies, log=log)
            log(f"cookie inject count={n}")
            try:
                work_page.get("https://accounts.x.ai/")
                _sleep(1.0)
                url = _page_url(work_page)
                text = _visible_text(work_page)
                snip = _norm(text)[:120]
                log(f"post-inject session url={url[:120]} visible={snip}")
            except Exception as e:
                log(f"post-inject check: {e}")

        stop_event = threading.Event()
        token_box: dict[str, Any] = {}
        err_box: dict[str, BaseException] = {}

        def _poll() -> None:
            try:
                time.sleep(2)
                tr = poll_device_token(
                    sess.device_code,
                    interval=max(sess.interval, 5),
                    expires_in=min(sess.expires_in, int(browser_timeout_sec) + 60),
                    log=log,
                    cancel=cancel,
                    proxy=resolved or None,
                )
                token_box["token"] = tr
                stop_event.set()
                log("token poll SUCCESS — stop_event set")
            except BaseException as e:  # noqa: BLE001
                err_box["err"] = e
                stop_event.set()

        t = threading.Thread(target=_poll, name="oauth-poll", daemon=True)
        t.start()
        try:
            approve_device_code(
                work_page,
                verification_uri_complete=sess.verification_uri_complete,
                email=email,
                password=password,
                user_code=sess.user_code,
                timeout_sec=browser_timeout_sec,
                stop_event=stop_event,
                log=log,
            )
        except BrowserConfirmError as e:
            log(f"browser confirm warning: {e}")

        t.join(timeout=max(browser_timeout_sec, 60) + 30)
        if "token" in token_box:
            tr = token_box["token"]
            success = True
            return {
                "access_token": tr.access_token,
                "refresh_token": tr.refresh_token,
                "id_token": tr.id_token,
                "token_type": tr.token_type,
                "expires_in": tr.expires_in,
                "user_code": sess.user_code,
            }
        if "err" in err_box:
            raise err_box["err"]
        raise OAuthDeviceError("token poll thread ended without result")
    finally:
        if own_browser is not None:
            if owned:
                close_standalone(own_browser)
            else:
                release_mint_browser(owned=False, success=success, log=log)
