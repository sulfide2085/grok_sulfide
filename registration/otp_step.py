from __future__ import annotations

import re
import time
from typing import Any, Callable

from DrissionPage.errors import PageDisconnectedError

from . import runtime
from .page_state import (
    dismiss_cookie_banner, page_has_code_input, page_still_on_email_form,
    page_on_signup_chooser, has_profile_form,
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

def page_otp_error(page=None) -> str:
    """Return visible OTP error text if present."""
    page = page or _get_page()
    if page is None:
        return ""
    try:
        hit = page.run_js(
            r"""
const t = ((document.body && (document.body.innerText || document.body.textContent)) || '');
const markers = [
  '验证码无效', '验证码错误', '无效的验证码', '代码无效', '代码不正确',
  'invalid code', 'incorrect code', 'wrong code', 'code is invalid',
  'expired', '已过期', '请重试'
];
const lower = t.toLowerCase();
for (const m of markers) {
  if (lower.includes(m.toLowerCase()) || t.includes(m)) return m;
}
return '';
            """
        )
        return str(hit or "").strip()
    except Exception:
        return ""



def _fill_otp_via_drission(page, clean_code, log_callback=None):
    """Prefer real keystrokes so React OTP state updates (JS-only often fakes success)."""
    if page is None or not clean_code:
        return ""
    selectors = [
        'css:input[data-input-otp="true"]',
        'css:input[autocomplete="one-time-code"]',
        'css:input[name="code"]',
        'css:input[inputmode="numeric"]',
        'xpath://input[@maxlength="1"]',
    ]
    try:
        # single aggregate field
        for sel in selectors[:4]:
            try:
                ele = page.ele(sel, timeout=0.6)
            except Exception:
                ele = None
            if not ele:
                continue
            try:
                ml = int(ele.attr("maxlength") or 0)
            except Exception:
                ml = 0
            if ml == 1:
                continue
            try:
                ele.clear()
            except Exception:
                logger.debug("suppressed exception", exc_info=True)
            try:
                ele.click()
            except Exception:
                logger.debug("suppressed exception", exc_info=True)
            try:
                ele.input(clean_code, clear=True)
            except TypeError:
                ele.input(clean_code)
            try:
                page.actions.key_down("ENTER").key_up("ENTER")
            except Exception:
                try:
                    ele.input("\n")
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
            val = str(ele.value or ele.attr("value") or "").replace(" ", "").strip()
            if val and (clean_code in val or val in clean_code or len(val) >= min(4, len(clean_code))):
                if log_callback:
                    log_callback(f"[*] Drission 写入验证码: aggregate value={val[:8]}")
                return "dp-aggregate"
        # multi-box OTP
        boxes = []
        try:
            boxes = page.eles('css:input[maxlength="1"]', timeout=0.8) or []
        except Exception:
            boxes = []
        boxes = [b for b in boxes if b]
        if len(boxes) >= len(clean_code):
            for i, ch in enumerate(clean_code):
                box = boxes[i]
                try:
                    box.click()
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
                try:
                    box.clear()
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
                try:
                    box.input(ch, clear=True)
                except TypeError:
                    box.input(ch)
            try:
                boxes[min(len(clean_code), len(boxes)) - 1].input("\n")
            except Exception:
                logger.debug("suppressed exception", exc_info=True)
            if log_callback:
                log_callback(f"[*] Drission 写入验证码: {len(clean_code)} boxes")
            return "dp-boxes"
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] Drission OTP 填写异常: {exc}")
    return ""



