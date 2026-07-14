"""按保留天数清理明文凭据 dump（sso_output/oauth_output/screenshots）。
用法: python scripts/purge_credentials.py --days 3 [--dry-run]
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET_DIRS = [
    ROOT / "protocol_engine" / "grok-build-auth" / "sso_output",
    ROOT / "protocol_engine" / "grok-build-auth" / "oauth_output",
    ROOT / "screenshots",
    ROOT / "cookies",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cutoff = time.time() - args.days * 86400
    removed = 0
    for d in TARGET_DIRS:
        if not d.is_dir():
            continue
        for f in d.glob("*"):
            if f.is_file() and f.stat().st_mtime < cutoff:
                print(("[dry] " if args.dry_run else "") + f"purge {f.name}")
                if not args.dry_run:
                    f.unlink()
                removed += 1
    print(f"total: {removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
