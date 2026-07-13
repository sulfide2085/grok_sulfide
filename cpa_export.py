"""Register-machine hook: mint CPA xai auth after successful registration.

OIDC package lives at ./cpa_xai (bundled with this project).
Optional override: config `api_reverse_tools` / env `API_REVERSE_TOOLS`
points at a directory that *contains* the `cpa_xai` package.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

_REG_DIR = Path(__file__).resolve().parent
_DEFAULT_OUT = _REG_DIR / "cpa_auths"
_DEFAULT_CPA = Path("")  # empty = do not assume a machine-local CPA path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_cpa_proxy(cfg: dict) -> str:
    """Resolve CPA outbound proxy, supporting an explicit direct mode."""
    configured = str(cfg.get("cpa_proxy") or "").strip()
    if configured.lower() in {"direct", "none", "off", "disabled"}:
        return ""
    if configured:
        return configured
    fallback = str(cfg.get("proxy") or "").strip()
    if fallback:
        return fallback
    return (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or ""
    ).strip()


def upload_cpa_auth_file(
    auth_path: str | Path,
    config: dict,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Upload one auth JSON file through the CLIProxyAPI management API."""
    log = log_callback or (lambda m: print(m, flush=True))
    src = Path(auth_path).resolve()
    base = str(config.get("cpa_management_base") or "").strip().rstrip("/")
    key = str(config.get("cpa_management_key") or "").strip()
    if not base or not key:
        raise ValueError("CPA management 地址或密码未配置")
    if base.endswith("/v0/management"):
        base = base[: -len("/v0/management")]

    boundary = f"----grokreg{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{src.name}"\r\n'
            ).encode("utf-8"),
            b"Content-Type: application/json\r\n\r\n",
            src.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode("ascii"),
        ]
    )
    request = urllib.request.Request(
        f"{base}/v0/management/auth-files",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")
        status = int(getattr(response, "status", 200) or 200)
    if status < 200 or status >= 300:
        raise RuntimeError(f"CPA management upload HTTP {status}: {raw[:300]}")
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"response": raw[:300]}
    log(f"[CPA][管理API][OK] 上传成功 | 地址={base} | 文件={src.name}")
    return {"ok": True, "status": status, "response": payload}


