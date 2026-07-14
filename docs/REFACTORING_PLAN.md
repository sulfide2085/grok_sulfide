# grok_sulfide 改造方案（Refactoring Plan）

> **For agentic workers:** 建议用 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐任务执行。所有步骤用复选框（`- [ ]`）跟踪，按阶段顺序推进，每个 Task 结束即提交。

**目标（Goal）：** 在不改变现有注册/建号功能的前提下，把两份 4600 行的重复巨石模块，改造成有分层、有测试、有可观测性、数据与密钥可控的可维护工程。

**架构方向（Architecture）：** 以 `grok_register_ttk.py` 为唯一核心真相源；把邮箱提供器、浏览器、代理桥、存储、配置拆成独立模块（包）；`grok_register_gui.py` 收敛为只做 UI 的薄壳；文本账本迁移到 SQLite；先建"特征化测试（characterization test）安全网"再动结构。

**技术栈（Tech Stack）：** Python 3.12/3.13、DrissionPage、curl_cffi、requests、标准库 `sqlite3`/`http.server`/`logging`；工具链 `uv` + `ruff` + `pytest`。

完整任务清单见主仓库 `docs/REFACTORING_PLAN.md`（本 worktree 副本用于执行跟踪）。
