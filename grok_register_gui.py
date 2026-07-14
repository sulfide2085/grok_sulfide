#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Grok 注册机 - 桌面 GUI（薄壳，业务逻辑委托 grok_register_ttk）。"""

from __future__ import annotations

import datetime
import os
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

import grok_register_ttk as reg


class GrokRegisterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Grok 注册机")
        self.root.geometry("980x860")
        self.root.minsize(900, 760)
        self.is_running = False
        self.batch_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.stop_requested = False
        self.ui_queue = queue.Queue()
        self.accounts_output_file = ""
        self.stats_lock = threading.Lock()
        self._tutorial_window = None
        self.setup_ui()
        self.root.after(200, self._maybe_show_tutorial_on_start)

    def setup_ui(self):
        reg.load_config()
        cfg = reg.config
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        config_frame = ttk.LabelFrame(main_frame, text="配置", padding=10)
        config_frame.pack(fill=tk.X, pady=5)
        ttk.Label(config_frame, text="邮箱服务商:").grid(row=0, column=0, sticky=tk.W)
        self.email_provider_var = tk.StringVar(value=cfg.get("email_provider", "hotmail"))
        self.email_provider_combo = ttk.Combobox(
            config_frame,
            textvariable=self.email_provider_var,
            values=["hotmail", "duckmail", "yyds", "cloudflare", "cloudmail"],
            width=12,
            state="readonly",
        )
        self.email_provider_combo.grid(row=0, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="注册数量:").grid(row=0, column=2, sticky=tk.W, padx=10)
        self.count_var = tk.StringVar(value=str(cfg.get("register_count", 1)))
        self.count_spinbox = ttk.Spinbox(config_frame, from_=1, to=100, width=8, textvariable=self.count_var)
        self.count_spinbox.grid(row=0, column=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="并发线程:").grid(row=1, column=2, sticky=tk.W, padx=10)
        self.thread_var = tk.StringVar(value=str(cfg.get("register_threads", 1)))
        self.thread_spinbox = ttk.Spinbox(config_frame, from_=1, to=10, width=8, textvariable=self.thread_var)
        self.thread_spinbox.grid(row=1, column=3, sticky=tk.W, padx=5)
        self.nsfw_var = tk.BooleanVar(value=cfg.get("enable_nsfw", True))
        self.nsfw_check = ttk.Checkbutton(config_frame, text="注册后开启 NSFW", variable=self.nsfw_var)
        self.nsfw_check.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Label(config_frame, text="代理（可选）:").grid(row=2, column=0, sticky=tk.W)
        self.proxy_var = tk.StringVar(value=cfg.get("proxy", "http://127.0.0.1:7890"))
        self.proxy_entry = ttk.Entry(config_frame, textvariable=self.proxy_var, width=22)
        self.proxy_entry.grid(row=2, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CPA mint 代理:").grid(row=2, column=2, sticky=tk.W, padx=10)
        self.cpa_proxy_var = tk.StringVar(value=str(cfg.get("cpa_proxy") or cfg.get("proxy") or "http://127.0.0.1:7890"))
        self.cpa_proxy_entry = ttk.Entry(config_frame, textvariable=self.cpa_proxy_var, width=18)
        self.cpa_proxy_entry.grid(row=2, column=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="DuckMail API Key:").grid(row=3, column=0, sticky=tk.W)
        self.api_key_var = tk.StringVar(value=cfg.get("duckmail_api_key", ""))
        self.api_key_entry = ttk.Entry(config_frame, textvariable=self.api_key_var, width=30)
        self.api_key_entry.grid(row=3, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Base:").grid(row=4, column=0, sticky=tk.W)
        self.cloudflare_api_base_var = tk.StringVar(value=cfg.get("cloudflare_api_base", ""))
        self.cloudflare_api_base_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_base_var, width=30)
        self.cloudflare_api_base_entry.grid(row=4, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Key:").grid(row=5, column=0, sticky=tk.W)
        self.cloudflare_api_key_var = tk.StringVar(value=cfg.get("cloudflare_api_key", ""))
        self.cloudflare_api_key_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_key_var, width=30)
        self.cloudflare_api_key_entry.grid(row=5, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare 鉴权模式:").grid(row=6, column=0, sticky=tk.W)
        self.cloudflare_auth_mode_var = tk.StringVar(value=cfg.get("cloudflare_auth_mode", "bearer"))
        self.cloudflare_auth_mode_combo = ttk.Combobox(
            config_frame,
            textvariable=self.cloudflare_auth_mode_var,
            values=["query-key", "bearer", "x-api-key", "none"],
            width=12,
            state="readonly",
        )
        self.cloudflare_auth_mode_combo.grid(row=6, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CF 路径(domains/accounts/token/messages):").grid(row=7, column=0, sticky=tk.W)
        self.cloudflare_paths_var = tk.StringVar(
            value=",".join(
                [
                    cfg.get("cloudflare_path_domains", "/domains"),
                    cfg.get("cloudflare_path_accounts", "/accounts"),
                    cfg.get("cloudflare_path_token", "/token"),
                    cfg.get("cloudflare_path_messages", "/messages"),
                ]
            )
        )
        self.cloudflare_paths_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_paths_var, width=30)
        self.cloudflare_paths_entry.grid(row=7, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail URL:").grid(row=8, column=0, sticky=tk.W)
        self.cloudmail_url_var = tk.StringVar(value=str(cfg.get("cloudmail_url", "")))
        self.cloudmail_url_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_url_var, width=30)
        self.cloudmail_url_entry.grid(row=8, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员邮箱:").grid(row=9, column=0, sticky=tk.W)
        self.cloudmail_admin_email_var = tk.StringVar(value=str(cfg.get("cloudmail_admin_email", "")))
        self.cloudmail_admin_email_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_admin_email_var, width=30)
        self.cloudmail_admin_email_entry.grid(row=9, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员密码:").grid(row=10, column=0, sticky=tk.W)
        self.cloudmail_password_var = tk.StringVar(value=str(cfg.get("cloudmail_password", "")))
        self.cloudmail_password_entry = ttk.Entry(
            config_frame, textvariable=self.cloudmail_password_var, width=30, show="*"
        )
        self.cloudmail_password_entry.grid(row=10, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 本地自动入池:").grid(row=11, column=0, sticky=tk.W)
        self.grok2api_local_auto_var = tk.BooleanVar(value=bool(cfg.get("grok2api_auto_add_local", True)))
        self.grok2api_local_auto_check = ttk.Checkbutton(config_frame, variable=self.grok2api_local_auto_var)
        self.grok2api_local_auto_check.grid(row=11, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 本地 token.json:").grid(row=12, column=0, sticky=tk.W)
        self.grok2api_local_file_var = tk.StringVar(value=str(cfg.get("grok2api_local_token_file", "")))
        self.grok2api_local_file_entry = ttk.Entry(config_frame, textvariable=self.grok2api_local_file_var, width=30)
        self.grok2api_local_file_entry.grid(row=12, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 池名:").grid(row=13, column=0, sticky=tk.W)
        self.grok2api_pool_name_var = tk.StringVar(value=str(cfg.get("grok2api_pool_name", "ssoBasic")))
        self.grok2api_pool_name_combo = ttk.Combobox(
            config_frame,
            textvariable=self.grok2api_pool_name_var,
            values=["ssoBasic", "ssoSuper"],
            width=12,
            state="readonly",
        )
        self.grok2api_pool_name_combo.grid(row=13, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端自动入池:").grid(row=14, column=0, sticky=tk.W)
        self.grok2api_remote_auto_var = tk.BooleanVar(value=bool(cfg.get("grok2api_auto_add_remote", False)))
        self.grok2api_remote_auto_check = ttk.Checkbutton(config_frame, variable=self.grok2api_remote_auto_var)
        self.grok2api_remote_auto_check.grid(row=14, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端 Base:").grid(row=15, column=0, sticky=tk.W)
        self.grok2api_remote_base_var = tk.StringVar(value=str(cfg.get("grok2api_remote_base", "")))
        self.grok2api_remote_base_entry = ttk.Entry(config_frame, textvariable=self.grok2api_remote_base_var, width=30)
        self.grok2api_remote_base_entry.grid(row=15, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端 app_key:").grid(row=16, column=0, sticky=tk.W)
        self.grok2api_remote_key_var = tk.StringVar(value=str(cfg.get("grok2api_remote_app_key", "")))
        self.grok2api_remote_key_entry = ttk.Entry(config_frame, textvariable=self.grok2api_remote_key_var, width=30)
        self.grok2api_remote_key_entry.grid(row=16, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="默认域名(defaultDomains):").grid(row=17, column=0, sticky=tk.W)
        self.default_domains_var = tk.StringVar(value=str(cfg.get("defaultDomains", "")))
        self.default_domains_entry = ttk.Entry(config_frame, textvariable=self.default_domains_var, width=30)
        self.default_domains_entry.grid(row=17, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Hotmail 凭证文件:").grid(row=18, column=0, sticky=tk.W)
        self.hotmail_accounts_file_var = tk.StringVar(
            value=str(cfg.get("hotmail_accounts_file", "mail_credentials.txt"))
        )
        self.hotmail_accounts_file_entry = ttk.Entry(
            config_frame, textvariable=self.hotmail_accounts_file_var, width=30
        )
        self.hotmail_accounts_file_entry.grid(row=18, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Hotmail 别名模式:").grid(row=19, column=0, sticky=tk.W)
        self.hotmail_alias_mode_var = tk.StringVar(value=str(cfg.get("hotmail_alias_mode", "primary")))
        self.hotmail_alias_mode_combo = ttk.Combobox(
            config_frame,
            textvariable=self.hotmail_alias_mode_var,
            values=["primary", "random", "sequential"],
            width=12,
            state="readonly",
        )
        self.hotmail_alias_mode_combo.grid(row=19, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="每账号最大别名:").grid(row=19, column=2, sticky=tk.W, padx=10)
        self.hotmail_max_aliases_var = tk.StringVar(value=str(cfg.get("hotmail_max_aliases_per_account", 1)))
        self.hotmail_max_aliases_spin = ttk.Spinbox(
            config_frame, from_=1, to=50, width=8, textvariable=self.hotmail_max_aliases_var
        )
        self.hotmail_max_aliases_spin.grid(row=19, column=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CPA 导出:").grid(row=20, column=0, sticky=tk.W)
        self.cpa_export_var = tk.BooleanVar(value=bool(cfg.get("cpa_export_enabled", True)))
        self.cpa_export_check = ttk.Checkbutton(config_frame, variable=self.cpa_export_var)
        self.cpa_export_check.grid(row=20, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CPA 目录:").grid(row=20, column=2, sticky=tk.W, padx=10)
        self.cpa_auth_dir_var = tk.StringVar(value=str(cfg.get("cpa_auth_dir", "./cpa_auths")))
        self.cpa_auth_dir_entry = ttk.Entry(config_frame, textvariable=self.cpa_auth_dir_var, width=18)
        self.cpa_auth_dir_entry.grid(row=20, column=3, sticky=tk.W, padx=5)
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)
        self.start_btn = ttk.Button(btn_frame, text="开始注册", command=self.start_registration)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop_registration, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.clear_btn = ttk.Button(btn_frame, text="清空日志", command=self.clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=5)
        self.help_btn = ttk.Button(btn_frame, text="教程", command=self.show_tutorial)
        self.help_btn.pack(side=tk.LEFT, padx=5)
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=5)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, text="状态: ").pack(side=tk.LEFT)
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, foreground="green")
        self.status_label.pack(side=tk.LEFT)
        self.stats_var = tk.StringVar(value="成功: 0 | 失败: 0")
        ttk.Label(status_frame, textvariable=self.stats_var).pack(side=tk.RIGHT)
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=60)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        yview = self.log_text.yview()
        at_bottom = bool(yview) and yview[1] >= 0.999
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        if at_bottom:
            self.log_text.see(tk.END)

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def update_stats(self):
        self.stats_var.set(f"成功: {self.success_count} | 失败: {self.fail_count}")

    def _set_running_ui(self, running):
        self.is_running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("运行中..." if running else "就绪")
        self.status_label.config(foreground="blue" if running else "green")

    def _maybe_show_tutorial_on_start(self):
        if bool(reg.config.get("show_tutorial_on_start", True)):
            self.show_tutorial()

    def _tutorial_text(self):
        return """欢迎使用 Grok 注册机。建议按下面顺序填写（从最关键到可选）：

【第一步：先确定邮箱后端信息从哪里来】
如果你使用 cloudflare 模式（你当前主要是这套），先去你的临时邮箱服务配置接口查信息：
- 常见接口: /open_api/settings、/api/settings、/health_check
- 重点字段:
  - api_base（对应本工具的 Cloudflare API Base）
  - domains / defaultDomains（可用域名）
  - needAuth（是否需要鉴权）
  - admin_password 或 api_key（需要鉴权时使用）
  - provider.type（应为 cloudflare_temp_email）

【第二步：先填最小可运行配置】
1) 邮箱服务商
- duckmail: 需要 DuckMail API Key
- yyds: 需要 YYDS API Key 或 JWT
- cloudflare: 需要 Cloudflare API Base（cloudflare_temp_email 临时邮箱）
- cloudmail: 需要 CloudMail URL + 密码 + defaultDomains（maillab/cloud-mail 完整邮箱）

2) Cloudflare API Base（cloudflare 模式必填）
- 示例: https://xxxx.pages.dev
- 填写规则: 与 settings 接口中的 api_base 保持一致

3) 默认域名(defaultDomains)
- 填写你要优先使用的域名
- 支持单域名或逗号分隔多域名轮换
- 示例: a.com,b.com

4) CF 路径(domains/accounts/token/messages)
- 必须与后端真实路由一致
- 常见新路径:
  - /api/domains,/api/new_address,/api/token,/api/mails
- 常见旧路径:
  - /domains,/accounts,/token,/messages

5) Cloudflare API Key / 鉴权模式
- needAuth=false: 通常鉴权模式选 none，key 可留空
- needAuth=true: 按后端要求填 key，并选择 bearer/x-api-key/query-key

6) CloudMail 模式配置（maillab/cloud-mail 部署）
- CloudMail URL: 你的 Worker 地址，如 https://mail.xxx.workers.dev
- CloudMail 管理员邮箱: 管理员账号，如 admin@yourdomain.com
- CloudMail 管理员密码: 管理员密码（用于获取公开 API token 查询邮件）
- defaultDomains: 必须填写可用域名，如 yourdomain.com
- 前提: CloudMail 管理面板需关闭注册验证码（Turnstile），或确保注册接口可用
- 邮件获取: 通过 /api/public/emailList 公开接口查询，自动刷新 token

【第三步：并发与稳定性】
6) 注册数量
- 本次要注册的总账号数

7) 并发线程
- 建议先 3-6 稳定后再升到 10

8) 代理（可选）
- 不填=直连
- 示例: http://127.0.0.1:7890
- 代理不稳会影响验证码和注册稳定性

9) 注册后开启 NSFW
- 勾选后成功账号会自动调用接口开启对应设置

【第四步：grok2api 入池（可选）】
10) grok2api 本地自动入池
- 开启后把成功 sso 自动写入本地池
- 本地 token.json 填 grok2api 的 token.json 路径

11) grok2api 池名
- ssoBasic 或 ssoSuper

12) grok2api 远端自动入池
- 开启后调用远端管理接口自动加 token
- 远端 Base 示例: https://xxx/admin/api
- app_key 按远端服务配置填写

【最后：快速自检】
1) 先设置: 注册数量=1，并发线程=1
2) 点开始后看日志是否出现：
- 已创建邮箱: xxx@你的域名
- Cloudflare/CloudMail 本轮邮件数量: ...
- 从邮件中提取到验证码: ...
3) 若第一步就失败：
- cloudflare 模式: 检查 API Base / CF 路径 / 鉴权模式
- cloudmail 模式: 检查 URL / 密码 / defaultDomains / 注册接口是否可用

提示:
- 点“开始注册”会自动保存当前配置到 config.json。
- 如果关闭了启动教程，可随时点主界面的“教程”按钮重新打开。"""

    def show_tutorial(self):
        if self._tutorial_window is not None and self._tutorial_window.winfo_exists():
            self._tutorial_window.lift()
            self._tutorial_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._tutorial_window = win
        win.title("使用教程")
        win.geometry("760x620")
        win.minsize(680, 520)
        win.transient(self.root)

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        txt = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=26)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert("1.0", self._tutorial_text())
        txt.config(state=tk.DISABLED)

        footer = ttk.Frame(frame)
        footer.pack(fill=tk.X, pady=(8, 0))

        dont_show_var = tk.BooleanVar(value=not bool(reg.config.get("show_tutorial_on_start", True)))
        chk = ttk.Checkbutton(
            footer,
            text="以后不再自动显示本教程",
            variable=dont_show_var,
        )
        chk.pack(side=tk.LEFT)

        def on_close():
            reg.config["show_tutorial_on_start"] = not bool(dont_show_var.get())
            reg.save_config()
            try:
                win.destroy()
            except Exception:
                pass

        close_btn = ttk.Button(footer, text="关闭", command=on_close)
        close_btn.pack(side=tk.RIGHT, padx=5)
        win.protocol("WM_DELETE_WINDOW", on_close)

    def should_stop(self):
        return self.stop_requested or not self.is_running

    def start_registration(self):
        if self.is_running:
            self.log("[!] 当前已有任务在运行")
            return

        cfg = reg.config
        cfg["email_provider"] = self.email_provider_var.get().strip() or "hotmail"
        cfg["proxy"] = self.proxy_var.get().strip() or "http://127.0.0.1:7890"
        cfg["cpa_proxy"] = self.cpa_proxy_var.get().strip() or cfg["proxy"]
        cfg["duckmail_api_key"] = self.api_key_var.get().strip()
        cfg["cloudflare_api_base"] = self.cloudflare_api_base_var.get().strip()
        cfg["cloudflare_api_key"] = self.cloudflare_api_key_var.get().strip()
        cfg["cloudflare_auth_mode"] = self.cloudflare_auth_mode_var.get().strip() or "bearer"
        cfg["cloudmail_url"] = self.cloudmail_url_var.get().strip()
        cfg["cloudmail_admin_email"] = self.cloudmail_admin_email_var.get().strip()
        cfg["cloudmail_password"] = self.cloudmail_password_var.get().strip()
        cfg["grok2api_auto_add_local"] = bool(self.grok2api_local_auto_var.get())
        cfg["grok2api_local_token_file"] = self.grok2api_local_file_var.get().strip()
        cfg["grok2api_pool_name"] = self.grok2api_pool_name_var.get().strip() or "ssoBasic"
        cfg["grok2api_auto_add_remote"] = bool(self.grok2api_remote_auto_var.get())
        cfg["grok2api_remote_base"] = self.grok2api_remote_base_var.get().strip()
        cfg["grok2api_remote_app_key"] = self.grok2api_remote_key_var.get().strip()
        cfg["defaultDomains"] = self.default_domains_var.get().strip()
        cfg["hotmail_accounts_file"] = self.hotmail_accounts_file_var.get().strip() or "mail_credentials.txt"
        cfg["hotmail_alias_mode"] = self.hotmail_alias_mode_var.get().strip() or "primary"
        try:
            cfg["hotmail_max_aliases_per_account"] = max(1, int(self.hotmail_max_aliases_var.get()))
        except Exception:
            cfg["hotmail_max_aliases_per_account"] = 1
        if str(cfg.get("hotmail_alias_mode") or "").strip().lower() == "primary":
            cfg["hotmail_max_aliases_per_account"] = 1
        cfg["cpa_export_enabled"] = bool(self.cpa_export_var.get())
        cfg["cpa_auth_dir"] = self.cpa_auth_dir_var.get().strip() or "./cpa_auths"
        try:
            cfg["register_threads"] = max(1, min(10, int(self.thread_var.get())))
        except Exception:
            cfg["register_threads"] = 1
        raw_paths = [x.strip() for x in self.cloudflare_paths_var.get().split(",") if x.strip()]
        if len(raw_paths) >= 4:
            cfg["cloudflare_path_domains"] = raw_paths[0] if raw_paths[0].startswith("/") else ("/" + raw_paths[0])
            cfg["cloudflare_path_accounts"] = raw_paths[1] if raw_paths[1].startswith("/") else ("/" + raw_paths[1])
            cfg["cloudflare_path_token"] = raw_paths[2] if raw_paths[2].startswith("/") else ("/" + raw_paths[2])
            cfg["cloudflare_path_messages"] = raw_paths[3] if raw_paths[3].startswith("/") else ("/" + raw_paths[3])
        normalized = reg._normalize_runtime_config(cfg)
        cfg.clear()
        cfg.update(normalized)
        reg.save_config()
        if cfg["email_provider"] in ("hotmail", "outlook", "outlookmail", "microsoft"):
            mail_file = cfg.get("hotmail_accounts_file") or "mail_credentials.txt"
            mail_path = mail_file if os.path.isabs(mail_file) else os.path.join(os.path.dirname(__file__), mail_file)
            if not os.path.isfile(mail_path):
                self.log(f"[!] Hotmail 模式需要凭证文件: {mail_path}")
                return
        if cfg["email_provider"] == "cloudflare" and not cfg["cloudflare_api_base"]:
            self.log("[!] Cloudflare 模式需要先填写 Cloudflare API Base")
            return
        if cfg["email_provider"] == "cloudmail":
            if not cfg.get("cloudmail_url"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail URL")
                return
            if not cfg.get("cloudmail_admin_email"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail 管理员邮箱")
                return
            if not cfg.get("cloudmail_password"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail 管理员密码")
                return
        try:
            count = int(self.count_var.get())
        except Exception:
            self.log("[!] 注册数量无效")
            return
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.accounts_output_file = os.path.join(os.path.dirname(__file__), f"accounts_{now}.txt")
        self.update_stats()
        self._set_running_ui(True)
        worker_count = max(1, min(cfg.get("register_threads", 1), count))
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count}，并发线程: {worker_count}")
        self.log(
            f"[*] 策略: provider={cfg.get('email_provider')} "
            f"alias={cfg.get('hotmail_alias_mode')}/{cfg.get('hotmail_max_aliases_per_account')} "
            f"proxy={cfg.get('proxy')} cpa_proxy={cfg.get('cpa_proxy')} "
            f"cpa_export={cfg.get('cpa_export_enabled')}"
        )
        self.log(f"[*] 成功账号将实时保存到: {self.accounts_output_file}")
        threading.Thread(
            target=self.run_registration,
            args=(count, worker_count),
            daemon=True,
        ).start()

    def stop_registration(self):
        self.stop_requested = True
        self.log("[!] 用户停止注册")

    def _run_single_registration(self, idx, total, logf):
        email = ""
        dev_token = ""
        code = ""
        mail_ok = False
        max_mail_retry = 5
        try:
            reg.stop_browser()
        except Exception:
            pass
        proxy_account = reg.begin_registration_proxy_session(f"gui_n{idx}")
        if proxy_account:
            logf(f"[*] Resin 粘性会话: {proxy_account}")
        reg.start_browser(log_callback=logf)
        for mail_try in range(1, max_mail_retry + 1):
            logf(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
            reg.open_signup_page(log_callback=logf, cancel_callback=self.should_stop)
            logf("[*] 2. 创建邮箱并提交")
            try:
                email, dev_token = reg.fill_email_and_submit(
                    log_callback=logf, cancel_callback=self.should_stop
                )
            except reg.EmailAlreadyRegisteredError as exist_exc:
                bad = getattr(exist_exc, "email", "") or email
                reg.mark_error(bad, reason="xai_account_already_exists")
                logf(f"[!] 邮箱已在 xAI 注册，已记入 emails_error 并换号: {bad}")
                if mail_try < max_mail_retry:
                    reg.restart_browser(log_callback=logf)
                    reg.sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            except reg.EmailOtpRateLimitedError as rate_exc:
                bad = getattr(rate_exc, "email", "") or email
                reg.mark_error(bad, reason=f"otp_send_rate_limit:{str(rate_exc)[:100]}")
                logf(f"[!] xAI 验证码发送过多/限流，已标记并换号: {bad}")
                if mail_try < max_mail_retry:
                    reg.restart_browser(log_callback=logf)
                    reg.sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            except Exception as submit_exc:
                msg = str(submit_exc)
                is_page_stuck = (
                    "未找到邮箱输入框" in msg
                    or "未进入验证码页" in msg
                    or "仍在邮箱表单" in msg
                    or "反复回到注册方式页" in msg
                    or "表单卡住" in msg
                    or "未找到「使用邮箱注册」" in msg
                )
                if is_page_stuck and mail_try < max_mail_retry:
                    logf(f"[!] 邮箱提交页卡住，重开浏览器重试 ({mail_try}/{max_mail_retry}): {msg}")
                    reg.restart_browser(log_callback=logf)
                    reg.sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            logf(f"[*] 邮箱: {email}")
            logf("[*] 3. 拉取验证码")
            try:
                code = reg.fill_code_and_submit(
                    email, dev_token, log_callback=logf, cancel_callback=self.should_stop
                )
                mail_ok = True
                break
            except reg.EmailAlreadyRegisteredError as exist_exc:
                bad = getattr(exist_exc, "email", "") or email
                reg.mark_error(bad, reason="xai_account_already_exists")
                logf(f"[!] 邮箱已在 xAI 注册，已记入 emails_error 并换号: {bad}")
                if mail_try < max_mail_retry:
                    reg.restart_browser(log_callback=logf)
                    reg.sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            except reg.EmailOtpRateLimitedError as rate_exc:
                bad = getattr(rate_exc, "email", "") or email
                reg.mark_error(bad, reason=f"otp_send_rate_limit:{str(rate_exc)[:100]}")
                logf(f"[!] xAI 验证码发送过多/限流，已标记并换号: {bad}")
                if mail_try < max_mail_retry:
                    reg.restart_browser(log_callback=logf)
                    reg.sleep_with_cancel(1, self.should_stop)
                    continue
                raise
            except Exception as mail_exc:
                msg = str(mail_exc)
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
                    ("未收到验证码" in msg or "在" in msg and "验证码" in msg)
                    and not is_oauth_dead
                )
                is_page_stuck = (
                    "未进入验证码页" in msg
                    or "仍在邮箱表单" in msg
                    or "未找到邮箱输入框" in msg
                    or "反复回到注册方式页" in msg
                    or "表单卡住" in msg
                    or "回到注册方式页" in msg
                    or "未进入资料页" in msg
                    or "验证码无效" in msg
                    or "验证码已填写但未进入" in msg
                )
                if (is_oauth_dead or is_otp_miss or is_page_stuck) and mail_try < max_mail_retry:
                    if is_oauth_dead or is_otp_miss:
                        reason = (
                            f"hotmail_oauth_dead:{msg[:100]}"
                            if is_oauth_dead
                            else f"otp_timeout:{msg[:80]}"
                        )
                        reg.mark_error(email, reason=reason)
                        logf(
                            f"[!] 邮箱读信失败，已标记并换号"
                            f"{'（微软判定账号异常/OAuth 失效）' if is_oauth_dead else ''}：{msg}"
                        )
                    else:
                        logf(f"[!] 页面未进入验证码流程，重开浏览器重试: {msg}")
                    reg.restart_browser(log_callback=logf)
                    reg.sleep_with_cancel(1, self.should_stop)
                    continue
                raise
        if not mail_ok:
            raise Exception("验证码阶段失败，已达到最大重试次数")
        logf(f"[*] 验证码: {code}")
        logf("[*] 4. 填写资料")
        try:
            profile = reg.fill_profile_and_submit(log_callback=logf, cancel_callback=self.should_stop)
            logf(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
            logf("[*] 5. 等待 sso cookie")
            sso = reg.wait_for_sso_cookie(log_callback=logf, cancel_callback=self.should_stop)
        except Exception as flow_exc:
            reg.mark_error(email, reason=str(flow_exc)[:120])
            raise
        password = profile.get("password", "") or ""
        reg.mark_used(email, password)
        with self.stats_lock:
            self.results.append({"email": email, "sso": sso, "profile": profile})
            self.success_count += 1
            line = f"{email}----{password}----{sso}\n"
            try:
                with open(self.accounts_output_file, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as file_exc:
                logf(f"[Debug] 保存账号文件失败: {file_exc}")
        reg.add_token_to_grok2api_pools(sso, email=email, log_callback=logf)
        logf(f"[+] 注册成功: {email}")

    def _worker_loop(self, worker_id, total, task_queue):
        prefix = f"[T{worker_id}]"

        def logf(m):
            self.log(f"{prefix} {m}")

        try:
            logf("[*] 工作线程就绪（每账号独立浏览器 + Resin 粘性会话）")
            while not self.should_stop():
                try:
                    idx = task_queue.get_nowait()
                except queue.Empty:
                    break
                logf(f"--- 开始第 {idx}/{total} 个账号 ---")
                try:
                    self._run_single_registration(idx, total, logf)
                except reg.RegistrationCancelled:
                    logf("[!] 注册被用户停止")
                    break
                except Exception as exc:
                    with self.stats_lock:
                        self.fail_count += 1
                    logf(f"[-] 注册失败: {exc}")
                finally:
                    self.update_stats()
                    try:
                        reg.stop_browser()
                    except Exception:
                        pass
                    if self.should_stop():
                        break
                    reg.sleep_with_cancel(1, self.should_stop)
        except Exception as exc:
            logf(f"[!] 线程异常: {exc}")
        finally:
            try:
                reg.stop_browser()
            except Exception:
                pass

    def run_registration(self, count, worker_count):
        task_queue = queue.Queue()
        for i in range(1, count + 1):
            task_queue.put(i)
        workers = []
        try:
            start_interval = float(reg.config.get("thread_start_interval", 0.8))
        except Exception:
            start_interval = 0.8
        if start_interval < 0:
            start_interval = 0.0
        for wid in range(1, worker_count + 1):
            t = threading.Thread(target=self._worker_loop, args=(wid, count, task_queue), daemon=True)
            workers.append(t)
            t.start()
            if wid < worker_count and start_interval > 0:
                reg.sleep_with_cancel(start_interval, self.should_stop)
        for t in workers:
            t.join()
        self._set_running_ui(False)
        self.log("[*] 任务结束")


def main():
    root = tk.Tk()
    GrokRegisterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