def fill_code_and_submit(email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
    page = _get_page()
    check_timeout(time.time())
    dismiss_cookie_banner(page, log_callback=log_callback)
    raise_if_existing_account(email, page=page, log_callback=log_callback)
    dump_state(page, "wait-code")
    take_screenshot(page, "wait-code")
    # Don't burn 180s polling IMAP if page never asked for a code.
    if not page_has_code_input(page) and page_still_on_email_form(page):
        raise Exception(f"未进入验证码页（仍在邮箱表单）: {email}")
    if not page_has_code_input(page):
        # brief wait — page may still be transitioning
        for _ in range(10):
            raise_if_cancelled(cancel_callback)
            dismiss_cookie_banner(page, log_callback=None)
            raise_if_existing_account(email, page=page, log_callback=log_callback)
            if page_has_code_input(page) or has_profile_form(log_callback=None):
                break
            human_sleep(0.5, cancel_callback)
        else:
            if not page_has_code_input(page):
                raise Exception(f"未进入验证码页，跳过 IMAP 空等: {email}")

    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    code = get_oai_code(
        dev_token,
        email,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout
    submit_tries = 0
    max_submit_tries = 4

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        dismiss_cookie_banner(page, log_callback=None)
        # already advanced while we were polling mail
        if has_profile_form(log_callback=None):
            if log_callback:
                log_callback("[*] 已在资料页，跳过验证码填写")
            return code
        if page_on_signup_chooser(page):
            raise Exception(f"验证码阶段页面回到注册方式页: {email}")
        if page_still_on_email_form(page) and not page_has_code_input(page):
            raise Exception(f"验证码阶段退回邮箱表单: {email}")
        if not page_has_code_input(page):
            human_sleep(0.5, cancel_callback)
            continue

        # 1) real keystrokes first (ported from gui hardening)
        filled = _fill_otp_via_drission(page, clean_code, log_callback=log_callback)
        # 2) JS fallback for stubborn React OTP widgets
        if not filled:
            filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    // many OTP UIs auto-submit on full length; also fire Enter
    aggregate.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    aggregate.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const last = otpBoxes[Math.min(code.length, otpBoxes.length) - 1];
    if (last) {
        last.focus();
        last.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
        last.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            human_sleep(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            human_sleep(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('验证') ||
        t.includes('提交') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('verify') ||
        t.includes('submit') ||
        t.includes('next')
    );
});

if (btn) {
  btn.focus();
  btn.click();
  return 'clicked';
}
// fallback: form submit or Enter on focused OTP
const form = document.querySelector('form');
if (form && typeof form.requestSubmit === 'function') {
  try { form.requestSubmit(); return 'form-submit'; } catch (e) {}
}
if (form) { try { form.submit(); return 'form-submit'; } catch (e) {} }
return 'no-button';
            """
        )

        submit_tries += 1
        if log_callback:
            log_callback(f"[*] 已填写验证码并提交: {code} ({clicked}, 第{submit_tries}次)")
        dump_state(page, "code-submitted")
        take_screenshot(page, "code-submitted")

        # CRITICAL: wait until profile form (or hard fail). Old code returned after 1.5s
        # even when page bounced back to 「使用邮箱注册」 chooser.
        confirm_deadline = time.time() + float(config.get("code_form_timeout", 45) or 45)
        if confirm_deadline > deadline:
            confirm_deadline = deadline
        advanced = False
        while time.time() < confirm_deadline:
            raise_if_cancelled(cancel_callback)
            dismiss_cookie_banner(page, log_callback=None)
            try:
                raise_if_existing_account(email, page=page, log_callback=log_callback)
            except EmailAlreadyRegisteredError:
                dump_state(page, "existing-account")
                take_screenshot(page, "existing-account")
                raise
            err = page_otp_error(page)
            if err and page_has_code_input(page):
                if log_callback:
                    log_callback(f"[!] 验证码被拒: {err}")
                take_screenshot(page, "code-rejected")
                raise Exception(f"验证码无效/被拒 ({err}): {email}")
            if has_profile_form(log_callback=None):
                advanced = True
                break
            # some flows go straight past profile (rare) — accept leave of OTP page
            if not page_has_code_input(page) and not page_still_on_email_form(page) and not page_on_signup_chooser(page):
                # maybe intermediate loading / CF / redirect
                try:
                    url = str(page.url or "")
                except Exception:
                    url = ""
                if "sign-up" not in url or "complete" in url or "profile" in url or "password" in url:
                    # still wait a bit for profile fields
                    pass
            if page_on_signup_chooser(page):
                take_screenshot(page, "code-bounced-chooser")
                raise Exception(f"验证码提交后回到注册方式页（使用邮箱注册）: {email}")
            if page_still_on_email_form(page) and not page_has_code_input(page):
                take_screenshot(page, "code-bounced-email")
                raise Exception(f"验证码提交后回到邮箱表单: {email}")
            human_sleep(0.6, cancel_callback)

        if advanced:
            if log_callback:
                log_callback("[*] 验证码通过，已进入资料页")
            take_screenshot(page, "after-code-profile")
            return code

        # still on OTP after wait — retry fill/submit a few times
        if page_has_code_input(page) and submit_tries < max_submit_tries:
            if log_callback:
                log_callback("[!] 验证码已填但仍停在验证码页，重试提交")
            human_sleep(0.8, cancel_callback)
            continue
        if page_on_signup_chooser(page):
            raise Exception(f"验证码提交后回到注册方式页（使用邮箱注册）: {email}")
        if has_profile_form(log_callback=None):
            return code
        raise Exception(f"验证码已填写但未进入资料页: {email}")

    raise Exception("验证码已获取，但自动填写/提交失败")


