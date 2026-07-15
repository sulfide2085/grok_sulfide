from __future__ import annotations

import re
import time
from typing import Any, Callable

from DrissionPage.errors import PageDisconnectedError

from . import runtime
from .page_state import (
    dismiss_cookie_banner, page_has_code_input, page_still_on_email_form,
    page_on_signup_chooser, page_email_submit_loading, wait_for_email_form,
    click_email_signup_button, has_profile_form,
)


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

def open_signup_page(log_callback=None, cancel_callback=None):
    browser = _get_browser()
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    if browser is None:
        browser, page = start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        page = _get_page()
        page.get(SIGNUP_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            stop_browser()
            browser, page = start_browser()
            page.get(SIGNUP_URL)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 创建新标签页异常: {e2}")
            restart_browser()
            page = _get_page()
            page.get(SIGNUP_URL)
    page.wait.doc_loaded()
    dump_state(page, "signup-loaded")
    take_screenshot(page, "signup")
    human_sleep(1, cancel_callback)
    dismiss_cookie_banner(page, log_callback=log_callback)
    human_sleep(0.5, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )
    dismiss_cookie_banner(page, log_callback=log_callback)
    dump_state(page, "after-email-signup-click")


def fill_email_and_submit(timeout=None, log_callback=None, cancel_callback=None):
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    check_timeout(time.time())
    dismiss_cookie_banner(page, log_callback=log_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    form_timeout = float(
        timeout
        if timeout is not None
        else config.get("email_form_timeout", 45) or 45
    )
    confirm_timeout = float(config.get("email_submit_confirm_timeout", 60) or 60)
    deadline = time.time() + max(25.0, form_timeout)
    submit_attempts = 0
    max_submit_attempts = 6
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        dismiss_cookie_banner(page, log_callback=None)
        # bounced to method chooser mid-loop
        if page_on_signup_chooser(page) and not page_still_on_email_form(page):
            if log_callback:
                log_callback("[!] 当前在注册方式页，重新进入邮箱表单")
            try:
                click_email_signup_button(
                    timeout=12, log_callback=log_callback, cancel_callback=cancel_callback
                )
            except Exception as click_exc:
                if log_callback:
                    log_callback(f"[!] 重进邮箱表单失败: {click_exc}")
                human_sleep(0.8, cancel_callback)
            continue
        if not page_still_on_email_form(page):
            if not wait_for_email_form(
                timeout=8, log_callback=log_callback, cancel_callback=cancel_callback
            ):
                human_sleep(0.5, cancel_callback)
                continue
        filled = page.run_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) return 'not-ready';
input.focus(); input.click();
// 清空并设置值
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
// 完整事件序列，确保 React 受控组件同步
input.dispatchEvent(new Event('focus', { bubbles: true }));
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('blur', { bubbles: true }));
// 验证：值已写入即可（不依赖 checkValidity，部分站点自定义校验会导致误判）
const current = (input.value || '').trim();
if (current === email) return 'filled';
// 兜底：尝试逐字符输入
input.value = '';
input.dispatchEvent(new Event('input', { bubbles: true }));
for (const ch of email) {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
    input.value += ch;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
    input.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
}
input.dispatchEvent(new Event('change', { bubbles: true }));
if ((input.value || '').trim() === email) return 'filled';
return input.value;
            """,
            email,
        )
        if filled == "not-ready":
            human_sleep(0.5, cancel_callback)
            continue
        if filled != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            human_sleep(0.5, cancel_callback)
            continue
        human_sleep(0.8, cancel_callback)
        dismiss_cookie_banner(page, log_callback=log_callback)
        # wait until primary CTA is clickable (not spinner-disabled)
        clicked = None
        for _ready in range(12):
            raise_if_cancelled(cancel_callback)
            dismiss_cookie_banner(page, log_callback=None)
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return 'no-input';
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => isVisible(node));
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        // spinner-only button with empty text near email form
        (text === '' && node.closest('form, [class*="form"], main, body'))
    );
}) || buttons.find((node) => node.type === 'submit') || null;
if (!submitButton) return 'no-button';
if (submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true' || submitButton.getAttribute('aria-busy') === 'true') return 'disabled';
submitButton.scrollIntoView({block: 'center', inline: 'nearest'});
submitButton.click();
return 'clicked';
                """
            )
            if clicked == "clicked":
                break
            if clicked == "disabled":
                dismiss_cookie_banner(page, log_callback=log_callback)
                human_sleep(0.6, cancel_callback)
                continue
            human_sleep(0.4, cancel_callback)
        if clicked != "clicked":
            human_sleep(0.5, cancel_callback)
            continue
        submit_attempts += 1
        if log_callback:
            log_callback(f"[*] 已填写邮箱并点击注册: {email} (第{submit_attempts}次)")
        dump_state(page, "email-submitted")
        take_screenshot(page, "email-submitted")
        # Wait for real navigation: code page / existing account / profile.
        # xAI often shows a long spinner then either OTP or bounces to chooser.
        advanced = False
        wait_deadline = time.time() + max(20.0, confirm_timeout)
        last_status = ""
        while time.time() < wait_deadline:
            raise_if_cancelled(cancel_callback)
            dismiss_cookie_banner(page, log_callback=None)
            try:
                raise_if_existing_account(email, page=page, log_callback=log_callback)
            except EmailAlreadyRegisteredError:
                dump_state(page, "existing-account")
                take_screenshot(page, "existing-account")
                raise
            try:
                raise_if_otp_rate_limited(email, page=page, log_callback=log_callback)
            except EmailOtpRateLimitedError:
                dump_state(page, "otp-rate-limit")
                take_screenshot(page, "otp-rate-limit")
                raise
            if has_profile_form(log_callback=None) or page_has_code_input(page):
                advanced = True
                break
            if page_email_submit_loading(page):
                status = "loading"
            elif page_still_on_email_form(page):
                status = "email-form"
            elif page_on_signup_chooser(page):
                status = "chooser"
                break
            else:
                status = "other"
            if status != last_status and log_callback and status in ("loading", "chooser"):
                log_callback(f"[*] 提交后页面状态: {status}")
                last_status = status
            human_sleep(0.6, cancel_callback)
        if advanced:
            return email, dev_token
        # Final check: rate-limit banner may appear after spinner ends
        raise_if_otp_rate_limited(email, page=page, log_callback=log_callback)
        # still loading after confirm_timeout — soft fail, retry submit if attempts left
        if page_email_submit_loading(page) or page_still_on_email_form(page):
            if submit_attempts < max_submit_attempts and time.time() < deadline:
                if log_callback:
                    log_callback(
                        f"[!] 提交后仍停在邮箱表单/加载中（{confirm_timeout:.0f}s 未进验证码），重试提交"
                    )
                take_screenshot(page, "email-submit-stuck")
                # try soft reload of form: back to chooser then re-enter
                try:
                    page.run_js(
                        r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const back = nodes.find((n) => {
  const t = (n.innerText || n.textContent || '').replace(/\s+/g, '');
  return t === '返回' || t.toLowerCase() === 'back';
});
if (back && !back.disabled) { back.click(); return true; }
return false;
                        """
                    )
                    human_sleep(1.0, cancel_callback)
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
                if page_on_signup_chooser(page) or not page_still_on_email_form(page):
                    try:
                        click_email_signup_button(
                            timeout=12,
                            log_callback=log_callback,
                            cancel_callback=cancel_callback,
                        )
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
                human_sleep(0.5, cancel_callback)
                continue
            raise Exception(f"邮箱提交后未进入验证码页（表单卡住）: {email}")
        if page_on_signup_chooser(page):
            if submit_attempts < max_submit_attempts and time.time() < deadline:
                if log_callback:
                    log_callback("[!] 提交后回到注册方式页，重新进入邮箱表单并重试")
                take_screenshot(page, "email-submit-bounced")
                try:
                    click_email_signup_button(
                        timeout=12, log_callback=log_callback, cancel_callback=cancel_callback
                    )
                except Exception as bounce_exc:
                    if log_callback:
                        log_callback(f"[!] 重新进入邮箱表单失败: {bounce_exc}")
                human_sleep(0.5, cancel_callback)
                continue
            raise Exception(f"邮箱提交后反复回到注册方式页: {email}")
        raise Exception(f"邮箱提交后未进入验证码页: {email}")
    raise Exception("未找到邮箱输入框或注册按钮")


