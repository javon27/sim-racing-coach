import json
from simcoach import config


def test_defaults_present():
    for k in ("units", "input_device", "model", "corner_naming", "driver"):
        assert k in config.DEFAULTS


def test_load_merges_file_over_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"units": "imperial"}))
    cfg = config.load_config(str(p))
    assert cfg["units"] == "imperial"
    assert cfg["model"] == "sonnet"  # default fills the rest


def test_env_override_beats_file(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"input_device": "wheel"}))
    monkeypatch.setenv("SIMCOACH_INPUT_DEVICE", "controller")
    cfg = config.load_config(str(p))
    assert cfg["input_device"] == "controller"


def test_save_round_trips(tmp_path):
    p = tmp_path / "c.json"
    config.save_config({"units": "imperial", "model": "opus"}, str(p))
    assert json.loads(p.read_text())["model"] == "opus"
    assert config.load_config(str(p))["units"] == "imperial"


def test_malformed_file_falls_back(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{ not json")
    assert config.load_config(str(p))["units"] == "metric"
