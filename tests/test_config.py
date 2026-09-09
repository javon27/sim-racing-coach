import json
import pytest
from simcoach import config


def test_defaults_present():
    for k in ("units", "input_device", "model", "corner_naming", "driver", "show_cost"):
        assert k in config.DEFAULTS


def test_show_cost_defaults_to_true(tmp_path):
    """The driver sees what the coach costs unless they turn it off."""
    assert config.DEFAULTS["show_cost"] is True
    assert config.load_config(str(tmp_path / "fresh.json"))["show_cost"] is True


def test_show_cost_round_trips_false(tmp_path):
    p = tmp_path / "c.json"
    config.save_config({"show_cost": False}, str(p))
    assert config.load_config(str(p))["show_cost"] is False


def test_load_merges_file_over_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"units": "imperial"}))
    cfg = config.load_config(str(p))
    assert cfg["units"] == "imperial"
    assert cfg["model"] == "sonnet"  # default fills the rest


@pytest.mark.parametrize("key,env", list(config.ENV_OVERRIDES.items()))
def test_env_override_beats_file(tmp_path, monkeypatch, key, env):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({key: "from-file"}))
    monkeypatch.setenv(env, "from-env")
    cfg = config.load_config(str(p))
    assert cfg[key] == "from-env"


def test_driver_partial_merge(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"driver": {"braking_style": "late"}}))
    cfg = config.load_config(str(p))
    assert cfg["driver"]["braking_style"] == "late"
    assert cfg["driver"]["balance_preference"] is None
    assert cfg["driver"]["risk_tolerance"] is None


def test_load_allow_lists_unknown_keys(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"api_key": "sk-x", "units": "imperial"}))
    cfg = config.load_config(str(p))
    assert cfg["units"] == "imperial"
    assert "api_key" not in cfg


def test_save_drops_env_override(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    monkeypatch.setenv("SIMCOACH_MODEL", "opus")
    cfg = config.load_config(str(p))
    config.save_config(cfg, str(p))
    monkeypatch.delenv("SIMCOACH_MODEL")
    assert config.load_config(str(p))["model"] != "opus"


def test_save_round_trips(tmp_path):
    p = tmp_path / "c.json"
    config.save_config({"units": "imperial", "model": "opus"}, str(p))
    assert json.loads(p.read_text())["model"] == "opus"
    assert config.load_config(str(p))["units"] == "imperial"


def test_malformed_file_falls_back(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{ not json")
    assert config.load_config(str(p))["units"] == "metric"
