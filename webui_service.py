"""WebUI config, inventory, and status helpers."""
from __future__ import annotations

from webui_service_common import *  # noqa: F403
from webui_process import MANAGER, ProcessManager

def mailbox_status(config: dict[str, Any]) -> dict[str, Any]:
    try:
        path = local_path(
            str(config.get("hotmail_accounts_file", "mail_credentials.txt")),
            default_name="mail_credentials.txt",
        )
        return {
            "path": path.relative_to(ROOT).as_posix(),
            "exists": path.exists(),
            "count": count_nonempty_lines(path),
        }
    except ValueError:
        return {"path": "outside project", "exists": False, "count": 0}


def cpa_status(config: dict[str, Any]) -> dict[str, Any]:
    try:
        directory = local_path(
            str(config.get("cpa_auth_dir", "./cpa_auths")),
            default_name="cpa_auths",
        )
    except ValueError:
        return {"path": "outside project", "count": 0}
    count = sum(1 for _ in directory.glob("xai-*.json")) if directory.exists() else 0
    return {"path": directory.relative_to(ROOT).as_posix(), "count": count}


def resolve_preset_values(config: dict[str, Any], preset_id: str = "") -> dict[str, Any]:
    values = dict(config)
    presets = config.get("registration_presets")
    if preset_id and isinstance(presets, dict):
        preset = presets.get(preset_id)
        if isinstance(preset, dict) and isinstance(preset.get("values"), dict):
            values.update(preset["values"])
    return values


def _primary_email(address: str) -> str:
    value = str(address or "").strip().lower()
    if "@" not in value:
        return value
    local, domain = value.rsplit("@", 1)
    return f"{local.split('+', 1)[0]}@{domain}"


def _tracked_mail_addresses() -> set[str]:
    tracked: set[str] = set()
    for name in ("emails_used.txt", "emails_error.txt"):
        path = ROOT / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            value = line.strip()
            if not value or value.startswith(("#", "//")):
                continue
            email = value.split("----", 1)[0].strip().lower()
            if "@" in email:
                tracked.add(email)
    return tracked


def outlook_inventory(
    config: dict[str, Any],
    *,
    alias_enabled: bool,
    alias_limit: int,
) -> dict[str, Any]:
    path = local_path(
        str(config.get("hotmail_accounts_file") or "mail_credentials.txt"),
        default_name="mail_credentials.txt",
    )
    accounts: list[str] = []
    seen: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            value = line.strip()
            if not value or value.startswith(("#", "//")):
                continue
            email = value.split("----", 1)[0].strip().lower()
            primary = _primary_email(email)
            if "@" in primary and primary not in seen:
                seen.add(primary)
                accounts.append(primary)

    tracked = _tracked_mail_addresses()
    used_primary = {_primary_email(address) for address in tracked if "+" not in address.split("@", 1)[0]}
    alias_counts: dict[str, int] = {}
    for address in tracked:
        local = address.split("@", 1)[0]
        if "+" in local:
            primary = _primary_email(address)
            alias_counts[primary] = alias_counts.get(primary, 0) + 1

    items = []
    primary_available = 0
    alias_capacity = 0
    for primary in accounts:
        aliases_used = alias_counts.get(primary, 0)
        primary_is_used = primary in used_primary
        if alias_enabled:
            remaining = max(0, alias_limit - aliases_used)
            if remaining <= 0:
                continue
            alias_capacity += remaining
            items.append(
                {
                    "email": mask_email(primary),
                    "primary_used": primary_is_used,
                    "aliases_used": aliases_used,
                    "remaining": remaining,
                }
            )
        elif not primary_is_used:
            primary_available += 1
            items.append(
                {
                    "email": mask_email(primary),
                    "primary_used": False,
                    "aliases_used": aliases_used,
                    "remaining": 1,
                }
            )
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "mode": "alias" if alias_enabled else "primary",
        "alias_limit": alias_limit,
        "mailboxes": len(accounts),
        "available_mailboxes": len(items),
        "primary_available": primary_available if not alias_enabled else 0,
        "alias_capacity": alias_capacity if alias_enabled else 0,
        "items": items,
    }


