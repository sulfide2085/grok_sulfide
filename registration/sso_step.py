from __future__ import annotations

import re
import time
from typing import Any, Callable

from DrissionPage.errors import PageDisconnectedError

from . import runtime
from .page_state import dismiss_cookie_banner
from .profile_step import getTurnstileToken


def _cfg():
    return runtime.host().config


class _Cfg:
    def get(self, *a, **k):
        return _cfg().get(*a, **k)
    def __getitem__(self, k):
        return _cfg()[k]


config = _Cfg()


def _get_page():
    return runtime.host()._get_page()


def _get_browser():
    return runtime.host()._get_browser()


def start_browser(*a, **k):
    return runtime.host().start_browser(*a, **k)


def stop_browser(*a, **k):
    return runtime.host().stop_browser(*a, **k)


def restart_browser(*a, **k):
    return runtime.host().restart_browser(*a, **k)


def refresh_active_page(*a, **k):
    return runtime.host().refresh_active_page(*a, **k)


def sync_active_page(*a, **k):
    return runtime.host().sync_active_page(*a, **k)


def raise_if_cancelled(*a, **k):
    return runtime.host().raise_if_cancelled(*a, **k)


def sleep_with_cancel(*a, **k):
    return runtime.host().sleep_with_cancel(*a, **k)


def human_sleep(*a, **k):
    return runtime.host().human_sleep(*a, **k)


def check_timeout(*a, **k):
    return runtime.host().check_timeout(*a, **k)


def dump_state(*a, **k):
    return runtime.host().dump_state(*a, **k)


def take_screenshot(*a, **k):
    return runtime.host().take_screenshot(*a, **k)


def raise_if_existing_account(*a, **k):
    return runtime.host().raise_if_existing_account(*a, **k)


def raise_if_otp_rate_limited(*a, **k):
    return runtime.host().raise_if_otp_rate_limited(*a, **k)


def get_email_and_token(*a, **k):
    return runtime.host().get_email_and_token(*a, **k)


def get_oai_code(*a, **k):
    return runtime.host().get_oai_code(*a, **k)


def mark_error(*a, **k):
    return runtime.host().mark_error(*a, **k)


