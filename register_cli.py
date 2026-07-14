"""CLI wrapper for grok_register_ttk — multi-thread register + async CPA mint pipeline.

Architecture:
  Register workers (R)  →  accounts_cli + mint_queue
  Mint workers (M)      →  cpa_auths/xai-*.json + optional hotload

Browser lifecycle:
  - One Chromium per register worker, reused via TabPool.clear_session
  - Full recycle every N accounts or on error
  - Register browser released BEFORE mint (mint always standalone Chromium)
  - Peak browsers ≈ R + M (not 2×R)
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# 强制走本目录的 grok_register_ttk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import grok_register_ttk as reg  # noqa: E402
from failure_classify import FailureStats  # noqa: E402

_failure_stats = FailureStats()


# Linux 适配: DrissionPage 默认找 'chrome', 我们装的是 chromium
# 保留原版 slim flags + proxy，再补 chromium 路径与 turnstilePatch。
_orig_create_browser_options = reg.create_browser_options


def _patched_create_browser_options(*args, **kwargs):
    # Prefer original factory (proxy + CHROMIUM_SLIM_FLAGS + extension)
    try:
        opts = _orig_create_browser_options(*args, **kwargs)
    except Exception:
        from DrissionPage import ChromiumOptions

        opts = ChromiumOptions()
        opts.auto_port()
        opts.set_timeouts(base=1)
        for flag in getattr(reg, "CHROMIUM_SLIM_FLAGS", ()) or ():
            try:
                opts.set_argument(flag)
            except Exception:
                pass

    try:
        opts.auto_port()
    except Exception:
        pass
    try:
        opts.set_timeouts(base=1)
    except Exception:
        pass

    for cand in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if os.path.isfile(cand):
            try:
                opts.set_browser_path(cand)
            except Exception:
                pass
            break

    ext_path = os.path.join(os.path.dirname(os.path.abspath(reg.__file__)), "turnstilePatch")
    if os.path.isdir(ext_path):
        try:
            opts.add_extension(ext_path)
        except Exception:
            pass
    return opts


reg.create_browser_options = _patched_create_browser_options


# ── 线程安全日志 ──

_log_queue: queue.Queue = queue.Queue()


def _log_writer():
    while True:
        msg = _log_queue.get()
        if msg is None:
            break
        print(msg, flush=True)


def log(worker_id: int | str, msg: str) -> None:
    _log_queue.put(f"[{time.strftime('%H:%M:%S')}] [W{worker_id}] {msg}")


# ── 统计 ──

_stats_lock = threading.Lock()
_stats = {
    "reg_success": 0,
    "reg_fail": 0,
    "mint_success": 0,
    "mint_fail": 0,
    "mint_skip": 0,
}


def _inc(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


# forever 任务索引
_next_idx_lock = threading.Lock()
_next_idx = [1]

# mint 队列结束哨兵
_MINT_STOP = object()
_accounts_write_lock = threading.Lock()


def resolve_mint_workers(
    *,
    cli_value: int,
    threads: int,
    config: dict,
    inline_mint: bool,
) -> int:
    """Resolve mint worker count.

    Priority: --inline-mint > CLI --mint-workers (>=0) > config cpa_mint_workers > auto.
    auto (-1): min(threads, 4) when CPA export enabled, else 0.
    0: inline mint on register threads.
    """
    if inline_mint:
        return 0
    if cli_value >= 0:
        return max(0, min(int(cli_value), 10))
    cfg_v = config.get("cpa_mint_workers", -1)
    try:
        cfg_v = int(cfg_v)
    except Exception:
        cfg_v = -1
    if cfg_v >= 0:
        return max(0, min(cfg_v, 10))
    # auto
    if config.get("cpa_export_enabled", True):
        return max(1, min(int(threads), 4))
    return 0


def resolve_mint_queue_max(config: dict, mint_workers: int, cli_value: int | None = None) -> int:
    if cli_value is not None and cli_value >= 0:
        return int(cli_value)
    try:
        v = int(config.get("cpa_mint_queue_max", 0) or 0)
    except Exception:
        v = 0
    if v > 0:
        return v
    # default backpressure: 2 × mint workers (0 if no mint pool)
    return max(0, mint_workers * 2) if mint_workers > 0 else 0


class DummyStop:
    def __call__(self) -> bool:
        return False


def _ensure_browser(worker_id: int, force_recycle: bool = False):
    """Start browser if missing; optional full recycle."""
    if force_recycle:
        try:
            reg.stop_browser()
        except Exception:
            pass
    if reg.TabPool.get_browser() is None:
        reg.start_browser(log_callback=lambda m: log(worker_id, m))


def register_one(
    worker_id: int,
    idx: int,
    total: int,
    accounts_file: str,
    *,
    do_mint_inline: bool = False,
    mint_queue: queue.Queue | None = None,
) -> dict | None:
    """Run one registration. Enqueue CPA mint (default) instead of blocking.

    Returns dict(email, sso, profile) or None.
    """
    email = ""
    dev_token = ""
    max_mail_retry = 3
    cancel = DummyStop()

    try:
        try:
            reg.stop_browser()
        except Exception:
            pass
        proxy_account = reg.begin_registration_proxy_session(f"w{worker_id}_n{idx}")
        if proxy_account:
            log(worker_id, f"[*] Resin 粘性会话: {proxy_account}")
        _ensure_browser(worker_id, force_recycle=False)
    except Exception as exc:
        log(worker_id, f"! 浏览器启动失败: {exc}")
        return None

    max_mail_retry = 5
    for mail_try in range(1, max_mail_retry + 1):
        try:
            log(worker_id, f"--- 第 {idx}/{total} 个账号, 邮箱尝试 {mail_try}/{max_mail_retry} ---")
            log(worker_id, "1. 打开注册页")
            reg.open_signup_page(log_callback=lambda m: log(worker_id, m), cancel_callback=cancel)
            log(worker_id, "2. 创建邮箱并提交")
            email, dev_token = reg.fill_email_and_submit(
                log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
            )
            log(worker_id, f"邮箱: {email}")
            log(worker_id, "3. 拉取验证码")
            code = reg.fill_code_and_submit(
                email,
                dev_token,
                log_callback=lambda m: log(worker_id, m),
                cancel_callback=cancel,
            )
            log(worker_id, f"验证码: {code}")
            break
        except Exception as exc:
            msg = str(exc)
            existing_cls = getattr(reg, "EmailAlreadyRegisteredError", None)
            rate_cls = getattr(reg, "EmailOtpRateLimitedError", None)
            is_existing = (
                (existing_cls is not None and isinstance(exc, existing_cls))
                or "已存在账户" in msg
                or "account already exists" in msg.lower()
                or "already associated with an account" in msg.lower()
            )
            is_otp_rate = (
                (rate_cls is not None and isinstance(exc, rate_cls))
                or "验证码发送过多" in msg
                or "验证码过多" in msg
                or "otp_send_rate_limit" in msg
                or "too many verification codes" in msg.lower()
                or "too many codes" in msg.lower()
            )
            if is_existing or is_otp_rate:
                bad = getattr(exc, "email", "") or email
                reason = (
                    "xai_account_already_exists"
                    if is_existing
                    else f"otp_send_rate_limit:{msg[:100]}"
                )
                try:
                    reg.mark_error(bad, reason=reason)
                except Exception:
                    pass
                log(
                    worker_id,
                    f"! xAI {'已存在账户' if is_existing else '验证码发送过多/限流'}，已标记并换号: {bad}",
                )
                if mail_try < max_mail_retry:
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass
                    reg.sleep_with_cancel(1, cancel)
                    continue
            msg_l = msg.lower()
            is_oauth_dead = (
                "oauth 永久失败" in msg
                or "oauth2 refresh 失败" in msg_l
                or "aadsts" in msg_l
                or "compromised" in msg_l
                or "security interrupt" in msg_l
                or "invalid_grant" in msg_l
            )
            is_otp_miss = (
                ("未收到验证码" in msg or ("验证码邮件" in msg))
                and not is_oauth_dead
            )
            # Align with GUI: email-submit bounce / stuck form are session issues —
            # recycle browser and retry with a fresh mailbox instead of hard-failing.
            is_page_stuck = (
                "未进入验证码页" in msg
                or "仍在邮箱表单" in msg
                or "未找到邮箱输入框" in msg
                or "反复回到注册方式页" in msg
                or "表单卡住" in msg
                or "回到注册方式页" in msg
                or "未找到「使用邮箱注册」" in msg
                or "未找到邮箱输入框或注册按钮" in msg
            )
            if (is_oauth_dead or is_otp_miss or is_page_stuck) and mail_try < max_mail_retry:
                if is_oauth_dead or is_otp_miss:
                    try:
                        reg.mark_error(
                            email or "",
                            reason=(
                                f"hotmail_oauth_dead:{msg[:100]}"
                                if is_oauth_dead
                                else f"otp_timeout:{msg[:80]}"
                            ),
                        )
                    except Exception:
                        pass
                    log(
                        worker_id,
                        f"! 邮箱读信失败，标记后换号"
                        f"{'（微软 OAuth/安全锁定）' if is_oauth_dead else ''}：{msg}",
                    )
                else:
                    log(
                        worker_id,
                        f"! 邮箱提交页卡住，重开浏览器重试 ({mail_try}/{max_mail_retry}): {msg}",
                    )
                try:
                    reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                except Exception:
                    pass
                reg.sleep_with_cancel(1, cancel)
                continue
            log(worker_id, f"! 邮箱阶段失败: {msg}")
            traceback.print_exc()
            _inc("reg_fail")
            try:
                if email:
                    reg.mark_error(email, reason=msg[:120])
            except Exception:
                pass
            try:
                reg.restart_browser(log_callback=lambda m: log(worker_id, m))
            except Exception:
                pass
            return None

    try:
        log(worker_id, "4. 填写资料")
        profile = reg.fill_profile_and_submit(
            log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
        )
        log(worker_id, f"资料已填: {profile.get('given_name')} {profile.get('family_name')}")
        log(worker_id, "5. 等待 sso cookie")
        sso = reg.wait_for_sso_cookie(
            log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
        )
        password = profile.get("password", "") or ""
        line = f"{email}----{password}----{sso}\n"
        with open(accounts_file, "a", encoding="utf-8") as f:
            f.write(line)
        log(worker_id, f"+ 注册成功: {email}")
        reg.mark_used(email, password)

        # Capture cookies BEFORE releasing browser (for mint cookie inject)
        page = reg._get_page()
        cookies = []
        try:
            import cpa_export as _cpa_exp

            cookies = _cpa_exp.export_cookies_from_page(page) if page is not None else []
        except Exception:
            cookies = []
        if cookies:
            log(worker_id, f"[*] 导出 cookie {len(cookies)} 条供 mint 注入")

        if page and reg.PERF_FLAGS.get("cookie_snapshot", True):
            try:
                reg.save_cookies_snapshot(page, "success", email)
            except Exception:
                pass
        try:
            reg.add_token_to_grok2api_pools(
                sso, email=email, log_callback=lambda m: log(worker_id, m)
            )
        except Exception as exc:
            log(worker_id, f"[Debug] grok2api: {exc}")

        # Release / recycle register browser BEFORE mint so peak browsers ≈ R+M
        try:
            reg.prepare_browser_for_next_account(log_callback=lambda m: log(worker_id, m))
        except Exception:
            try:
                reg.stop_browser()
            except Exception:
                pass

        job = {
            "email": email,
            "password": password,
            "sso": sso,
            "profile": profile,
            "idx": idx,
            "cookies": cookies,
        }

        if do_mint_inline:
            _run_mint_job(f"R{worker_id}", job, getattr(reg, "config", {}) or {})
        elif mint_queue is not None:
            # backpressure: wait while queue is saturated
            qmax = int(getattr(mint_queue, "_reg_qmax", 0) or 0)
            while qmax > 0 and mint_queue.qsize() >= qmax:
                log(worker_id, f"[cpa] mint 队列背压 qsize={mint_queue.qsize()}≥{qmax}，等待...")
                time.sleep(1.0)
            mint_queue.put(job)
            log(worker_id, f"[cpa] enqueued mint for {email} (queue≈{mint_queue.qsize()})")
        else:
            log(worker_id, "[cpa] mint skipped (no queue / inline)")

        _inc("reg_success")
        return job
    except Exception as exc:
        cls = _failure_stats.record(exc)
        log(worker_id, f"! 注册失败 [{cls.value}]: {exc}")
        reg.mark_error(email or "", reason=str(exc)[:120])
        traceback.print_exc()
        _inc("reg_fail")
        try:
            reg.restart_browser(log_callback=lambda m: log(worker_id, m))
        except Exception:
            pass
        return None


def _run_mint_job(worker_id: int | str, job: dict[str, Any], config: dict) -> dict:
    """Standalone CPA mint (own Chromium). Never reuses register browser."""
    email = job.get("email") or ""
    password = job.get("password") or ""
    if not email or not password:
        _inc("mint_fail")
        return {"ok": False, "error": "missing email/password", "email": email}
    if not config.get("cpa_export_enabled", True):
        _inc("mint_skip")
        log(worker_id, f"[cpa] export disabled, skip {email}")
        return {"ok": False, "skipped": True, "email": email}
    try:
        import cpa_export

        # page=None always — force standalone path inside export
        result = cpa_export.export_cpa_xai_for_account(
            email,
            password,
            page=None,
            cookies=job.get("cookies"),
            sso=job.get("sso") or "",
            config=config,
            log_callback=lambda m: log(worker_id, m),
        )
        if result.get("ok"):
            log(worker_id, f"+ CPA auth: {result.get('path')}")
            _inc("mint_success")
        elif result.get("skipped"):
            _inc("mint_skip")
            log(worker_id, f"[cpa] skipped: {result.get('reason')}")
        else:
            _inc("mint_fail")
            log(worker_id, f"! CPA auth 未成功: {result.get('error') or result}")
        return result
    except Exception as exc:
        _inc("mint_fail")
        log(worker_id, f"! CPA export 异常: {exc}")
        traceback.print_exc()
        return {"ok": False, "error": str(exc), "email": email}


def _register_worker(
    worker_id: int,
    task_queue: queue.Queue,
    total: int,
    accounts_file: str,
    mint_queue: queue.Queue | None,
    forever: bool,
    do_mint_inline: bool,
):
    while True:
        try:
            idx = task_queue.get_nowait()
        except queue.Empty:
            if not forever:
                break
            with _next_idx_lock:
                nxt = _next_idx[0]
                _next_idx[0] = nxt + 5
            for i in range(nxt, nxt + 5):
                task_queue.put(i)
            continue

        retry = 0
        while retry < 2:
            try:
                result = register_one(
                    worker_id,
                    idx,
                    total,
                    accounts_file,
                    do_mint_inline=do_mint_inline,
                    mint_queue=mint_queue,
                )
                if result:
                    break
                retry += 1
                if retry < 2:
                    log(worker_id, f"[retry] 账号 {idx} 失败，重试 {retry}/1")
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass
            except Exception:
                retry += 1
                if retry < 2:
                    log(worker_id, f"[retry] 账号 {idx} 异常，重试 {retry}/1")
                    traceback.print_exc()
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass

        if retry >= 2:
            # register_one already counted fail on exception path; if both returned None, count once more only if needed
            pass

    # worker exit: free browser
    try:
        reg.stop_browser()
    except Exception:
        pass
    log(worker_id, "register worker exit")


def _mint_worker(worker_id: str, mint_queue: queue.Queue, config: dict):
    while True:
        job = mint_queue.get()
        try:
            if job is _MINT_STOP:
                break
            if not isinstance(job, dict):
                continue
            _run_mint_job(worker_id, job, config)
        finally:
            mint_queue.task_done()
    try:
        from cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass
    log(worker_id, "mint worker exit")


def _run_protocol_registration(
    *,
    remaining: int,
    done_count: int,
    threads: int,
    accounts_file: str,
    config: dict,
) -> int:
    """Run the vendored grok-build-auth protocol flow without Chromium."""
    from protocol_register import register_one_protocol
    from cpa_export import publish_cpa_auth_file

    success = 0
    failed = 0

    def run(index: int) -> dict[str, Any]:
        worker = ((index - done_count - 1) % threads) + 1
        result = register_one_protocol(
            index,
            config,
            log=lambda message: log(f"P{worker}", message),
        )
        line = f"{result['email']}----{result['password']}----{result['sso']}\n"
        with _accounts_write_lock:
            with open(accounts_file, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        publish = {}
        if result.get("cpa_path"):
            publish = publish_cpa_auth_file(
                result["cpa_path"],
                config,
                log_callback=lambda message: log(f"P{worker}", message),
            )
        elif result.get("partial"):
            log(f"P{worker}", "[protocol] account saved; SSO/CPA pending backfill")
        result["publish"] = publish
        return result

    log_thread = threading.Thread(target=_log_writer, daemon=True)
    log_thread.start()
    print(
        f"[*] registration_method=protocol, jobs={remaining}, threads={threads}, "
        "engine=vendored grok-build-auth",
        flush=True,
    )
    try:
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="protocol-reg") as pool:
            futures = {
                pool.submit(run, done_count + offset): done_count + offset
                for offset in range(1, remaining + 1)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    result = future.result()
                    success += 1
                    suffix = " (partial: SSO/CPA pending)" if result.get("partial") else ""
                    log("P", f"+ protocol registration complete #{index}: {result['email']}{suffix}")
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    cls = _failure_stats.record(exc)
                    log("P", f"! protocol registration failed [{cls.value}] #{index}: {exc}")
                    traceback.print_exc()
    except KeyboardInterrupt:
        print("\n[!] 用户中断", flush=True)
        failed += max(0, remaining - success - failed)
    finally:
        _log_queue.put(None)
        log_thread.join(timeout=2)
    print(f"=== 完成: 协议注册成功 {success}, 失败 {failed} ===", flush=True)
    if failed:
        print(f"=== 失败分类: {_failure_stats.summary()} ===", flush=True)
    return 0 if failed == 0 else 1


def main() -> int:
    try:
        import logging_setup

        logging_setup.init()
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="CLI runner for grok_register_ttk (pipelined).")
    parser.add_argument("--count", type=int, default=1, help="账号总数目标（0=不限；含已有）")
    parser.add_argument(
        "--extra",
        type=int,
        default=0,
        help="在已有 accounts 基础上再新注册 N 个",
    )
    parser.add_argument("--threads", type=int, default=1, help="注册并发线程数（1-10）")
    parser.add_argument(
        "--mint-workers",
        type=int,
        default=-1,
        help="CPA mint 并发：-1=用 config/auto；0=内联；1-10=固定。覆盖 config.cpa_mint_workers",
    )
    parser.add_argument(
        "--mint-queue-max",
        type=int,
        default=-1,
        help="mint 队列背压上限：-1=用 config/auto(2×workers)；0=不限制",
    )
    parser.add_argument("--accounts-file", default=os.path.join(os.path.dirname(__file__), "accounts_cli.txt"))
    parser.add_argument(
        "--registration-method",
        choices=("browser", "protocol"),
        default=None,
        help="注册方式；默认读取 config.registration_method",
    )
    parser.add_argument("--preset", default="", help="读取 config.registration_presets 中的预设")
    parser.add_argument(
        "--alias-mode",
        choices=("primary", "random", "sequential"),
        default=None,
        help="本次运行覆盖 Outlook 选号模式",
    )
    parser.add_argument("--alias-limit", type=int, default=None, help="本次运行每个主邮箱允许的别名数量")
    parser.add_argument("--fast", action="store_true", default=True, help="快速模式（默认开）：压缩 sleep、关截图")
    parser.add_argument("--no-fast", action="store_true", help="关闭快速模式")
    parser.add_argument("--no-browser-reuse", action="store_true", help="每号强制 quit 浏览器")
    parser.add_argument("--browser-recycle-every", type=int, default=25, help="复用 N 次后完整回收")
    parser.add_argument("--cookie-snapshot", action="store_true", help="注册成功写 cookie 快照（默认关，fast）")
    parser.add_argument("--inline-mint", action="store_true", help="强制注册线程内联 mint（调试用）")
    args = parser.parse_args()

    reg.load_config()
    if args.preset:
        presets = getattr(reg, "config", {}).get("registration_presets") or {}
        preset = presets.get(args.preset) if isinstance(presets, dict) else None
        values = preset.get("values") if isinstance(preset, dict) else None
        if not isinstance(values, dict):
            print(f"[!] 注册预设不存在: {args.preset}", flush=True)
            return 2
        reg.config.update(values)
    if args.alias_mode:
        reg.config["hotmail_alias_mode"] = args.alias_mode
    if args.alias_limit is not None:
        reg.config["hotmail_max_aliases_per_account"] = max(1, min(args.alias_limit, 1000))
    cfg0 = getattr(reg, "config", {}) or {}
    registration_method = str(
        args.registration_method or cfg0.get("registration_method") or "browser"
    ).strip().lower()
    threads = max(1, min(args.threads, 10))
    fast = bool(args.fast) and not bool(args.no_fast)

    mint_workers = resolve_mint_workers(
        cli_value=args.mint_workers,
        threads=threads,
        config=cfg0,
        inline_mint=bool(args.inline_mint),
    )
    do_mint_inline = mint_workers == 0
    mint_qmax = resolve_mint_queue_max(
        cfg0,
        mint_workers,
        cli_value=(None if args.mint_queue_max < 0 else args.mint_queue_max),
    )

    # perf knobs
    reg.configure_perf(
        fast=fast,
        sleep_scale=0.15 if fast else 1.0,
        skip_debug_io=fast,
        cookie_snapshot=bool(args.cookie_snapshot) or not fast,
        async_side_effects=True,
        browser_reuse=not args.no_browser_reuse,
        browser_recycle_every=max(1, int(args.browser_recycle_every)),
    )

    # 断点续跑
    done_count = 0
    if os.path.exists(args.accounts_file):
        with open(args.accounts_file) as f:
            done_count = sum(1 for line in f if line.strip())

    if args.extra and args.extra > 0:
        target_total = done_count + args.extra
        remaining = args.extra
        print(
            f"[*] 配置加载完成，额外新注册 {args.extra} 个（当前已有 {done_count} → 目标 {target_total}），"
            f"注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
        args.count = target_total
    elif args.count == 0:
        remaining = None
        print(
            f"[*] 配置加载完成，不限数量，注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
    else:
        remaining = max(0, args.count - done_count)
        print(
            f"[*] 配置加载完成，目标 {args.count} 个账号，注册线程={threads} "
            f"mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
    print(f"[*] accounts_file = {args.accounts_file}", flush=True)
    if done_count > 0:
        print(f"[*] 断点续跑：已完成 {done_count}", flush=True)
    if remaining is not None and remaining <= 0:
        print("[*] 所有账号已完成，无需继续（可用 --extra N 再注册）", flush=True)
        return 0

    if registration_method == "protocol":
        if remaining is None:
            print("[!] protocol 模式不支持 --count 0 无限任务，请使用 --extra N", flush=True)
            return 2
        return _run_protocol_registration(
            remaining=remaining,
            done_count=done_count,
            threads=threads,
            accounts_file=args.accounts_file,
            config=cfg0,
        )

    log_thread = threading.Thread(target=_log_writer, daemon=True)
    log_thread.start()

    try:
        reg.TabPool.init(reg.create_browser_options, log_callback=lambda m: log(0, m))
    except Exception as exc:
        print(f"[!] 浏览器初始化失败: {exc}", flush=True)
        return 1

    task_queue: queue.Queue = queue.Queue()
    mint_queue: queue.Queue | None = queue.Queue() if not do_mint_inline else None
    if mint_queue is not None:
        mint_queue._reg_qmax = mint_qmax  # type: ignore[attr-defined]
    global _next_idx
    _next_idx[0] = done_count + 1
    if remaining is not None:
        for i in range(done_count + 1, args.count + 1):
            task_queue.put(i)
    else:
        for i in range(done_count + 1, done_count + threads * 5 + 1):
            task_queue.put(i)
        _next_idx[0] = done_count + threads * 5 + 1

    forever = remaining is None
    cfg = getattr(reg, "config", {}) or {}

    # mint workers first (so queue consumers ready)
    mint_threads: list[threading.Thread] = []
    if mint_queue is not None and mint_workers > 0:
        for i in range(1, mint_workers + 1):
            wid = f"M{i}"
            t = threading.Thread(
                target=_mint_worker,
                args=(wid, mint_queue, cfg),
                daemon=True,
                name=f"mint-{i}",
            )
            t.start()
            mint_threads.append(t)

    reg_threads: list[threading.Thread] = []
    for wid in range(1, threads + 1):
        t = threading.Thread(
            target=_register_worker,
            args=(wid, task_queue, args.count, args.accounts_file, mint_queue, forever, do_mint_inline),
            daemon=True,
            name=f"reg-{wid}",
        )
        t.start()
        reg_threads.append(t)

    try:
        for t in reg_threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[!] 用户中断", flush=True)

    # drain mint queue
    if mint_queue is not None:
        log(0, f"[cpa] 等待 mint 队列清空（qsize≈{mint_queue.qsize()}）...")
        mint_queue.join()
        for _ in mint_threads:
            mint_queue.put(_MINT_STOP)
        for t in mint_threads:
            t.join(timeout=600)

    try:
        reg.shutdown_browser()
    except Exception:
        pass

    # stop side-effect pool
    try:
        pool = getattr(reg, "_side_effect_pool", None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

    _log_queue.put(None)
    log_thread.join(timeout=2)

    with _stats_lock:
        s = dict(_stats)
    print(
        f"=== 完成: 注册成功 {s.get('reg_success', 0)}, 注册失败 {s.get('reg_fail', 0)}, "
        f"CPA成功 {s.get('mint_success', 0)}, CPA失败 {s.get('mint_fail', 0)}, "
        f"CPA跳过 {s.get('mint_skip', 0)} ===",
        flush=True,
    )
    if s.get("reg_fail", 0):
        print(f"=== 失败分类: {_failure_stats.summary()} ===", flush=True)
    return 0 if s.get("reg_success", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