def account_summary(path: Path, config: dict[str, Any], limit: int = 12) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total = 0
    if path.exists():
        try:
            with path.open(encoding="utf-8-sig", errors="ignore") as handle:
                parsed: list[tuple[str, int]] = []
                for index, line in enumerate(handle, 1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    total += 1
                    parsed.append((stripped.split("----", 1)[0].strip(), index))
                for email, index in parsed[-max(1, min(limit, 50)) :]:
                    rows.append(
                        {
                            "index": index,
                            "email": mask_email(email),
                            "has_cpa": False,
                        }
                    )
        except OSError:
            pass
    cpa = cpa_status(config)
    cpa_dir = ROOT / cpa["path"] if cpa["path"] != "outside project" else None
    if cpa_dir is not None and cpa_dir.exists():
        cpa_names: set[str] = set()
        cpa_emails: set[str] = set()
        for item in cpa_dir.glob("xai-*.json"):
            cpa_names.add(item.name.lower())
            try:
                payload = json.loads(item.read_text(encoding="utf-8"))
                email = str(payload.get("email") or "").strip().lower()
                if email:
                    cpa_emails.add(email)
            except (OSError, ValueError, TypeError):
                pass
        if path.exists():
            try:
                with path.open(encoding="utf-8-sig", errors="ignore") as handle:
                    emails = [
                        line.strip().split("----", 1)[0].strip()
                        for line in handle
                        if line.strip() and not line.lstrip().startswith("#")
                    ][-len(rows) :]
                for row, email in zip(rows, emails):
                    normalized = email.strip().lower()
                    row["has_cpa"] = (
                        normalized in cpa_emails
                        or f"xai-{normalized}.json" in cpa_names
                    )
            except OSError:
                pass
    return {
        "total": total,
        "path": path.relative_to(ROOT).as_posix(),
        "items": rows,
    }


def public_config() -> dict[str, Any]:
    config = load_runtime_config()
    presets_raw = config.get("registration_presets")
    presets = presets_raw if isinstance(presets_raw, dict) else {}
    if not presets:
        presets = {
            "default": {
                "name": "默认配置",
                "values": {key: config.get(key) for key in EDITABLE_CONFIG if key in config},
            }
        }
    active_id = str(config.get("active_registration_preset") or "").strip()
    if active_id not in presets and presets:
        active_id = next(iter(presets))
    active_values = dict(config)
    active_preset = presets.get(active_id) if active_id else None
    if isinstance(active_preset, dict) and isinstance(active_preset.get("values"), dict):
        active_values.update(active_preset["values"])
    secret_keys = {key for key, kind in EDITABLE_CONFIG.items() if kind == "secret"}
    values = {key: active_values.get(key) for key in EDITABLE_CONFIG if key not in secret_keys}
    for key in secret_keys:
        values[key] = ""
    public_presets = []
    for preset_id, preset in presets.items():
        if not isinstance(preset, dict):
            continue
        preset_values = dict(config)
        if isinstance(preset.get("values"), dict):
            preset_values.update(preset["values"])
        safe_values = {
            key: preset_values.get(key)
            for key in EDITABLE_CONFIG
            if key not in secret_keys
        }
        for key in secret_keys:
            safe_values[key] = ""
        public_presets.append(
            {
                "id": preset_id,
                "name": str(preset.get("name") or preset_id),
                "values": safe_values,
                "secrets": {key: bool(preset_values.get(key)) for key in secret_keys},
                "mailbox": mailbox_status(preset_values),
            }
        )
    return {
        "exists": CONFIG_FILE.exists(),
        "active_preset_id": active_id,
        "presets": public_presets,
        "values": values,
        "secrets": {key: bool(active_values.get(key)) for key in secret_keys},
        "mailbox": mailbox_status(active_values),
        "cpa": cpa_status(active_values),
    }


def normalize_config_updates(payload: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    updates = payload.get("values", payload)
    if not isinstance(updates, dict):
        raise ValueError("values must be an object")
    result: dict[str, Any] = {}
    for key, value in updates.items():
        kind = EDITABLE_CONFIG.get(key)
        if kind is None:
            continue
        if kind == "bool":
            result[key] = bool(value)
        elif kind == "int":
            parsed = int(value)
            low, high = INT_RANGES.get(key, (-1000000, 1000000))
            if not low <= parsed <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            result[key] = parsed
        elif kind == "choice":
            parsed = str(value or "").strip().lower()
            if parsed not in CHOICES[key]:
                raise ValueError(f"Unsupported value for {key}")
            result[key] = parsed
        elif kind == "local_path":
            default = "mail_credentials.txt" if key == "hotmail_accounts_file" else "cpa_auths"
            result[key] = relative_local_path(str(value or default), default_name=default)
        elif kind == "secret":
            parsed = str(value or "").strip()
            if parsed:
                result[key] = parsed
        else:
            parsed = str(value or "").strip()
            if len(parsed) > 4096:
                raise ValueError(f"{key} is too long")
            result[key] = parsed

    clear_secrets = payload.get("clear_secrets", [])
    if isinstance(clear_secrets, list):
        for key in clear_secrets:
            if EDITABLE_CONFIG.get(str(key)) == "secret":
                result[str(key)] = ""

    merged = dict(current)
    merged.update(result)
    merged["api_reverse_tools"] = ""
    return merged


def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_runtime_config()
    delete_id = str(payload.get("delete_preset_id") or "").strip()
    if delete_id:
        presets = current.get("registration_presets")
        if not isinstance(presets, dict) or delete_id not in presets:
            raise ValueError("Preset does not exist")
        if len(presets) <= 1:
            raise ValueError("At least one registration preset is required")
        presets.pop(delete_id)
        active_id = str(current.get("active_registration_preset") or "")
        if active_id == delete_id:
            active_id = next(reversed(presets))
            current["active_registration_preset"] = active_id
            active = presets[active_id]
            if isinstance(active, dict) and isinstance(active.get("values"), dict):
                current.update(active["values"])
        temp = CONFIG_FILE.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, CONFIG_FILE)
        return public_config()

    preset_id = str(payload.get("preset_id") or "").strip()
    if preset_id:
        if not PRESET_ID_PATTERN.fullmatch(preset_id):
            raise ValueError("Invalid preset id")
        preset_name = str(payload.get("preset_name") or preset_id).strip()[:80] or preset_id
        presets = current.get("registration_presets")
        if not isinstance(presets, dict):
            presets = {}
            current["registration_presets"] = presets
        existing = presets.get(preset_id)
        existing_values = existing.get("values") if isinstance(existing, dict) else {}
        effective = dict(current)
        if isinstance(existing_values, dict):
            effective.update(existing_values)
        normalized = normalize_config_updates(payload, effective)
        preset_values = {
            key: normalized.get(key)
            for key in EDITABLE_CONFIG
            if key in normalized
        }
        presets[preset_id] = {"name": preset_name, "values": preset_values}
        current["active_registration_preset"] = preset_id
        current.update(preset_values)
        current["api_reverse_tools"] = ""
        temp = CONFIG_FILE.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, CONFIG_FILE)
        return public_config()

    merged = normalize_config_updates(payload, current)
    temp = CONFIG_FILE.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, CONFIG_FILE)
    return public_config()


def dashboard_status() -> dict[str, Any]:
    config = load_runtime_config()
    status = MANAGER.status()
    status["accounts"] = account_summary(MANAGER.accounts_file, config, limit=10)
    status["mailbox"] = mailbox_status(config)
    status["cpa"] = cpa_status(config)
    status["config_exists"] = CONFIG_FILE.exists()
    return status