def getTurnstileToken(*a, **k):
    # resolved later if defined in profile module
    fn = getattr(runtime.host(), "getTurnstileToken", None)
    if fn is None:
        from . import profile_step
        return profile_step.getTurnstileToken(*a, **k)
    return fn(*a, **k)


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            page = _get_page()
            if page is None:
                human_sleep(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    return t.includes('完成注册');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('sign up') || t.includes('createaccount');
});
if (!submitBtn) return 'final-page-no-submit';
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and retried in ("final-page-no-submit", "final-page-clicked-submit"):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] 最终页 Turnstile 二次复用完成，回填长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        human_sleep(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


# ── 登录（非注册）获取 sso ──

LOGIN_URL = "https://accounts.x.ai/login?redirect=grok-com"


def open_login_page(log_callback=None, cancel_callback=None):
    """打开 xAI 登录页，点击「使用邮箱登录」。"""
    browser = _get_browser()
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    if browser is None:
        browser, page = start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        page = _get_page()
        page.get(LOGIN_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        restart_browser()
        page = _get_page()
        page.get(LOGIN_URL)
    page.wait.doc_loaded()
    human_sleep(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    # 点击「使用邮箱登录」
    clicked = page.run_js("""
const btn = document.querySelector('button[data-testid="continue-with-email"]');
if (btn) { btn.click(); return 'clicked'; }
return 'not-found';
""")
    if clicked != 'clicked':
        raise Exception("未找到「使用邮箱登录」按钮")
    human_sleep(2, cancel_callback)
    if log_callback:
        log_callback("[*] 已点击「使用邮箱登录」")


def fill_login_and_submit(email, password, timeout=120, log_callback=None, cancel_callback=None):
    """两步登录：1.填邮箱点下一步 2.填密码处理Turnstile点登录。"""
    page = _get_page()
    deadline = time.time() + timeout
    last_cf_retry = 0.0

    # ── 步骤1：填邮箱，点「下一步」 ──
    email_submitted = False
    while time.time() < deadline and not email_submitted:
        raise_if_cancelled(cancel_callback)
        state = page.run_js("""
const emailInput = document.querySelector('input[data-testid="email"]');
if (!emailInput) return 'not-ready';
const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
const tracker = emailInput._valueTracker;
if (tracker) tracker.setValue('');
if (ns) ns.call(emailInput, arguments[0]); else emailInput.value = arguments[0];
emailInput.dispatchEvent(new InputEvent('input', {bubbles:true, data:arguments[0], inputType:'insertText'}));
emailInput.dispatchEvent(new Event('change', {bubbles:true}));
emailInput.blur();
if (String(emailInput.value||'').trim() !== String(arguments[0]||'').trim()) return 'fill-failed';
const btn = document.querySelector('button[data-testid="sign-in-submit"]');
if (!btn) return 'no-btn';
if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return 'btn-disabled';
btn.click();
return 'submitted';
""", email)
        if state == 'submitted':
            email_submitted = True
            if log_callback:
                log_callback(f"[*] 已填写邮箱并提交: {email}")
        elif state == 'not-ready':
            human_sleep(0.5, cancel_callback)
        elif state == 'btn-disabled':
            human_sleep(0.5, cancel_callback)
        else:
            human_sleep(0.5, cancel_callback)
    if not email_submitted:
        raise Exception("邮箱提交超时")

    # 等密码框出现
    human_sleep(2, cancel_callback)

    # ── 步骤2：填密码，处理 Turnstile，点「登录」 ──
    pw_filled = False
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not pw_filled:
            filled = page.run_js("""
const pwInput = document.querySelector('input[data-testid="password"]');
if (!pwInput) return 'not-ready';
const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
const tracker = pwInput._valueTracker;
if (tracker) tracker.setValue('');
if (ns) ns.call(pwInput, arguments[0]); else pwInput.value = arguments[0];
pwInput.dispatchEvent(new InputEvent('input', {bubbles:true, data:arguments[0], inputType:'insertText'}));
pwInput.dispatchEvent(new Event('change', {bubbles:true}));
pwInput.blur();
if (String(pwInput.value||'').trim() !== String(arguments[0]||'').trim()) return 'fill-failed';
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cf:' + token.length;
}
return 'ready';
""", password)
            if isinstance(filled, str) and filled.startswith('wait-cf'):
                pw_filled = True
                if log_callback:
                    token_len = filled.split(':',1)[1] if ':' in filled else '0'
                    log_callback(f"[*] 已填密码，等待 Turnstile... token长度={token_len}")
                now = time.time()
                if now - last_cf_retry >= 8:
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            page.run_js("""
const token = String(arguments[0]||'').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (cfInput && token) {
    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (ns) ns.call(cfInput, token); else cfInput.value = token;
    cfInput.dispatchEvent(new Event('input', {bubbles:true}));
    cfInput.dispatchEvent(new Event('change', {bubbles:true}));
}
""", token)
                            if log_callback:
                                log_callback("[*] Turnstile 已通过，回填完成")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 复用失败: {cf_exc}")
                    last_cf_retry = now
                human_sleep(1, cancel_callback)
                continue
            elif filled == 'ready':
                pw_filled = True
                if log_callback:
                    log_callback("[*] 密码已填写，准备提交")
            elif filled == 'not-ready':
                human_sleep(0.5, cancel_callback)
                continue
            elif filled == 'fill-failed':
                human_sleep(0.5, cancel_callback)
                continue

        # 提交
        state = page.run_js("""
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cf:' + token.length;
}
const btn = document.querySelector('button[data-testid="sign-in-submit"]');
if (!btn) return 'no-submit';
if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return 'btn-disabled';
btn.click();
return 'submitted';
""")
        if isinstance(state, str) and state.startswith('wait-cf'):
            if log_callback:
                token_len = state.split(':',1)[1] if ':' in state else '0'
                log_callback(f"[*] 等待 Turnstile 通过后再提交... token长度={token_len}")
            now = time.time()
            if now - last_cf_retry >= 8:
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        page.run_js("""
const token = String(arguments[0]||'').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (cfInput && token) {
    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (ns) ns.call(cfInput, token); else cfInput.value = token;
    cfInput.dispatchEvent(new Event('input', {bubbles:true}));
    cfInput.dispatchEvent(new Event('change', {bubbles:true}));
}
""", token)
                        if log_callback:
                            log_callback("[*] Turnstile 二次复用完成")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 复用失败: {cf_exc}")
                last_cf_retry = now
            human_sleep(1, cancel_callback)
            continue
        elif state == 'submitted':
            if log_callback:
                log_callback("[*] 已点击登录，等待 sso cookie...")
            return
        elif state == 'btn-disabled':
            human_sleep(1, cancel_callback)
            continue
        human_sleep(1, cancel_callback)
    raise Exception("登录提交超时")

def login_and_get_sso(email, password, log_callback=None, cancel_callback=None):
    """完整登录流程：打开页 → 填邮箱密码 → Turnstile → 等 sso cookie。"""
    open_login_page(log_callback=log_callback, cancel_callback=cancel_callback)
    fill_login_and_submit(email, password, log_callback=log_callback, cancel_callback=cancel_callback)
    sso = wait_for_sso_cookie(timeout=120, log_callback=log_callback, cancel_callback=cancel_callback)
    return sso


