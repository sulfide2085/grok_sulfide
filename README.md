# grok_sulfide

独立运行的 Grok 注册机。注册、Hotmail OAuth2/IMAP 验证码、SSO 输出以及
CPA/xAI OIDC 凭据生成代码都在本目录中，不会加载 `grok_bytao`、
`grok_reg-protocol_cpa` 或其他兄弟项目。

## 包含内容

- `register_cli.py`：命令行批量注册入口
- `grok_register_gui.py`：桌面 GUI 入口
- `grok_register_ttk.py`：注册流程核心
- `webui_server.py`、`webui/`：本地 WebUI 与静态资源
- `hotmail_provider.py`：内置 Hotmail/Outlook OAuth2 + IMAP 提供器
- `cpa_xai/`、`cpa_export.py`：内置 CPA/xAI OIDC 生成与导出
- `protocol_engine/grok-build-auth/`、`protocol_register.py`：内置协议注册、SSO 与 CPA OAuth
- `turnstilePatch/`：浏览器扩展

项目仍需要 Python 包、Chrome/Chromium、邮箱服务和 xAI 网络服务；“独立”是指
不依赖工作区里的其他本地项目或源码目录。

## 安装

```powershell
cd grok_sulfide
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.json config.json
Copy-Item mail_credentials.example.txt mail_credentials.txt
```

`mail_credentials.txt` 每行格式：

```text
email----password----ClientID----refresh_token
```

从单列 CSV 导入并排除旧项目使用记录：

```powershell
python scripts/import_outlook_csv_pool.py `
  --csv "path\to\outlook-pool.csv" `
  --history-dir "path\to\old-registrar" `
  --history-dir "." `
  --output mail_credentials_imported_free.txt
```

导入是一次性快照；运行时仍只读取 sulfide 本目录，不依赖旧项目。

默认使用主邮箱一次，不启用 `+alias`：

```json
{
  "email_provider": "hotmail",
  "hotmail_alias_mode": "primary",
  "hotmail_max_aliases_per_account": 1
}
```

## 运行

```powershell
# WebUI，默认自动打开 http://127.0.0.1:8765/
.\start_webui.ps1

# 新注册 1 个账号
python register_cli.py --extra 1 --threads 1 --mint-workers 1

# grokcli-2api 协议注册（不走 Chromium 注册页面）
python register_cli.py --extra 1 --threads 1 --registration-method protocol

# 使用保存的注册预设
python register_cli.py --extra 1 --preset preset1
python register_cli.py --extra 1 --preset preset2

# GUI
python grok_register_gui.py
```

也可以直接启动 WebUI：

```powershell
python webui_server.py
python webui_server.py --port 8800 --no-open
```

若端口已占用，WebUI 会在随后 20 个端口中自动选择一个。默认仅监听
`127.0.0.1`，页面展示的账号邮箱经过脱敏，不返回密码或 SSO。

WebUI 的每个注册预设独立保存注册方式、邮箱提供器、代理、邮箱凭据、
别名策略、CPA/OIDC 和入池设置。运行页只需选择预设；配置页可以编辑、
新建或删除预设。默认包含：

- `preset1`：sulfide 浏览器注册 + 原始 Outlook 主邮箱配置
- `preset2`：grokcli-2api 协议注册 + Outlook 随机别名池

Outlook 的别名开关和别名数量上限位于运行页，属于本次任务覆盖项。
关闭别名时列表只显示尚未注册的主邮箱；开启别名时会按每个主邮箱
已使用的别名数量计算剩余额度，并显示当前可生成的别名账号总数。

主要输出：

- `accounts_cli.txt`：`email----password----sso`
- `cpa_auths/xai-*.json`：启用 CPA 导出时生成的 xAI OIDC 凭据
- `emails_used.txt` / `emails_error.txt`：邮箱使用记录

## 配置提示

- `proxy`：注册浏览器和普通 HTTP 请求使用的代理
- `email_proxy`：邮箱 OAuth 请求代理；默认 `direct`
- `cpa_proxy`：CPA/OIDC 流程代理；为空时回退到 `proxy`
- `cpa_management_upload_enabled`：默认关闭；需要上传远端池时再开启并配置密钥
- `registration_method`：`browser` 或 `protocol`
- `protocol_email_provider`：协议模式收件后端；默认 `outlook`，也可选 `moemail`、`duckmail`、`yyds`、`cloudflare`、`cloudmail`
- `protocol_moemail_*` / `protocol_yescaptcha_key`：协议注册使用的邮箱和 Turnstile 配置
- `cpa_ssh_upload_enabled`：独立的 SSH 自动入池开关，不替换管理 API 的地址或密钥
- `api_reverse_tools`：应保持为空，默认使用本项目内的 `cpa_xai/`

免密 SSH 自动入池示例：

```json
{
  "cpa_ssh_upload_enabled": true,
  "cpa_ssh_host": "example-ssh-host",
  "cpa_ssh_auth_dir": "/path/to/cliproxyapi/auths",
  "cpa_ssh_chmod": "600"
}
```

两种注册方式都会规范化写入 `accounts_cli.txt` 的
`email----password----sso`，并生成 `cpa_auths/xai-<email>.json`。

`protocol_yescaptcha_key` 是可选项。未配置时协议模式先直接创建账号；
若当前 xAI 会话无法无验证码提取 SSO/CPA，账号仍会保存到
`accounts_cli.txt`，并在 WebUI 中显示 CPA 待生成，之后可执行补提取。

完整运行说明见 [docs/REGISTER_PLAYBOOK.md](docs/REGISTER_PLAYBOOK.md)。
