# grok_sulfide 运行手册

## 独立性约束

运行时只允许从 `grok_sulfide` 本目录导入源码。以下能力均已内置：

- Grok 浏览器注册流程
- Hotmail/Outlook OAuth2 刷新与 IMAP 验证码读取
- CPA/xAI device-auth、浏览器授权、探测和凭据写入
- CLI 与 GUI

不要把 `api_reverse_tools` 指向其他项目；留空即可使用本目录 `cpa_xai/`。

## 首次配置

1. 复制 `config.example.json` 为 `config.json`。
2. 复制 `mail_credentials.example.txt` 为 `mail_credentials.txt`。
3. 填入 Hotmail 凭据，每行格式为：

```text
email----password----ClientID----refresh_token
```

4. 按本机环境设置 `proxy` 与 `cpa_proxy`。

推荐先保持：

```json
{
  "email_provider": "hotmail",
  "hotmail_alias_mode": "primary",
  "hotmail_max_aliases_per_account": 1,
  "register_threads": 1,
  "cpa_mint_workers": 1,
  "cpa_headless": false,
  "cpa_force_standalone": true,
  "cpa_management_upload_enabled": false
}
```

## 注册

```powershell
python register_cli.py --extra 1 --threads 1 --mint-workers 1
```

参数说明：

| 参数 | 含义 |
| --- | --- |
| `--extra N` | 在现有账号基础上再注册 N 个 |
| `--count N` | 目标账号总数，包含已有账号 |
| `--threads N` | 注册并发数 |
| `--mint-workers N` | CPA/OIDC 生成并发数 |
| `--accounts-file PATH` | 账号输出文件 |

## 补生成 CPA/OIDC

```powershell
python scripts\backfill_cpa_xai_from_accounts.py --limit 1 --probe --timeout 300
```

确认单个账号成功后，再将 `--limit` 调大。默认输出到 `cpa_auths/`。

## 文件安全

以下文件包含敏感信息，不应打包分享：

- `config.json`
- `mail_credentials.txt`
- `accounts_cli.txt`
- `emails_used.txt`、`emails_error.txt`
- `cpa_auths/*.json`
- `cookies/`、`screenshots/`、日志文件

仓库自带的 `.gitignore` 已忽略这些运行时文件。