def upload_cpa_auth_file_ssh(
    auth_path: str | Path,
    config: dict,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Upload one auth JSON through an existing passwordless SSH host alias."""
    log = log_callback or (lambda m: print(m, flush=True))
    src = Path(auth_path).resolve()
    host = str(config.get("cpa_ssh_host") or "").strip()
    remote_dir = str(config.get("cpa_ssh_auth_dir") or "").strip()
    if not host or not remote_dir:
        raise ValueError("CPA SSH host or auth directory is not configured")
    if host.startswith("-") or any(ch.isspace() for ch in host):
        raise ValueError("CPA SSH host must be a plain SSH host or alias")
    if not remote_dir.startswith("/") or "\x00" in remote_dir:
        raise ValueError("CPA SSH auth directory must be an absolute remote path")
    timeout = max(5, min(int(config.get("cpa_ssh_timeout_sec") or 30), 300))
    mode = str(config.get("cpa_ssh_chmod") or "600").strip()
    if mode not in {"600", "640", "644"}:
        raise ValueError("CPA SSH chmod must be 600, 640, or 644")

    local_sha256 = _sha256_file(src)
    remote_tmp = f"/tmp/.grok-sulfide-{uuid.uuid4().hex}-{src.name}"
    destination = remote_dir.rstrip("/") + "/" + src.name
    log(
        f"[CPA][SSH][START] 正在上传 | 本地={src.name} | "
        f"远端={host}:{destination}"
    )
    scp_target = f"{host}:{remote_tmp}"
    scp = subprocess.run(
        ["scp", "-q", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", str(src), scp_target],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 15,
        check=False,
    )
    if scp.returncode != 0:
        raise RuntimeError(f"scp failed: {(scp.stderr or scp.stdout).strip()[:300]}")

    command = (
        f"install -m {mode} -- {shlex.quote(remote_tmp)} {shlex.quote(destination)}"
        f" && rm -f -- {shlex.quote(remote_tmp)}"
        f" && sha256sum -- {shlex.quote(destination)}"
    )
    try:
        ssh = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", host, command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 15,
            check=False,
        )
    except Exception:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, f"rm -f -- {shlex.quote(remote_tmp)}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        raise
    if ssh.returncode != 0:
        raise RuntimeError(f"ssh install failed: {(ssh.stderr or ssh.stdout).strip()[:300]}")
    remote_sha256 = str(ssh.stdout or "").strip().split(maxsplit=1)[0].lower()
    if remote_sha256 != local_sha256:
        raise RuntimeError(
            "SSH upload checksum mismatch: "
            f"local={local_sha256[:12]} remote={remote_sha256[:12] or 'missing'}"
        )
    log(
        f"[CPA][SSH][OK] 上传并校验成功 | 远端={host}:{destination} | "
        f"SHA256={local_sha256[:12]}"
    )
    return {
        "ok": True,
        "host": host,
        "remote_path": destination,
        "sha256": local_sha256,
    }


def publish_cpa_auth_file(
    auth_path: str | Path,
    config: dict,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Run all independently enabled CPA publication targets."""
    log = log_callback or (lambda m: print(m, flush=True))
    src = Path(auth_path).resolve()
    local_sha256 = _sha256_file(src)
    result: dict[str, Any] = {"path": str(src), "sha256": local_sha256}
    states = {"hotload": "SKIP", "management": "SKIP", "ssh": "SKIP"}
    log(
        f"[CPA][本地][OK] 凭据已生成 | 文件={src.name} | "
        f"大小={src.stat().st_size}B | SHA256={local_sha256[:12]}"
    )

    hotload_raw = str(config.get("cpa_hotload_dir") or "").strip()
    if config.get("cpa_copy_to_hotload", False) and hotload_raw:
        try:
            cpa_dir = Path(hotload_raw).expanduser()
            if not cpa_dir.is_absolute():
                cpa_dir = (_REG_DIR / cpa_dir).resolve()
            cpa_dir.mkdir(parents=True, exist_ok=True)
            dst = cpa_dir / src.name
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            result["cpa_path"] = str(dst)
            states["hotload"] = "OK"
            log(f"[CPA][热加载][OK] 已复制 | 目标={dst}")
        except Exception as exc:  # noqa: BLE001
            result["cpa_copy_error"] = str(exc)
            states["hotload"] = "FAIL"
            log(f"[CPA][热加载][FAIL] 复制失败 | 原因={exc}")

    if config.get("cpa_management_upload_enabled", False):
        management_base = str(config.get("cpa_management_base") or "").strip()
        management_key = str(config.get("cpa_management_key") or "").strip()
        if not management_base or not management_key:
            reason = "地址或密钥未配置"
            result["cpa_management_upload"] = {"ok": False, "skipped": True, "reason": reason}
            log(f"[CPA][管理API][SKIP] {reason}，继续执行其他发布方式")
        else:
            log(f"[CPA][管理API][START] 正在上传 | 地址={management_base}")
            try:
                result["cpa_management_upload"] = upload_cpa_auth_file(src, config, log)
                states["management"] = "OK"
            except Exception as exc:  # noqa: BLE001
                result["cpa_management_upload_error"] = str(exc)
                states["management"] = "FAIL"
                log(f"[CPA][管理API][FAIL] 上传失败 | 原因={exc}")
    else:
        log("[CPA][管理API][SKIP] 未启用")

    if config.get("cpa_ssh_upload_enabled", False):
        try:
            result["cpa_ssh_upload"] = upload_cpa_auth_file_ssh(src, config, log)
            states["ssh"] = "OK"
        except Exception as exc:  # noqa: BLE001
            result["cpa_ssh_upload_error"] = str(exc)
            states["ssh"] = "FAIL"
            log(f"[CPA][SSH][FAIL] 上传或校验失败 | 原因={exc}")
    else:
        log("[CPA][SSH][SKIP] 未启用")

    remote_ok = states["management"] == "OK" or states["ssh"] == "OK"
    any_failure = "FAIL" in states.values()
    final = "OK" if remote_ok and not any_failure else "WARN" if remote_ok else "LOCAL_ONLY"
    log(
        f"[CPA][发布完成][{final}] 本地=OK | 管理API={states['management']} | "
        f"SSH={states['ssh']} | 热加载={states['hotload']}"
    )
    return result


def _ensure_cpa_xai_on_path(tools_dir: str | Path | None = None) -> Path:
    """Put the parent of `cpa_xai` on sys.path. Default: this project root."""
    if tools_dir:
        tools = Path(tools_dir).expanduser().resolve()
    else:
        env = (os.environ.get("API_REVERSE_TOOLS") or "").strip()
        tools = Path(env).expanduser().resolve() if env else _REG_DIR
    # If user pointed at .../cpa_xai itself, use its parent
    if tools.name == "cpa_xai" and (tools / "__init__.py").is_file():
        tools = tools.parent
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    return tools


def export_cookies_from_page(page: Any) -> list[dict]:
    """Best-effort export of cookies from a DrissionPage tab/browser."""
    if page is None:
        return []
    cookies = None
    for getter in (
        lambda: page.cookies(all_domains=True, all_info=True),
        lambda: page.cookies(all_domains=True),
        lambda: page.cookies(),
    ):
        try:
            cookies = getter()
            if cookies:
                break
        except TypeError:
            continue
        except Exception:
            continue
    if not cookies:
        try:
            browser = getattr(page, "browser", None)
            if browser is not None:
                cookies = browser.cookies()
        except Exception:
            cookies = None
    if isinstance(cookies, list):
        return [c for c in cookies if isinstance(c, dict)]
    return []


def export_cpa_xai_for_account(
    email: str,
    password: str,
    *,
    page: Any | None = None,
    cookies: Any | None = None,
    sso: str | None = None,
    config: dict | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Mint OIDC + write xai-<email>.json under register cpa_auths (and optional CPA auth-dir)."""
    cfg = config or {}
    log = log_callback or (lambda m: print(m, flush=True))

    if not cfg.get("cpa_export_enabled", True):
        log("[cpa] export disabled")
        return {"ok": False, "skipped": True, "reason": "disabled"}

    tools_dir = cfg.get("api_reverse_tools") or cfg.get("cpa_xai_parent") or None
    _ensure_cpa_xai_on_path(tools_dir)

    try:
        from cpa_xai import mint_and_export  # type: ignore
    except Exception as e:  # noqa: BLE001
        log(f"[cpa] import cpa_xai failed: {e}")
        return {"ok": False, "error": f"import: {e}"}

    out_dir = Path(cfg.get("cpa_auth_dir") or _DEFAULT_OUT).expanduser()
    if not out_dir.is_absolute():
        out_dir = (_REG_DIR / out_dir).resolve()

    # Priority: cpa_proxy > proxy > env. "direct" explicitly disables all proxies.
    proxy = resolve_cpa_proxy(cfg)
    # Default headed: headless is frequently Cloudflare-blocked on accounts.x.ai
    headless = bool(cfg.get("cpa_headless", False))
    probe = bool(cfg.get("cpa_probe_after_write", True))
    probe_chat = bool(cfg.get("cpa_probe_chat", False))
    timeout = float(cfg.get("cpa_mint_timeout_sec", 240))
    base_url = cfg.get("cpa_base_url") or "https://cli-chat-proxy.grok.com/v1"
    force_standalone = bool(cfg.get("cpa_force_standalone", True))
    cookie_inject = bool(cfg.get("cpa_mint_cookie_inject", True))
    reuse_browser = bool(cfg.get("cpa_mint_browser_reuse", True))
    recycle_every = int(cfg.get("cpa_mint_browser_recycle_every", 15) or 0)

    # cookies: explicit arg > page export > none
    use_cookies = cookies
    if use_cookies is None and cookie_inject and page is not None:
        use_cookies = export_cookies_from_page(page)
    if not cookie_inject:
        use_cookies = None
    else:
        # Always attach SSO cookie clones — register cookies alone often miss accounts.x.ai host
        sso_val = (sso or "").strip()
        if not sso_val and isinstance(use_cookies, list):
            for c in use_cookies:
                if isinstance(c, dict) and c.get("name") in ("sso", "sso-rw") and c.get("value"):
                    sso_val = str(c.get("value"))
                    break
        if sso_val:
            base = list(use_cookies) if isinstance(use_cookies, list) else []
            for name in ("sso", "sso-rw"):
                for dom in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai", "grok.com", ".grok.com"):
                    base.append({
                        "name": name,
                        "value": sso_val,
                        "domain": dom,
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    })
            use_cookies = base

    out_dir.mkdir(parents=True, exist_ok=True)
    log(
        f"[cpa] mint OIDC for {email} -> {out_dir} proxy={proxy or '(none)'} "
        f"cookies={len(use_cookies) if isinstance(use_cookies, list) else (1 if use_cookies else 0)} "
        f"reuse={reuse_browser}"
    )

    def _log(msg: str) -> None:
        log(f"[cpa] {msg}")

    result = mint_and_export(
        email=email,
        password=password,
        auth_dir=out_dir,
        page=None if force_standalone else page,
        proxy=proxy or None,
        headless=headless,
        base_url=base_url,
        probe=probe,
        probe_chat=probe_chat,
        browser_timeout_sec=timeout,
        force_standalone=force_standalone,
        cookies=use_cookies,
        reuse_browser=reuse_browser,
        recycle_every=recycle_every,
        log=_log,
    )

    if result.get("path"):
        result.update(publish_cpa_auth_file(result["path"], cfg, log_callback=log))

    # failure log under register dir
    if not result.get("ok"):
        fail_path = out_dir / "cpa_auth_failed.txt"
        with open(fail_path, "a", encoding="utf-8") as f:
            f.write(f"{email}----{result.get('error') or 'unknown'}----{int(time.time())}\n")
        if cfg.get("cpa_mint_required", False):
            raise RuntimeError(f"CPA mint required but failed: {result.get('error')}")

    return result
