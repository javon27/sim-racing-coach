"""User settings, persisted as JSON so the future desktop UI's settings page and
the terminal app read/write the same file. Flat dict on purpose — easy for a UI
to bind to. Unknown/malformed files fall back to defaults rather than crashing."""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "units": "metric",   # "metric" (km/h, °C) or "imperial" (mph, °F); distances stay meters
}

CONFIG_PATH = "simcoach.config.json"


def load_config(path: str | None = None) -> dict:
    """Read the config file merged over DEFAULTS. Writes a starter file with the
    defaults on first run so there's something to edit / point the UI at."""
    p = Path(path or CONFIG_PATH)
    cfg = dict(DEFAULTS)
    if p.exists():
        try:
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (json.JSONDecodeError, OSError):
            pass  # malformed/unreadable -> defaults
    else:
        try:
            p.write_text(json.dumps(cfg, indent=2))
        except OSError:
            pass
    return cfg
