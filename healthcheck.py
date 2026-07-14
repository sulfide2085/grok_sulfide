"""Pre-batch canary checks so bulk registration fails early and loudly."""
from __future__ import annotations

import logging
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger("grok_sulfide.healthcheck")

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


class HealthCheckError(RuntimeError):
    """Raised when a preflight check fails."""


def _proxy_dict(proxy: str) -> dict[str, str] | None:
    raw = str(proxy or "").strip()
    if not raw or raw.lower() in {"direct", "none", "off", "disabled"}:
        return None
    return {"http": raw, "https": raw}


def check_signup_reachable(
    *,
    proxy: str = "",
    timeout: float = 20,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Lightweight GET of the signup page. Does not require a browser."""
    getter = http_get
    if getter is None:
        try:
            from curl_cffi import requests as cf_requests

            def getter(url, **kwargs):
                return cf_requests.get(url, impersonate="chrome120", **kwargs)
        except Exception:
            import requests

            getter = requests.get

    proxies = _proxy_dict(proxy)
    try:
        resp = getter(SIGNUP_URL, timeout=timeout, proxies=proxies or {})
        status = int(getattr(resp, "status_code", 0) or 0)
        body = str(getattr(resp, "text", "") or "")[:4000]
    except Exception as exc:
        raise HealthCheckError(f"signup page unreachable via proxy={proxy or 'direct'}: {exc}") from exc

    if status >= 400:
        raise HealthCheckError(f"signup page HTTP {status}")

    lower = body.lower()
    # Next.js signup often ships a large shell with few plaintext markers in the
    # first chunk; accept either semantic markers or a normal HTML document.
    markers = ("x.ai", "sign", "email", "grok", "accounts", "turnstile", "inter_")
    html_ok = "<!doctype html" in lower or "<html" in lower
    if not (html_ok or any(m in lower for m in markers)):
        raise HealthCheckError("signup page body missing expected markers (possible DOM/block change)")

    return {"ok": True, "status": status, "bytes": len(body), "url": SIGNUP_URL}


def check_mail_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate mail-related config without opening network sockets when possible."""
    method = str(config.get("registration_method") or "browser").strip().lower()
    if method == "protocol":
        provider = str(config.get("protocol_email_provider") or "outlook").strip().lower()
        if provider == "moemail" and not str(config.get("protocol_moemail_api_key") or "").strip():
            raise HealthCheckError("protocol_moemail_api_key is required for protocol/moemail")
        return {"ok": True, "mode": "protocol", "provider": provider}

    provider = str(config.get("email_provider") or "hotmail").strip().lower()
    if provider in {"hotmail", "outlook", "outlookmail", "microsoft"}:
        path = str(config.get("hotmail_accounts_file") or "mail_credentials.txt")
        from pathlib import Path

        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.is_file():
            raise HealthCheckError(f"hotmail credentials file missing: {p}")
    elif provider == "cloudflare" and not str(config.get("cloudflare_api_base") or "").strip():
        raise HealthCheckError("cloudflare_api_base is required")
    elif provider == "cloudmail" and not str(config.get("cloudmail_url") or "").strip():
        raise HealthCheckError("cloudmail_url is required")
    elif provider == "duckmail" and not str(config.get("duckmail_api_key") or "").strip():
        # duckmail may work without key on public instances; warn only
        logger.warning("duckmail_api_key is empty; public instance may rate-limit")
    return {"ok": True, "mode": "browser", "provider": provider}


def run_preflight(config: dict[str, Any], *, skip_network: bool = False) -> list[dict[str, Any]]:
    """Run all cheap preflight checks. Raises HealthCheckError on hard failure."""
    results: list[dict[str, Any]] = []
    results.append(check_mail_config(config))
    if not skip_network:
        proxy = str(
            config.get("protocol_proxy")
            or config.get("proxy")
            or ""
        ).strip()
        if str(config.get("registration_method") or "browser").lower() == "protocol":
            proxy = str(config.get("protocol_proxy") or config.get("proxy") or "").strip()
        results.append(check_signup_reachable(proxy=proxy))
    return results
