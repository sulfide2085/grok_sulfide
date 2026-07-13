"""Standalone grok-build-auth protocol registration adapter.

The protocol engine is vendored under ``protocol_engine/grok-build-auth`` so
this module never imports code from sibling projects at runtime.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import time
import uuid
import hashlib
from pathlib import Path
from typing import Any, Callable

import requests


ROOT = Path(__file__).resolve().parent
ENGINE_ROOT = ROOT / "protocol_engine" / "grok-build-auth"
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


def _load_engine() -> None:
    if not (ENGINE_ROOT / "xconsole_client").is_dir():
        raise RuntimeError(f"vendored protocol engine is missing: {ENGINE_ROOT}")
    engine = str(ENGINE_ROOT)
    if engine not in sys.path:
        sys.path.insert(0, engine)


def _mail_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key, "Content-Type": "application/json"}


def _infer_moemail_domain(base_url: str, api_key: str) -> str:
    try:
        response = requests.get(
            f"{base_url}/api/emails",
            headers={"X-API-Key": api_key},
            timeout=20,
        )
        response.raise_for_status()
        emails = response.json().get("emails") or []
        for item in emails:
            address = str((item or {}).get("email") or (item or {}).get("address") or "")
            if "@" in address:
                return address.rsplit("@", 1)[1].strip()
    except Exception:
        pass
    return ""


class MoeMailReceiver:
    def __init__(self, email_id: str, api_key: str, base_url: str) -> None:
        self.email_id = email_id
        self.api_key = api_key
        self.base_url = base_url

    def wait_for_code(self, timeout: float = 120) -> str:
        deadline = time.time() + timeout
        poll = 1.5
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{self.base_url}/api/emails/{self.email_id}",
                    headers={"X-API-Key": self.api_key},
                    timeout=20,
                )
                response.raise_for_status()
                messages = response.json().get("messages") or []
                for raw in messages[:20]:
                    item = dict(raw or {})
                    message_id = item.get("id") or item.get("messageId")
                    if message_id:
                        detail = requests.get(
                            f"{self.base_url}/api/emails/{self.email_id}/{message_id}",
                            headers={"X-API-Key": self.api_key},
                            timeout=20,
                        )
                        if detail.ok:
                            payload = detail.json()
                            message = payload.get("message") if isinstance(payload, dict) else None
                            if isinstance(message, dict):
                                item.update(message)
                    text = "\n".join(
                        str(item.get(key) or "")
                        for key in ("subject", "content", "html", "from_address", "from")
                    )
                    match = re.search(r"\b([A-Z0-9]{3})-([A-Z0-9]{3})\b", text, re.I)
                    if match:
                        return "".join(match.groups()).upper()
                    match = re.search(r"\b([A-Z0-9]{6})\b", text, re.I)
                    if match and "x.ai" in text.lower():
                        return match.group(1).upper()
            except Exception:
                pass
            time.sleep(poll)
            poll = min(3.0, poll + 0.25)
        raise RuntimeError("timeout waiting for xAI email verification code")

    def mark_used(self, _password: str = "") -> None:
        return None

    def mark_error(self, _reason: str = "") -> None:
        return None

    def release(self) -> None:
        return None


class SulfideMailReceiver:
    """Adapter around sulfide's existing Outlook and API mail providers."""

    def __init__(self, provider: str, email: str, token: str, config: dict) -> None:
        self.provider = provider
        self.email = email
        self.token = token
        self.config = config

    def _registrar(self):
        import grok_register_ttk as registrar

        registrar.config.update(self.config)
        registrar.config["email_provider"] = self.provider
        return registrar

    def wait_for_code(self, timeout: float = 120) -> str:
        registrar = self._registrar()
        code = registrar.get_oai_code(
            self.token,
            self.email,
            timeout=int(timeout),
            poll_interval=float(self.config.get("mail_poll_interval") or 3),
            log_callback=None,
            cancel_callback=lambda: False,
        )
        clean = str(code or "").strip().upper().replace("-", "").replace(" ", "")
        if len(clean) != 6:
            raise RuntimeError(f"mail provider returned an invalid verification code shape: {clean!r}")
        return clean

    def mark_used(self, password: str = "") -> None:
        self._registrar().mark_used(self.email, password)

    def mark_error(self, reason: str = "") -> None:
        self._registrar().mark_error(self.email, reason=reason)

    def release(self) -> None:
        registrar = self._registrar()
        if self.provider == "hotmail":
            registrar._hotmail_bridge().mark_error(self.email, "", "release")


