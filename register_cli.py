"""CLI wrapper for grok_register_ttk — multi-thread register + async CPA mint pipeline.

Architecture:
  Register workers (R)  →  accounts_cli + mint_queue
  Mint workers (M)      →  cpa_auths/xai-*.json + optional hotload

Browser lifecycle:
  - One Chromium per register worker thread (browser.BrowserSession / TabPool)
  - Sessions are not shared across threads; multi-thread = multi-session
  - Reuse via TabPool.clear_session; full recycle every N accounts or on error
  - Register browser released BEFORE mint (mint always standalone Chromium)
  - Peak browsers ≈ R + M (not 2×R)
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading

import grok_register_ttk as reg
from cli_runtime import (  # noqa: F401
    _MINT_STOP,
    _ensure_browser,
    _failure_stats,
    _inc,
    _log_queue,
    _log_writer,
    _mint_worker,
    _next_idx,
    _register_worker,
    _run_mint_job,
    _run_protocol_registration,
    _stats,
    _stats_lock,
    log,
    register_one,
    resolve_mint_queue_max,
    resolve_mint_workers,
)

logger = logging.getLogger("grok_sulfide.cli")


def main() -> int:
    try:
        import logging_setup

        logging_setup.init()
    except Exception:
        logger.debug("suppressed exception", exc_info=True)
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
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="跳过开跑前健康探针（邮箱配置/注册页可达性）",
    )
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
    if not args.skip_preflight:
        try:
            from healthcheck import HealthCheckError, run_preflight

            print("[*] preflight: running health checks...", flush=True)
            results = run_preflight(cfg0)
            for item in results:
                print(f"[*] preflight ok: {item}", flush=True)
        except HealthCheckError as exc:
            print(f"[!] preflight failed: {exc}", flush=True)
            return 3
        except Exception as exc:
            print(f"[!] preflight unexpected error: {exc}", flush=True)
            return 3
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
        from tab_pool import TabPool

        TabPool.init(reg.create_browser_options, log_callback=lambda m: log(0, m))
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
        logger.debug("suppressed exception", exc_info=True)

    # stop side-effect pool
    try:
        pool = getattr(reg, "_side_effect_pool", None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        logger.debug("suppressed exception", exc_info=True)

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
