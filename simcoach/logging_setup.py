"""Rotating file logging for the sidecar server. Replaces the Tauri shell's
crude stdout-to-file redirect with real Python logging: a rotating file
handler under app-data/logs plus a console handler for dev visibility."""
from __future__ import annotations

import logging
import logging.handlers

from . import config
from .paths import logs_dir

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _resolve_level(name: str | None) -> int:
    level = getattr(logging, str(name).upper(), None)
    return level if isinstance(level, int) else logging.INFO


def apply_log_level(level: str) -> None:
    """Re-set the root logger, uvicorn loggers, and our own handlers to
    `level` at runtime (no restart needed). Invalid values fall back to INFO."""
    resolved = _resolve_level(level)
    root = logging.getLogger()
    root.setLevel(resolved)
    for name in _UVICORN_LOGGERS:
        logging.getLogger(name).setLevel(resolved)
    for h in root.handlers:
        if getattr(h, "_simcoach_handler", False):
            h.setLevel(resolved)


def configure_logging() -> None:
    root = logging.getLogger()

    # Idempotent: drop any handler we previously attached before re-adding.
    for h in list(root.handlers):
        if getattr(h, "_simcoach_handler", False):
            root.removeHandler(h)

    formatter = logging.Formatter(_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir() / "server.log", maxBytes=5_000_000, backupCount=3
    )
    file_handler.setFormatter(formatter)
    file_handler._simcoach_handler = True

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler._simcoach_handler = True

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    level = _resolve_level(config.load_config().get("log_level"))
    root.setLevel(level)
    for name in _UVICORN_LOGGERS:
        logging.getLogger(name).setLevel(level)