def create_moemail(config: dict) -> tuple[str, MoeMailReceiver]:
    api_key = str(config.get("protocol_moemail_api_key") or "").strip()
    if not api_key:
        raise ValueError("protocol_moemail_api_key is required")
    base_url = str(
        config.get("protocol_moemail_base_url") or "https://moemail.example.com"
    ).strip().rstrip("/")
    domain = str(config.get("protocol_moemail_domain") or "").strip().strip(".")
    if not domain:
        domain = _infer_moemail_domain(base_url, api_key)
    if not domain:
        raise RuntimeError("MoeMail domain is empty and could not be inferred")
    expiry_ms = int(config.get("protocol_moemail_expiry_ms") or 3_600_000)
    if expiry_ms not in {0, 3_600_000, 86_400_000, 259_200_000}:
        expiry_ms = 3_600_000
    payload = {
        "name": f"grok-{secrets.token_hex(4)}",
        "domain": domain,
        "expiryTime": expiry_ms,
    }
    response = requests.post(
        f"{base_url}/api/emails/generate",
        json=payload,
        headers=_mail_headers(api_key),
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"MoeMail create failed HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    email_id = data.get("id") or data.get("emailId")
    email = data.get("email") or data.get("address")
    if not email_id or not email:
        raise RuntimeError("MoeMail create returned an unexpected response")
    return str(email).strip().lower(), MoeMailReceiver(str(email_id), api_key, base_url)


def create_email_receiver(config: dict) -> tuple[str, Any]:
    provider = str(config.get("protocol_email_provider") or "outlook").strip().lower()
    if provider == "moemail":
        return create_moemail(config)
    provider = {
        "outlook": "hotmail",
        "outlookmail": "hotmail",
        "microsoft": "hotmail",
    }.get(provider, provider)
    supported = {"hotmail", "duckmail", "yyds", "cloudflare", "cloudmail"}
    if provider not in supported:
        raise ValueError(f"unsupported protocol_email_provider: {provider}")
    import grok_register_ttk as registrar

    registrar.config.update(config)
    registrar.config["email_provider"] = provider
    email, token = registrar.get_email_and_token()
    address = str(email).strip().lower()
    return address, SulfideMailReceiver(provider, address, str(token), config)


def _fresh_turnstile(solver: Any, website_url: str, sitekey: str) -> str:
    return solver.solve_turnstile(
        website_url=website_url,
        website_key=sitekey,
        premium=True,
        fallback_non_premium=True,
    )


def _set_cookie_names(set_cookies: list[str]) -> list[str]:
    names: list[str] = []
    for raw_cookie in set_cookies:
        match = re.match(r"\s*([^=;,\s]+)=", str(raw_cookie or ""))
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def _cookie_scope_summary(client: Any) -> list[str]:
    try:
        metadata = client.cookie_metadata()
    except Exception:
        metadata = []
    scopes: list[str] = []
    for item in metadata:
        name = str((item or {}).get("name") or "")
        domain = str((item or {}).get("domain") or "")
        if name:
            scopes.append(f"{name}@{domain or '(host-only)'}")
    return sorted(set(scopes))


def _emit_signup_diagnostics(
    emit: Callable[[str], None], index: int, response: Any, client: Any
) -> None:
    from xconsole_client.sso import parse_all_set_cookie_urls, parse_sso_token_from_text

    body = str(getattr(response, "rsc_body", "") or "")
    set_cookies = list(getattr(response, "set_cookies", None) or [])
    digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:12]
    markers = []
    for label, present in (
        ("error-flight", bool(re.search(r"(?m)^\d+:E\{", body))),
        ("set-cookie-url", bool(parse_all_set_cookie_urls(body))),
        ("inline-sso", bool(parse_sso_token_from_text(body))),
        ("redirect", "redirect" in body.lower()),
        ("wke", "wke=" in body.lower()),
    ):
        if present:
            markers.append(label)
    emit(
        f"[protocol #{index}] signup response: "
        f"HTTP {int(getattr(response, 'http_status', 0) or 0)}, "
        f"ok={bool(getattr(response, 'ok', False))}, "
        f"rsc_len={len(body)}, rsc_sha256={digest}, "
        f"markers={markers or ['plain-rsc']}"
    )
    emit(
        f"[protocol #{index}] signup cookies: "
        f"set-cookie={_set_cookie_names(set_cookies) or ['none']}; "
        f"jar={_cookie_scope_summary(client) or ['none']}"
    )


