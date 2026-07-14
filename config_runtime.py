"""Runtime config load/save/normalize for grok_sulfide.

Named config_runtime.py (not config.py) to avoid clashing with the
runtime config.json path and accidental `import config` shadowing.
"""
from __future__ import annotations

import json
import os
from typing import Any

_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_ROOT, "config.json")

# Defaults aligned with docs/REGISTER_PLAYBOOK.md.
DEFAULT_CONFIG: dict[str, Any] = {
    "email_provider": "hotmail",
    "defaultDomains": "",
    "hotmail_accounts_file": "mail_credentials.txt",
    "hotmail_alias_mode": "primary",
    "hotmail_alias_random_length": 8,
    "hotmail_alias_random_max_attempts": 200,
    "hotmail_max_aliases_per_account": 1,
    "hotmail_poll_interval": 5,
    "hotmail_recent_seconds": 900,
    "hotmail_imap_hosts": "outlook.office365.com,imap-mail.outlook.com",
    "hotmail_imap_last_n": 30,
    "hotmail_require_recipient_match": True,
    "duckmail_api_key": "",
    "yyds_api_key": "",
    "yyds_jwt": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "bearer",
    "cloudflare_path_domains": "/domains",
    "cloudflare_path_accounts": "/accounts",
    "cloudflare_path_token": "/token",
    "cloudflare_path_messages": "/messages",
    "proxy": "http://127.0.0.1:7890",
    "email_proxy": "direct",
    "cpa_proxy": "http://127.0.0.1:7890",
    "resin_sticky_enabled": True,
    "resin_account_prefix": "grok",
    "enable_nsfw": True,
    "register_count": 1,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
    "grok2api_auto_add_local": False,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    "grok2api_remote_base": "http://127.0.0.1:8000/admin/api",
    "grok2api_remote_app_key": "",
    "grok2api_import_retries": 5,
    "grok2api_import_retry_delay": 2,
    "register_threads": 1,
    "thread_start_interval": 0.8,
    "show_tutorial_on_start": False,
    "cloudmail_url": "",
    "cloudmail_admin_email": "",
    "cloudmail_password": "",
    "api_reverse_tools": "",
    "cpa_export_enabled": True,
    "cpa_auth_dir": "./cpa_auths",
    "cpa_copy_to_hotload": False,
    "cpa_hotload_dir": "",
    "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
    "cpa_management_upload_enabled": False,
    "cpa_management_base": "https://api.example.com",
    "cpa_management_key": "",
    "cpa_headless": False,
    "cpa_force_standalone": True,
    "cpa_mint_timeout_sec": 300,
    "cpa_mint_required": False,
    "cpa_probe_after_write": True,
    "cpa_probe_chat": False,
    "cpa_mint_workers": 1,
    "cpa_mint_queue_max": 0,
    "cpa_mint_cookie_inject": True,
    "cpa_mint_browser_reuse": True,
    "cpa_mint_browser_recycle_every": 15,
    "register_max_attempts": 30,
    "account_hard_timeout": 720,
    "nav_email_button_timeout": 12,
    "email_form_timeout": 45,
    "mail_timeout": 150,
    "mail_poll_interval": 0.3,
    "mail_retry_count": 3,
    "code_form_timeout": 180,
    "profile_timeout": 240,
    "turnstile_retry_limit": 3,
    "turnstile_stuck_timeout": 150,
    "sso_timeout_base": 240,
    "sso_timeout_max": 480,
    "sso_progress_extension": 120,
    "sso_cookie_read_timeout": 20,
    "email_submit_confirm_timeout": 60,
    "registration_method": "browser",
}

# Mutable process-wide config object (same pattern as legacy ttk module).
config: dict[str, Any] = DEFAULT_CONFIG.copy()

_VALID_REGISTRATION_METHODS = frozenset({"browser", "protocol"})


class ConfigError(ValueError):
    """Invalid configuration value."""


def load_env(env_path: str | None = None) -> None:
    """Load .env into os.environ only for keys not already set."""
    path = env_path or os.path.join(_ROOT, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


def validate_config(cfg: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
    """Validate and coerce critical keys. Raises ConfigError when strict and invalid."""
    out = dict(cfg)
    method = str(out.get("registration_method") or "browser").strip().lower()
    if method not in _VALID_REGISTRATION_METHODS:
        if strict:
            raise ConfigError(
                f"registration_method must be one of {sorted(_VALID_REGISTRATION_METHODS)}, got {method!r}"
            )
        method = "browser"
    out["registration_method"] = method

    for key in (
        "register_threads",
        "register_count",
        "register_max_attempts",
        "mail_timeout",
        "account_hard_timeout",
        "cpa_mint_timeout_sec",
        "cpa_mint_workers",
    ):
        try:
            val = int(out.get(key) if out.get(key) is not None else DEFAULT_CONFIG.get(key, 1))
        except (TypeError, ValueError):
            if strict:
                raise ConfigError(f"{key} must be an integer, got {out.get(key)!r}") from None
            val = int(DEFAULT_CONFIG.get(key, 1))
        if val < 1:
            if strict:
                raise ConfigError(f"{key} must be >= 1, got {val}")
            val = 1
        out[key] = val
    return out


def normalize_runtime_config(cfg: dict) -> dict:
    """Apply playbook defaults and coerce hotmail primary-only policy."""
    if not isinstance(cfg, dict):
        return DEFAULT_CONFIG.copy()
    out = {**DEFAULT_CONFIG, **cfg}

    provider = str(out.get("email_provider") or "hotmail").strip().lower()
    if provider in ("outlook", "outlookmail", "microsoft"):
        provider = "hotmail"
    out["email_provider"] = provider or "hotmail"

    alias_mode = str(out.get("hotmail_alias_mode") or "primary").strip().lower()
    if alias_mode in ("", "main", "bare", "no_alias", "no-alias"):
        alias_mode = "primary"
    out["hotmail_alias_mode"] = alias_mode

    try:
        max_aliases = int(out.get("hotmail_max_aliases_per_account") or 1)
    except Exception:
        max_aliases = 1
    if alias_mode == "primary":
        max_aliases = 1
    out["hotmail_max_aliases_per_account"] = max(1, max_aliases)

    proxy = str(out.get("proxy") or "").strip()
    cpa_proxy = str(out.get("cpa_proxy") or "").strip()
    if not cpa_proxy and proxy:
        out["cpa_proxy"] = proxy
    if not str(out.get("cpa_base_url") or "").strip():
        out["cpa_base_url"] = "https://cli-chat-proxy.grok.com/v1"

    return validate_config(out, strict=False)


# Back-compat name used throughout ttk / gui.
_normalize_runtime_config = normalize_runtime_config


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load config.json into the module-level `config` and return it."""
    global config
    load_env()
    cfg_path = path or CONFIG_FILE
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                loaded = {
                    k: v
                    for k, v in loaded.items()
                    if not (isinstance(k, str) and (k.startswith("//") or k.startswith("#")))
                }
            config = normalize_runtime_config(loaded)
        except Exception:
            config = DEFAULT_CONFIG.copy()
    else:
        config = DEFAULT_CONFIG.copy()
    return config


def save_config(path: str | None = None) -> None:
    cfg_path = path or CONFIG_FILE
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")
