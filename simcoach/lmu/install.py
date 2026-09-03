"""Locate LMU's UserData directory: Steam library (any drive), Epic/standalone
fallback paths, or a config/env override. Everything degrades to None --
this module must NEVER raise."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .. import config as _config

_STEAM_DEFAULT_BASE = Path(r"C:\Program Files (x86)\Steam")

_FALLBACK_USERDATA_DIRS = [
    Path(r"C:\Program Files\Epic Games\LeMansUltimate\UserData"),
    Path(r"C:\Program Files (x86)\Epic Games\LeMansUltimate\UserData"),
    Path(r"C:\Le Mans Ultimate\UserData"),
]


def _steam_base() -> Path | None:
    """Steam install root: HKCU\\Software\\Valve\\Steam\\SteamPath, else the
    common default install location. Monkeypatch this function in tests."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            path, _ = winreg.QueryValueEx(key, "SteamPath")
            if path:
                return Path(path)
    except Exception:
        pass
    return _STEAM_DEFAULT_BASE


def _steam_libraries(steam_base: Path) -> list[Path]:
    """Library folders from libraryfolders.vdf, plus the Steam base itself."""
    libs = [steam_base]
    vdf = steam_base / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return libs
    for m in re.finditer(r'"path"\s+"([^"]+)"', text):
        libs.append(Path(m.group(1).replace("\\\\", "\\")))
    return libs


def _has_results(userdata: Path) -> bool:
    return (userdata / "Log" / "Results").is_dir()


def _steam_userdata_dir() -> Path | None:
    try:
        base = _steam_base()
        if not base:
            return None
        for lib in _steam_libraries(base):
            candidate = lib / "steamapps" / "common" / "Le Mans Ultimate" / "UserData"
            if _has_results(candidate):
                return candidate
    except Exception:
        pass
    return None


def _override_dir() -> Path | None:
    env = (os.environ.get("SIMCOACH_LMU_DIR") or "").strip()
    if env:
        return Path(env)
    try:
        val = _config.load_config().get("lmu_dir")
    except Exception:
        val = None
    return Path(val) if val else None


def lmu_userdata_dir() -> Path | None:
    override = _override_dir()
    if override and _has_results(override):
        return override
    found = _steam_userdata_dir()
    if found:
        return found
    for candidate in _FALLBACK_USERDATA_DIRS:
        if _has_results(candidate):
            return candidate
    return None


def results_dir() -> Path | None:
    d = lmu_userdata_dir()
    return d / "Log" / "Results" if d else None


def player_dir() -> Path | None:
    d = lmu_userdata_dir()
    return d / "player" if d else None