def register_one_protocol(
    index: int,
    config: dict,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Register one account and emit both SSO and CPA-compatible auth JSON."""
    emit = log or (lambda message: print(message, flush=True))
    _load_engine()
    from xconsole_client import XConsoleAuthClient, YesCaptchaSolver
    from xconsole_client import config as engine_config
    from xconsole_client.oauth_protocol import extract_cookies_from_auth_client
    from xconsole_client.xai_oauth import complete_build_oauth

    yescaptcha_key = str(config.get("protocol_yescaptcha_key") or "").strip()
    endpoint = str(config.get("protocol_yescaptcha_endpoint") or "").strip() or None
    proxy = str(config.get("protocol_proxy") or config.get("proxy") or "").strip()
    if proxy.lower() in {"direct", "none", "off", "disabled"}:
        proxy = ""
    auth_dir = Path(str(config.get("cpa_auth_dir") or "./cpa_auths")).expanduser()
    if not auth_dir.is_absolute():
        auth_dir = (ROOT / auth_dir).resolve()
    auth_dir.mkdir(parents=True, exist_ok=True)

    email, receiver = create_email_receiver(config)
    password = f"Aa{os.urandom(5).hex()}9!xZ"
    client = XConsoleAuthClient(debug=False, proxy=proxy, signup_url=SIGNUP_URL)
    email_code_requested = False
    try:
        emit(f"[protocol #{index}] mailbox ready: {email}")
        client.visit_home()
        client.load_signup_page()
        sitekey = str(
            getattr(client, "turnstile_sitekey", None)
            or getattr(engine_config, "TURNSTILE_SITEKEY", None)
            or ""
        ).strip()
        website_url = str(getattr(client, "signup_url", None) or SIGNUP_URL).strip()
        if yescaptcha_key and not sitekey:
            raise RuntimeError("Turnstile sitekey was not discovered")

        solver = None
        if yescaptcha_key:
            solver = YesCaptchaSolver(
                yescaptcha_key,
                endpoint=endpoint,
                timeout=float(config.get("protocol_yescaptcha_timeout_sec") or 180),
                debug=False,
                auto_fallback_endpoint=True,
            )
        else:
            emit(f"[protocol #{index}] YesCaptcha not configured; trying direct signup")
        client.validate_password(email, password)
        client.create_email_validation_code(email)
        email_code_requested = True
        emit(f"[protocol #{index}] waiting for email code")
        code = receiver.wait_for_code(timeout=float(config.get("protocol_mail_timeout_sec") or 120))

        response = None
        signup_error = ""
        for attempt in range(1, 3):
            if attempt > 1:
                emit(f"[protocol #{index}] refreshing verification flight")
                client.create_email_validation_code(email)
                code = receiver.wait_for_code(timeout=120)
            client.verify_email_validation_code(email, code)
            turnstile = ""
            if solver is not None:
                emit(f"[protocol #{index}] solving signup Turnstile")
                try:
                    turnstile = _fresh_turnstile(solver, website_url, sitekey)
                except Exception as exc:  # noqa: BLE001
                    emit(
                        f"[protocol #{index}] signup Turnstile unavailable; "
                        f"trying direct create_account ({str(exc)[:120]})"
                    )
            response = client.create_account(
                email=email,
                given_name="User",
                family_name="Grok",
                password=password,
                email_validation_code=code,
                turnstile_token=turnstile,
                castle_request_token="",
                conversion_id=str(uuid.uuid4()),
            )
            _emit_signup_diagnostics(emit, index, response, client)
            body = str(getattr(response, "rsc_body", "") or "")
            try:
                signup_error = str(client.extract_signup_error(body) or "")
            except Exception:
                signup_error = ""
            status = int(getattr(response, "http_status", 0) or 0)
            if status == 200 and not signup_error:
                break
            captcha_rejected = any(
                marker in signup_error.lower()
                for marker in ("turnstile", "captcha")
            )
            if captcha_rejected and solver is None:
                raise RuntimeError(
                    "xAI requires Turnstile for this request; configure the optional YesCaptcha Key and retry"
                )
            if attempt >= 2:
                raise RuntimeError(f"create_account rejected: HTTP {status}; {signup_error or 'unknown'}")

        emit(f"[protocol #{index}] extracting SSO")
        sso = client.fetch_sso_token(email=email, password=password, save=False, retries=4)
        emit(
            f"[protocol #{index}] post-signup cookie jar: "
            f"{_cookie_scope_summary(client) or ['none']}"
        )
        sso_error = ""
        if not sso and solver is not None:
            time.sleep(2.0)
            signin_url = "https://accounts.x.ai/sign-in?redirect=grok-com"
            emit(f"[protocol #{index}] solving sign-in Turnstile for SSO recovery")
            try:
                signin_turnstile = _fresh_turnstile(solver, signin_url, sitekey)
                sso = client.obtain_session_via_password(
                    email=email,
                    password=password,
                    turnstile_token=signin_turnstile,
                    referer=signin_url,
                    retries=4,
                )
                session_diag = client.session_diagnostics()
                emit(f"[protocol #{index}] password-session diagnostics: {session_diag}")
                if not sso:
                    sso_error = str(
                        session_diag.get("grpc_message")
                        or "CreateSession completed without an SSO session"
                    )
            except Exception as exc:  # noqa: BLE001
                sso_error = f"sign-in session recovery failed: {str(exc)[:180]}"
                emit(f"[protocol #{index}] {sso_error}")
        elif not sso:
            sso_error = (
                "signup response did not contain an authenticated session; "
                "YesCaptcha is not configured for the sign-in recovery step"
            )
            emit(f"[protocol #{index}] {sso_error}")
        session_cookies = dict(extract_cookies_from_auth_client(client) or {})
        if sso:
            session_cookies["sso"] = sso
            session_cookies["sso-rw"] = sso
        else:
            emit(f"[protocol #{index}] account created; SSO/CPA will remain pending backfill")

        cpa_path = None
        oauth_error = ""
        if not sso:
            oauth_error = sso_error or "SSO unavailable; OAuth was not attempted"
            emit(f"[protocol #{index}] skipping CPA OAuth until SSO is available")
        else:
            try:
                emit(f"[protocol #{index}] minting CPA OAuth")
                oauth = complete_build_oauth(
                    email,
                    password,
                    cliproxyapi_auth_dir=auth_dir,
                    cliproxyapi_base_url=str(
                        config.get("cpa_base_url") or "https://cli-chat-proxy.grok.com/v1"
                    ),
                    timeout=float(config.get("protocol_oauth_timeout_sec") or 180),
                    proxy=proxy,
                    interactive_fallback=False,
                    yescaptcha_key=yescaptcha_key,
                    protocol=True,
                    debug=False,
                    session_cookies=session_cookies,
                    auth_client=client,
                )
                candidate = Path(oauth.cliproxyapi_path).resolve() if oauth.cliproxyapi_path else None
                if candidate and candidate.is_file():
                    cpa_path = candidate
                else:
                    oauth_error = "OAuth completed without writing CPA auth JSON"
            except Exception as exc:  # noqa: BLE001
                oauth_error = str(exc)
                emit(f"[protocol #{index}] CPA OAuth unavailable; account will be saved for backfill")

        result = {
            "ok": True,
            "email": email,
            "password": password,
            "sso": sso or "",
            "cpa_path": str(cpa_path) if cpa_path else "",
            "partial": not bool(sso and cpa_path),
            "sso_error": sso_error,
            "oauth_error": oauth_error,
        }
        receiver.mark_used(password)
        return result
    except Exception as exc:
        try:
            error_text = str(exc).lower()
            captcha_only_failure = any(
                marker in error_text
                for marker in ("turnstile", "captcha", "yescaptcha")
            )
            if email_code_requested and not captcha_only_failure:
                receiver.mark_error(str(exc)[:160])
            else:
                receiver.release()
        except Exception:
            pass
        raise
    finally:
        client.close()
