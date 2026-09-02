"""Resolve the OS app-data locations for config, the SQLite DB, and telemetry
files. Windows-first (LMU is Windows-only); a home-dir fallback keeps tests and
non-Windows CI working. SIMCOACH_DATA_DIR overrides the root for developers."""
from __future__ import annotations

import os
from pathlib import Path


def app_data_dir() -> Path:
    override = (os.environ.get("SIMCOACH_DATA_DIR") or "").strip()
    if override:
        root = Path(override)
    else:
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) / "SimCoach" if appdata else Path.home() / ".simcoach"
    if root.exists() and not root.is_dir():
        raise RuntimeError(f"SimCoach data path exists but is not a directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    return app_data_dir() / "simcoach.config.json"


def db_path() -> Path:
    return app_data_dir() / "simcoach.sqlite"


def telemetry_dir() -> Path:
    d = app_data_dir() / "telemetry"
    d.mkdir(parents=True, exist_ok=True)
    return d
