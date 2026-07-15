from __future__ import annotations

import re
import time
from typing import Any, Callable

from . import runtime


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

def dismiss_cookie_banner(page=None, log_callback=None) -> str:
    """Close xAI cookie consent if present. Returns which button was clicked or ''."""
    page = page or _get_page()
    if page is None:
        return ""
    try:
        hit = page.run_js(
            r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const labels = [
  '接受所有 Cookie', '接受所有Cookie', '接受全部', '全部允许', '全部接受',
  'Accept all', 'Accept All', 'Allow all', 'Allow All', 'Accept all cookies'
];
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(isVisible);
for (const label of labels) {
  const target = nodes.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    return t === label || t.includes(label);
  });
  if (target && !target.disabled) {
    target.click();
    return label;
  }
}
// close (X) on cookie dialog as fallback
const closeBtn = nodes.find((node) => {
  const aria = (node.getAttribute('aria-label') || '').toLowerCase();
  const t = (node.innerText || node.textContent || '').trim();
  return aria.includes('close') || aria.includes('关闭') || t === '×' || t === 'x' || t === 'X';
});
if (closeBtn) { closeBtn.click(); return 'close'; }
return '';
            """
        )
    except Exception:
        hit = ""
    hit = str(hit or "").strip()
    if hit and log_callback:
        log_callback(f"[*] 已关闭 Cookie 弹窗: {hit}")
    return hit


def page_has_code_input(page=None) -> bool:
    """True only when a real OTP input is visible (not chooser text / residual copy)."""
    page = page or _get_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
// Require actual input widgets. Text-only match falsely advances past spinner/chooser.
const selectors = [
  'input[data-input-otp="true"]',
  'input[autocomplete="one-time-code"]',
  'input[name="code"]',
  'input[inputmode="numeric"]',
  'input[inputmode="text"]'
];
const nodes = Array.from(document.querySelectorAll(selectors.join(','))).filter(
  (n) => isVisible(n) && !n.disabled && !n.readOnly
);
if (nodes.some((n) => Number(n.maxLength || 6) > 1 || String(n.autocomplete || '').toLowerCase() === 'one-time-code' || n.getAttribute('data-input-otp') === 'true' || String(n.name || '').toLowerCase() === 'code')) {
  return true;
}
// multi single-digit OTP boxes
const boxes = Array.from(document.querySelectorAll('input')).filter((n) => {
  if (!isVisible(n) || n.disabled || n.readOnly) return false;
  return Number(n.maxLength || 0) === 1;
});
return boxes.length >= 4;
                """
            )
        )
    except Exception:
        return False


def page_still_on_email_form(page=None) -> bool:
    page = page or _get_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const emailInput = Array.from(document.querySelectorAll(
  'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]'
)).find((n) => isVisible(n));
return !!emailInput;
                """
            )
        )
    except Exception:
        return False


def page_on_signup_chooser(page=None) -> bool:
    """True when back on '创建您的账户' method picker (email / X / Apple / Google)."""
    page = page or _get_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const t = ((document.body && (document.body.innerText || document.body.textContent)) || '');
const hasChooserText = t.includes('使用邮箱注册') && (t.includes('创建您的账户') || t.includes('创建您的帐户'));
if (!hasChooserText) return false;
// email form open = not chooser
const emailInput = Array.from(document.querySelectorAll(
  'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]'
)).some((n) => isVisible(n));
return !emailInput;
                """
            )
        )
    except Exception:
        return False


def page_email_submit_loading(page=None) -> bool:
    """Spinner / disabled primary CTA after email submit."""
    page = page or _get_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const t = ((document.body && (document.body.innerText || document.body.textContent)) || '');
const onEmailStep = t.includes('使用您的邮箱注册') || t.includes('Sign up with your email') ||
  !!document.querySelector('input[data-testid="email"], input[name="email"], input[type="email"]');
if (!onEmailStep) return false;
const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible);
const loading = buttons.some((node) => {
  if (node.disabled || node.getAttribute('aria-disabled') === 'true' || node.getAttribute('aria-busy') === 'true') {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    // spinner-only or 注册 while busy
    return text === '' || text.includes('注册') || text.toLowerCase().includes('sign');
  }
  // SVG spinner inside button
  return !!node.querySelector('svg animateTransform, svg [class*="spin"], .animate-spin, [class*="spinner"]');
});
return loading;
                """
            )
        )
    except Exception:
        return False


def wait_for_email_form(timeout=12, log_callback=None, cancel_callback=None) -> bool:
    """After clicking 使用邮箱注册, wait until email input is actually visible."""
    page = _get_page()
    deadline = time.time() + max(3.0, float(timeout))
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        dismiss_cookie_banner(page, log_callback=None)
        if page_still_on_email_form(page):
            return True
        human_sleep(0.4, cancel_callback)
    if log_callback:
        log_callback("[!] 等待邮箱输入框超时")
    return False


def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    page = _get_page()
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        dismiss_cookie_banner(page, log_callback=log_callback)
        # already on email form — no need to click again
        if page_still_on_email_form(page):
            if log_callback:
                log_callback("[*] 已在邮箱注册表单")
            return True
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(isVisible);
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text.includes('使用邮箱注册') ||
        lower.includes('signupwithemail') ||
        lower.includes('continuewithemail') ||
        (lower.includes('email') && (text.includes('注册') || lower.includes('sign')))
    );
});
if (!target) {
    return false;
}
target.scrollIntoView({block: 'center', inline: 'nearest'});
target.click();
return true;
        """)

        if clicked:
            if log_callback:
                log_callback("[*] 已点击「使用邮箱注册」按钮")
            human_sleep(1.2, cancel_callback)
            dismiss_cookie_banner(page, log_callback=log_callback)
            if wait_for_email_form(timeout=10, log_callback=log_callback, cancel_callback=cancel_callback):
                return True
            # click landed but form not painted yet — keep trying within deadline
            continue

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        human_sleep(0.8, cancel_callback)

    if log_callback:
        try:
            page_html = (page.html or "")[:500]
        except Exception:
            page_html = "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    raise Exception("未找到「使用邮箱注册」按钮")


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


def has_profile_form(log_callback=None):
    # Do NOT hard-refresh here: reload during OTP/profile bounces to 「使用邮箱注册」.
    page = sync_active_page()
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


