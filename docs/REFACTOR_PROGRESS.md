# Refactor progress (develop)

Last updated: 2026-07-15  
Branch: `develop` (pushed to `origin/develop`)  
Baseline tests: `uv run pytest -q` (48+), `uv run ruff check . --select F811,E722` clean on host tree.

## Done

| Plan stage | Status | Notes |
|------------|--------|-------|
| 0 Safety net | Done | uv.lock, ruff, pytest, characterization tests, purge_credentials |
| 1 Dedup | Done | F811 NSFW dups removed; gui → 701-line shell |
| 2 Modularize | Done | `providers/`, `browser.py`(+BrowserSession), `proxy_bridge.py`, `store.py`, `config_runtime.py` |
| 3 Observability | Done | logging_setup, failure_classify, preflight healthcheck, silent except→logger.debug |
| 4 Data/concurrency | Done | SQLite WAL + dual-write + `record_account`; thread-local BrowserSession contract documented |
| 5 Secrets/WebUI | Done | WebUI token+Host checks; purge script; state.db gitignored |
| 6 protocol_engine | Mostly done | BOUNDARY.md + adapter-only entry; `cpa_xai.facade` documents dual mint paths |

## Metrics

| Item | Before | Now |
|------|--------|-----|
| `grok_register_gui.py` | ~4600 | ~701 |
| `grok_register_ttk.py` | ~4600 | ~3249 |
| Host bare `except: pass` | many | 0 (excl. vendored engine) |
| F811 redefines | 10 | 0 |
| Unit tests | 0 | 48+ |

## Remaining (optional)

1. Thread `BrowserSession` objects deeper into fill_* signatures (currently available via `current_session()` / TabPool)  
2. Optional further OIDC code sharing between protocol engine and browser mint  
3. Merge PR `develop` → `main` after review  

## Commits on develop (since main)

See `git log origin/main..origin/develop --oneline`.
