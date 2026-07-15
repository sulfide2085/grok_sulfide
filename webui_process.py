"""WebUI registration process manager (spawns register_cli)."""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from webui_service_common import (
    ROOT,
    CLI_FILE,
    load_runtime_config,
    local_path,
    redact_log_text,
    utc_iso,
)

logger = logging.getLogger("grok_sulfide.webui")

class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._logs: deque[dict[str, Any]] = deque(maxlen=5000)
        self._next_log_id = 1
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._exit_code: int | None = None
        self._command: list[str] = []
        self._accounts_file = ROOT / "accounts_cli.txt"

    def _append_log(self, text: str, kind: str = "output") -> None:
        clean = redact_log_text(text.rstrip("\r\n"))
        if not clean:
            return
        if kind == "output" and clean.startswith("[CPA]"):
            if "[FAIL]" in clean:
                kind = "error"
            elif "[OK]" in clean:
                kind = "success"
            elif "[START]" in clean or "[WARN]" in clean:
                kind = "system"
        with self._lock:
            item = {
                "id": self._next_log_id,
                "time": utc_iso(),
                "kind": kind,
                "text": clean,
            }
            self._next_log_id += 1
            self._logs.append(item)

    def _refresh_locked(self) -> None:
        process = self._process
        if process is None:
            return
        code = process.poll()
        if code is not None and self._exit_code is None:
            self._exit_code = code
            self._ended_at = time.time()

    def start(self, options: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A registration task is already running")

            mode = str(options.get("mode", "extra")).strip().lower()
            if mode not in {"extra", "count"}:
                raise ValueError("mode must be extra or count")
            amount = int(options.get("amount", 1))
            if mode == "extra" and not 1 <= amount <= 10000:
                raise ValueError("extra amount must be between 1 and 10000")
            if mode == "count" and not 0 <= amount <= 100000:
                raise ValueError("target count must be between 0 and 100000")

            threads = max(1, min(int(options.get("threads", 1)), 10))
            alias_enabled = bool(options.get("alias_enabled", False))
            alias_limit = max(1, min(int(options.get("alias_limit", 1)), 1000))
            preset_id = str(options.get("preset_id") or "").strip()
            runtime_config = load_runtime_config()
            preset_values: dict[str, Any] = {}
            if preset_id:
                presets = runtime_config.get("registration_presets") or {}
                preset = presets.get(preset_id) if isinstance(presets, dict) else None
                if preset_id == "default" and not presets:
                    preset_values = runtime_config
                elif not isinstance(preset, dict):
                    raise ValueError("Selected registration preset does not exist")
                else:
                    preset_values = preset.get("values") if isinstance(preset.get("values"), dict) else {}
            registration_method = str(
                preset_values.get("registration_method")
                or options.get("registration_method", "browser")
            ).strip().lower()
            if registration_method not in {"browser", "protocol"}:
                raise ValueError("registration_method must be browser or protocol")
            mint_workers = max(-1, min(int(options.get("mint_workers", -1)), 10))
            recycle_every = max(1, min(int(options.get("browser_recycle_every", 25)), 1000))
            accounts_path = local_path(
                str(options.get("accounts_file", "accounts_cli.txt")),
                default_name="accounts_cli.txt",
            )
            self._accounts_file = accounts_path

            command = [
                sys.executable,
                "-u",
                str(CLI_FILE),
                f"--{mode}",
                str(amount),
                "--threads",
                str(threads),
                "--alias-mode",
                "random" if alias_enabled else "primary",
                "--alias-limit",
                str(alias_limit),
                "--registration-method",
                registration_method,
                "--mint-workers",
                str(mint_workers),
                "--browser-recycle-every",
                str(recycle_every),
                "--accounts-file",
                str(accounts_path),
            ]
            if preset_id:
                command.extend(["--preset", preset_id])
            if not bool(options.get("fast", True)):
                command.append("--no-fast")
            if bool(options.get("no_browser_reuse", False)):
                command.append("--no-browser-reuse")
            if bool(options.get("cookie_snapshot", False)):
                command.append("--cookie-snapshot")
            if bool(options.get("inline_mint", False)):
                command.append("--inline-mint")

            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            creationflags = 0
            start_new_session = False
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                start_new_session = True

            self._append_log("Starting registration task", "system")
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
            self._process = process
            self._started_at = time.time()
            self._ended_at = None
            self._exit_code = None
            self._command = command
            self._reader_thread = threading.Thread(
                target=self._read_output,
                args=(process,),
                daemon=True,
                name="webui-cli-output",
            )
            self._reader_thread.start()
            return self.status()

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        try:
            stream = process.stdout
            if stream is not None:
                for line in stream:
                    self._append_log(line)
        except Exception as exc:
            self._append_log(f"Output reader failed: {exc}", "error")
        finally:
            code = process.wait()
            with self._lock:
                if self._process is process:
                    self._exit_code = code
                    self._ended_at = time.time()
            kind = "success" if code == 0 else "error"
            self._append_log(f"Registration task exited with code {code}", kind)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            process = self._process
            if process is None or process.poll() is not None:
                return self.status()
            self._append_log("Stopping registration task", "system")

        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=5)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        if process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

        with self._lock:
            self._refresh_locked()
            return self.status()

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()

    def logs_after(self, after: int) -> dict[str, Any]:
        with self._lock:
            items = [item for item in self._logs if int(item["id"]) > after]
            cursor = self._logs[-1]["id"] if self._logs else after
            return {"items": items, "cursor": cursor}

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            process = self._process
            running = process is not None and process.poll() is None
            return {
                "running": running,
                "pid": process.pid if running and process is not None else None,
                "started_at": utc_iso(self._started_at) if self._started_at else None,
                "ended_at": utc_iso(self._ended_at) if self._ended_at else None,
                "exit_code": None if running else self._exit_code,
                "command": list(self._command),
                "accounts_file": self._accounts_file.relative_to(ROOT).as_posix(),
            }

    @property
    def accounts_file(self) -> Path:
        with self._lock:
            return self._accounts_file


MANAGER = ProcessManager()




MANAGER = ProcessManager()
