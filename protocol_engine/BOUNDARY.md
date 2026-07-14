# protocol_engine boundary

## Purpose

`protocol_engine/grok-build-auth` is a **vendored** third-party-style engine for
HTTP-protocol xAI signup + SSO + CPA/OIDC mint. The host project (`grok_sulfide`)
must treat it as an external package, not as a place to grow product logic.

## Public entry from host

Host code should only touch the engine through:

| Host module | Public API |
|-------------|------------|
| `protocol_register.py` | `register_one_protocol(index, config, *, log=None) -> dict` |
| `protocol_register.py` | `create_email_receiver(config) -> (email, receiver)` |

`register_one_protocol` is the stable contract used by `register_cli.py`.

### Return shape (`register_one_protocol`)

```python
{
  "ok": True,
  "email": str,
  "password": str,
  "sso": str,            # may be "" when partial
  "cpa_path": str,       # may be "" when partial
  "partial": bool,       # True when SSO and/or CPA missing
  "sso_error": str,
  "oauth_error": str,
}
```

### Receiver contract

Objects returned by `create_email_receiver` implement:

- `wait_for_code(timeout: float) -> str`
- `mark_used(password: str = "") -> None`
- `mark_error(reason: str = "") -> None`
- `release() -> None`

## Internal only (do not import from host)

- `protocol_engine/grok-build-auth/run.py` (standalone CLI)
- `protocol_engine/grok-build-auth/xconsole_client/**`
- engine-local `.env`, `sso_output/`, `oauth_output/`, `cliproxyapi_auth/`

`protocol_register._load_engine()` puts `ENGINE_ROOT` on `sys.path` so the
vendored package can import as `xconsole_client`. Host modules outside
`protocol_register.py` should **not** import `xconsole_client` directly.

## Config mapping

Host `config.json` keys (not engine `.env`) drive the adapter:

| Host key | Engine use |
|----------|------------|
| `protocol_proxy` / `proxy` | HTTP proxy for client |
| `protocol_yescaptcha_key` / `protocol_yescaptcha_endpoint` | Turnstile solver |
| `protocol_email_provider` | outlook/moemail/duckmail/yyds/cloudflare/cloudmail |
| `protocol_moemail_*` | MoeMail API |
| `cpa_auth_dir` / `cpa_base_url` | CPA JSON output |
| `protocol_*_timeout_sec` | mail/oauth timeouts |

When adding engine features, prefer extending `protocol_register.py` and this
table rather than scattering `sys.path` / `xconsole_client` imports elsewhere.

## Credential dumps

Engine may write plaintext dumps under:

- `protocol_engine/grok-build-auth/sso_output/`
- `protocol_engine/grok-build-auth/oauth_output/`

Use `scripts/purge_credentials.py` for retention cleanup. Do not commit these
directories (see root `.gitignore`).

## Dependency note

The engine ships its own `requirements.txt`. Prefer consolidating into the root
`uv.lock` over time; until then, treat engine deps as optional for protocol mode.
