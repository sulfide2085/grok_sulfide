# grok_sulfide

Grok 账号注册工具：支持浏览器注册与协议注册，内置 Outlook 邮箱验证码读取，并可导出 SSO / CPA（xAI OIDC）凭据。

## 功能

- **浏览器注册**：Chromium 自动化完成注册页流程
- **协议注册**：HTTP 协议建号（无需注册页浏览器）
- **邮箱**：Hotmail/Outlook OAuth2 + IMAP；协议模式还可接 MoeMail 等
- **凭据输出**：`accounts_cli.txt`（SSO）与 `cpa_auths/xai-*.json`（OIDC）
- **入口**：WebUI / CLI / 桌面 GUI
- **可选入池**：管理 API 或免密 SSH 上传 CPA 凭据

## 环境要求

- Python 3.12 / 3.13
- Windows（推荐；脚本以 PowerShell 为主）
- 浏览器注册需要 Chrome/Chromium
- 可用的邮箱池与网络代理（按你的环境配置）

## 安装

```powershell
cd grok_sulfide
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.json config.json
Copy-Item mail_credentials.example.txt mail_credentials.txt
```

编辑 `config.json`（代理、注册方式等），并按下面格式填写邮箱凭据。

### 邮箱凭据格式

`mail_credentials.txt` 每行一条：

```text
email----password----ClientID----refresh_token
```

从 CSV 导入邮箱池（可选）：

```powershell
python scripts/import_outlook_csv_pool.py `
  --csv "path\to\outlook-pool.csv" `
  --history-dir "path\to\old-registrar" `
  --history-dir "." `
  --output mail_credentials_imported_free.txt
```

默认建议先用主邮箱、不启用 `+alias`：

```json
{
  "email_provider": "hotmail",
  "hotmail_alias_mode": "primary",
  "hotmail_max_aliases_per_account": 1
}
```

## 快速开始

```powershell
# WebUI（默认 http://127.0.0.1:8765/）
.\start_webui.ps1

# 浏览器注册 1 个
python register_cli.py --extra 1 --threads 1 --mint-workers 1

# 协议注册 1 个
python register_cli.py --extra 1 --threads 1 --registration-method protocol

# 使用预设
python register_cli.py --extra 1 --preset preset1
python register_cli.py --extra 1 --preset preset2

# 桌面 GUI
python grok_register_gui.py
```

也可直接：

```powershell
python webui_server.py
python webui_server.py --port 8800 --no-open
```

端口占用时会在随后 20 个端口中自动选取。默认只监听 `127.0.0.1`；页面上的邮箱会脱敏，不返回密码或 SSO。

## 注册方式

| 方式 | 说明 |
|------|------|
| `browser` | 打开 Chromium 走注册页；CPA mint 可走独立 worker 队列 |
| `protocol` | 协议建号 + 邮箱验证码；Turnstile 可选 YesCaptcha；CPA 在协议链内完成 |

两种方式都会尽量写出：

- `accounts_cli.txt`：`email----password----sso`
- `cpa_auths/xai-<email>.json`：OIDC 凭据（启用 CPA 导出时）

协议模式未配置 `protocol_yescaptcha_key` 时会先尝试直接建号；若拿不到 SSO/CPA，账号仍会写入 `accounts_cli.txt`，可稍后补 mint。

## WebUI 预设

每个预设可保存注册方式、邮箱提供器、代理、凭据路径、别名策略、CPA 与入池设置。

- 运行页：选预设、设数量/线程/别名覆盖项后启动
- 配置页：编辑、新建、删除预设

示例：

- `preset1`：浏览器注册 + Outlook 主邮箱
- `preset2`：协议注册 + Outlook 随机别名

别名开关与数量上限在运行页，作用于当次任务。关闭别名时只显示未用主邮箱；开启时按每个主邮箱剩余别名额度汇总。

## 主要输出

| 文件 | 内容 |
|------|------|
| `accounts_cli.txt` | `email----password----sso` |
| `cpa_auths/xai-*.json` | xAI OIDC / CPA 凭据 |
| `emails_used.txt` / `emails_error.txt` | 邮箱使用与失败记录 |

补生成 CPA：

```powershell
python scripts\backfill_cpa_xai_from_accounts.py --limit 1 --probe --timeout 300
```

## 配置要点

| 键 | 含义 |
|----|------|
| `proxy` | 注册浏览器 / 普通 HTTP 代理 |
| `email_proxy` | 邮箱 OAuth 代理，默认 `direct` |
| `cpa_proxy` | CPA/OIDC 代理；空则回退 `proxy` |
| `registration_method` | `browser` 或 `protocol` |
| `protocol_email_provider` | 协议收件：`outlook` / `moemail` / `duckmail` / `yyds` / `cloudflare` / `cloudmail` |
| `protocol_yescaptcha_key` | 协议 Turnstile（可选） |
| `cpa_export_enabled` | 是否写 CPA JSON |
| `cpa_management_upload_enabled` | 管理 API 入池（默认关） |
| `cpa_ssh_upload_enabled` | SSH 入池（默认关） |

SSH 入池示例：

```json
{
  "cpa_ssh_upload_enabled": true,
  "cpa_ssh_host": "example-ssh-host",
  "cpa_ssh_auth_dir": "/path/to/cliproxyapi/auths",
  "cpa_ssh_chmod": "600"
}
```

完整字段见 `config.example.json`，运行细节见 [docs/REGISTER_PLAYBOOK.md](docs/REGISTER_PLAYBOOK.md)。

## 目录结构（节选）

```text
register_cli.py          CLI 批量注册
grok_register_gui.py     桌面 GUI
grok_register_ttk.py     浏览器注册核心
webui_server.py / webui/ 本地 WebUI
hotmail_provider.py      Outlook OAuth2 + IMAP
protocol_register.py     协议注册入口
protocol_engine/         协议引擎
cpa_xai/ / cpa_export.py CPA/OIDC 生成与导出
scripts/                 导入邮箱池、补 mint 等
```

## 安全说明

以下文件含敏感信息，**不要提交或公开分享**：

- `config.json`
- `mail_credentials.txt` 及各类邮箱池
- `accounts_*.txt`
- `cpa_auths/*.json`
- `cookies/`、`screenshots/`、日志

仓库 `.gitignore` 已忽略上述运行时文件。分享目录前可参考 `SHARE_BEFORE_SEND.txt`。

## 免责声明

仅供学习与研究。请遵守 xAI / Microsoft 等服务的服务条款与当地法律；滥用账号批量注册可能违反平台规则并导致封禁。
