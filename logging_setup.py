"""Structured logging with rotation and secret redaction."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_TOKEN_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|sso|bearer)\b(\s*[:=]\s*)([^\s,;]+)"
)
_PASSWORD_RE = re.compile(r"(?i)\b(password|passwd|pwd)\b(\s*[:=]\s*)([^\s,;]+)")
_INITIALIZED = False


def redact_log_text(value: str) -> str:
    text = str(value or "")
    text = _TOKEN_RE.sub(r"\1\2***", text)
    text = _PASSWORD_RE.sub(r"\1\2***", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return redact_log_text(original)


def init(
    *,
    name: str = "grok_sulfide",
    level: int = logging.INFO,
    log_dir: str | Path | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Initialize root project logger once; return it."""
    global _INITIALIZED
    logger = logging.getLogger(name)
    if _INITIALIZED:
        return logger

    logger.setLevel(level)
    logger.propagate = False
    fmt = RedactingFormatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    directory = Path(log_dir or Path.cwd() / "logs")
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        directory / "grok_sulfide.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    _INITIALIZED = True
    return logger


def get_logger(name: str = "grok_sulfide") -> logging.Logger:
    if not _INITIALIZED:
        return init(name=name)
    return logging.getLogger(name)
