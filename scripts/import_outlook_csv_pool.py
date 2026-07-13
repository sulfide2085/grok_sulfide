"""Import an Outlook CSV pool while excluding historical usage ledgers."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def primary_address(address: str) -> str:
    value = address.strip().lower()
    if "@" not in value:
        return value
    local, domain = value.rsplit("@", 1)
    return f"{local.split('+', 1)[0]}@{domain}"


def read_ledger_emails(path: Path) -> set[str]:
    result: set[str] = set()
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        value = line.strip()
        if not value or value.startswith(("#", "//")):
            continue
        email = value.split("----", 1)[0].strip()
        if "@" in email:
            result.add(primary_address(email))
    return result


def read_history(directory: Path) -> tuple[set[str], set[str]]:
    used = read_ledger_emails(directory / "emails_used.txt")
    used.update(read_ledger_emails(directory / "accounts_cli.txt"))
    errors = read_ledger_emails(directory / "emails_error.txt")
    return used, errors


def parse_pool(path: Path) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            raw = str(next(iter(raw_row.values()), "") or "").strip()
            parts = raw.split("----", 3)
            if len(parts) != 4:
                continue
            email, password, client_id, refresh_token = (part.strip() for part in parts)
            refresh_token = refresh_token.split("\t", 1)[0].strip()
            key = email.lower()
            if (
                key in seen
                or "@" not in email
                or not client_id
                or not refresh_token
            ):
                continue
            seen.add(key)
            rows.append((email, password, client_id, refresh_token))
    return rows


def atomic_write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def merge_used_ledger(path: Path, addresses: set[str]) -> None:
    existing_lines = []
    existing_emails: set[str] = set()
    if path.is_file():
        existing_lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        existing_emails = read_ledger_emails(path)
    additions = [
        f"{address}----historical_main_import"
        for address in sorted(addresses)
        if address not in existing_emails
    ]
    lines = [line.rstrip("\r\n") + "\n" for line in existing_lines if line.strip()]
    lines.extend(line + "\n" for line in additions)
    atomic_write_lines(path, lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--history-dir", action="append", default=[], type=Path)
    parser.add_argument(
        "--recover-main-only",
        action="store_true",
        help="Include mailboxes whose primary address was used but no +alias was used.",
    )
    parser.add_argument(
        "--seed-used-ledger",
        type=Path,
        default=None,
        help="Seed recovered primary addresses into a local used ledger.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "mail_credentials_imported_free.txt",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "outlook_pool_import_report.json",
    )
    args = parser.parse_args()

    source_rows = parse_pool(args.csv.resolve())
    used: set[str] = set()
    errors: set[str] = set()
    history_dirs = [directory.resolve() for directory in args.history_dir]
    for directory in history_dirs:
        directory_used, directory_errors = read_history(directory)
        used.update(directory_used)
        errors.update(directory_errors)

    usage_by_primary: dict[str, set[str]] = {}
    for address in used:
        usage_by_primary.setdefault(primary_address(address), set()).add(address)
    error_primaries = {primary_address(address) for address in errors}

    free_rows: list[tuple[str, str, str, str]] = []
    main_only_rows: list[tuple[str, str, str, str]] = []
    alias_used_rows = 0
    error_rows = 0
    for row in source_rows:
        primary = primary_address(row[0])
        if primary in error_primaries:
            error_rows += 1
            continue
        addresses = usage_by_primary.get(primary, set())
        if not addresses:
            free_rows.append(row)
        elif addresses == {primary}:
            main_only_rows.append(row)
        else:
            alias_used_rows += 1

    selected_rows = list(main_only_rows) if args.recover_main_only else []
    selected_rows.extend(free_rows)
    output = args.output.resolve()
    atomic_write_lines(
        output,
        ["----".join(row) + "\n" for row in selected_rows],
    )
    if args.seed_used_ledger and args.recover_main_only:
        merge_used_ledger(
            args.seed_used_ledger.resolve(),
            {primary_address(row[0]) for row in main_only_rows},
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(args.csv.resolve()),
        "history_dirs": [str(path) for path in history_dirs],
        "output": str(output),
        "valid_unique_rows": len(source_rows),
        "free_primary_rows": len(free_rows),
        "recovered_main_only_rows": len(main_only_rows) if args.recover_main_only else 0,
        "excluded_alias_used_rows": alias_used_rows,
        "excluded_error_rows": error_rows,
        "matched_consumed": len(source_rows) - len(free_rows),
        "free_rows": len(free_rows),
        "selected_rows": len(selected_rows),
    }
    args.report.resolve().write_text(
        json.dumps(report, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "valid_unique_rows",
                    "free_primary_rows",
                    "recovered_main_only_rows",
                    "excluded_alias_used_rows",
                    "excluded_error_rows",
                    "selected_rows",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
