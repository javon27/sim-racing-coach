"""User settings, persisted as flat JSON so the desktop UI's settings page and
the terminal app read/write the same file. Secrets are NOT stored here (see
simcoach.secrets). Env vars override the file for the documented keys only;
nothing requires them. Malformed/unknown files fall back to defaults."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import paths

DEFAULTS = {
    "units": "metric",            # "metric" (km/h, °C) or "imperial" (mph, °F)
    "input_device": "wheel",      # "wheel" or "controller"
    "model": "sonnet",            # "opus" | "sonnet" | "haiku" -> make_model
    "corner_naming": "names",     # "names" or "numbers"
    "show_cost": True,            # show the estimated $ on the chat meter (tokens always show)
    # Past this many billed tokens in one chat, the chat screen softly suggests
    # starting a New chat. A SOFT nudge only -- nothing is summarized or truncated.
    # 0 disables it.
    "chat_token_soft_limit": 150_000,
    "log_level": "INFO",          # stdlib logging level name; see logging_setup
    "lmu_dir": None,              # override for lmu.install.lmu_userdata_dir()
    "lmu_build": None,            # last-seen LMU Steam build id (detected, persisted)
    "lmu_player_names": [],       # alias set for result-log ingest matching (see lmu.results)
    "driver": {                   # DriverProfile fields; None = unset
        "braking_style": None,
        "balance_preference": None,
        "risk_tolerance": None,
    },
    "voice": {                    # see docs/superpowers/specs/2026-09-15-voice-loop-design.md
        "enabled": False,
        "ptt_key": "F9",          # keyboard PTT bind (KeyboardEvent.code / global-shortcut name)
        "ptt_wheel": None,        # "<device_id>:<button>" from the shell's capture flow (slice 4)
        "ptt_gamepad": None,
        "tts_voice": None,        # speechSynthesis voice name; None = browser default
        "tts_rate": 1.0,
        "vad_threshold": 0.02,    # RMS, 0-1
        "vad_silence_ms": 1000,
        "vad_max_s": 20,
        "conversation_idle_s": 30,
    },
}

ENV_OVERRIDES = {
    "input_device": "SIMCOACH_INPUT_DEVICE",
    "units": "SIMCOACH_UNITS",
    "model": "SIMCOACH_MODEL",
    "log_level": "SIMCOACH_LOG_LEVEL",
}


def _path(path: str | None) -> Path:
    return Path(path) if path else paths.config_path()


def load_config(path: str | None = None) -> dict:
    """Read the config file merged over DEFAULTS, then apply env overrides.
    Writes a starter file with the defaults on first run so there's something
    to edit / point the UI at."""
    p = _path(path)
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy (nested driver dict)
    if p.exists():
        try:
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                for key, default in DEFAULTS.items():
                    if isinstance(default, dict):
                        sub = loaded.get(key)
                        loaded = {**loaded, key: {**cfg[key], **sub}} if isinstance(sub, dict) \
                            else {k: v for k, v in loaded.items() if k != key}
                cfg.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            pass  # malformed/unreadable -> defaults
    else:
        try:
            p.write_text(json.dumps(cfg, indent=2))
        except OSError:
            pass
    for key, env in ENV_OVERRIDES.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    return cfg


def save_config(cfg: dict, path: str | None = None) -> None:
    safe = {k: v for k, v in cfg.items() if k in DEFAULTS}
    for key, env in ENV_OVERRIDES.items():
        if os.environ.get(env):
            safe.pop(key, None)  # never persist a dev env override
    p = _path(path)
    on_disk = {}
    if p.exists():
        try:
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                on_disk = loaded
        except (json.JSONDecodeError, OSError):
            on_disk = {}
    for key, default in DEFAULTS.items():
        if isinstance(default, dict) and isinstance(on_disk.get(key), dict) and isinstance(safe.get(key), dict):
            safe = {**safe, key: {**on_disk[key], **safe[key]}}
    on_disk.update(safe)
    p.write_text(json.dumps(on_disk, indent=2))
