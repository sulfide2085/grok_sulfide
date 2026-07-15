from __future__ import annotations

from . import runtime
from .page_state import (
    dismiss_cookie_banner,
    page_has_code_input,
    page_still_on_email_form,
    page_on_signup_chooser,
    page_email_submit_loading,
    wait_for_email_form,
    click_email_signup_button,
    has_profile_form,
)
from .email_step import open_signup_page, fill_email_and_submit
from .otp_step import page_otp_error, _fill_otp_via_drission, fill_code_and_submit
from .profile_step import getTurnstileToken, build_profile, fill_profile_and_submit
from .sso_step import wait_for_sso_cookie, open_login_page, fill_login_and_submit, login_and_get_sso

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


def bind_host(host) -> None:
    runtime.bind(host)


__all__ = [
    "bind_host",
    "SIGNUP_URL",
    "dismiss_cookie_banner",
    "page_has_code_input",
    "page_still_on_email_form",
    "page_on_signup_chooser",
    "page_email_submit_loading",
    "wait_for_email_form",
    "click_email_signup_button",
    "has_profile_form",
    "open_signup_page",
    "fill_email_and_submit",
    "page_otp_error",
    "_fill_otp_via_drission",
    "fill_code_and_submit",
    "getTurnstileToken",
    "build_profile",
    "fill_profile_and_submit",
    "wait_for_sso_cookie",
    "open_login_page",
    "fill_login_and_submit",
    "login_and_get_sso",
]
