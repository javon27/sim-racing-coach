import os
from pathlib import Path
from simcoach import paths


def test_override_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "custom"))
    d = paths.app_data_dir()
    assert d == tmp_path / "custom"
    assert d.is_dir()  # created


def test_derived_paths_live_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    assert paths.config_path() == tmp_path / "simcoach.config.json"
    assert paths.db_path() == tmp_path / "simcoach.sqlite"
    assert paths.telemetry_dir() == tmp_path / "telemetry"
    assert paths.telemetry_dir().is_dir()


def test_default_root_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SIMCOACH_DATA_DIR", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    d = paths.app_data_dir()
    assert d == tmp_path / ".simcoach"


def test_windows_appdata_used_when_set(tmp_path, monkeypatch):
    monkeypatch.delenv("SIMCOACH_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = paths.app_data_dir()
    assert d == tmp_path / "SimCoach"
    assert d.is_dir()
