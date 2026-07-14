"""Import text ledgers into state.db (idempotent).

Usage:
  python scripts/migrate_ledger_to_sqlite.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402


def main() -> int:
    stats = store.migrate_text_ledgers_into_sqlite()
    print(
        f"migrated used={stats['used']} error={stats['error']} accounts={stats['accounts']} "
        f"-> {store._DB_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
