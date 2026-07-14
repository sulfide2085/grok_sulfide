# Refactor progress (develop)

Last updated: 2026-07-15  
Branch: `develop` (pushed to `origin/develop`)  
Baseline tests: `uv run pytest -q` (44+), `uv run ruff check . --select F811,E722` clean on host tree.

## Done

| Plan stage | Status | Notes |
|------------|--------|-------|
| 0 Safety net | Done | uv.lock, ruff, pytest, characterization tests, purge_credentials |
| 1 Dedup | Done | F811 NSFW dups removed; gui → 701-line shell |
| 2 Modularize | Mostly done | `providers/`, `browser.py`, `proxy_bridge.py`, `store.py`, `config_runtime.py` |
| 3 Observability | Mostly done | logging_setup, failure_classify, preflight healthcheck, silent except→logger.debug |
| 4 Data/concurrency | Partial | SQLite WAL ledger + dual-write; TabPool still thread-local (acceptable) |
| 5 Secrets/WebUI | Mostly done | WebUI token+Host checks; purge script; state.db gitignored |
| 6 protocol_engine | Partial | BOUNDARY.md + adapter-only entry; OIDC dedupe still open |

## Metrics

| Item | Before | Now |
|------|--------|-----|
| `grok_register_gui.py` | ~4600 | ~701 |
| `grok_register_ttk.py` | ~4600 | ~3246 |
| Host bare `except: pass` | many | 0 (excl. vendored engine) |
| F811 redefines | 10 | 0 |
| Unit tests | 0 | 44+ |

## Remaining (next)

1. Optional: delete leftover dead helpers still unused after provider extraction audits  
2. Deeper `BrowserSession` API through fill_* call chain (currently TabPool wrappers)  
3. Merge engine deps into root `uv.lock` / optional extra  
4. Further OIDC path dedupe between `cpa_xai` and engine export  
5. Open PR `develop` → `main` when ready for review  

## Commits on develop (since main)

See `git log origin/main..origin/develop --oneline`.
