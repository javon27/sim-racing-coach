import json
import os

import pytest

from simcoach import config


def test_defaults_present():
    for k in ("units", "input_device", "model", "corner_naming", "driver", "show_cost",
              "chat_token_soft_limit"):
        assert k in config.DEFAULTS


def test_show_cost_defaults_to_true(tmp_path):
    """The driver sees what the coach costs unless they turn it off."""
    assert config.DEFAULTS["show_cost"] is True
    assert config.load_config(str(tmp_path / "fresh.json"))["show_cost"] is True


def test_chat_token_soft_limit_default():
    """The soft nudge threshold is configurable, not a magic number in the UI."""
    assert config.DEFAULTS["chat_token_soft_limit"] == 150_000


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


def test_voice_defaults_and_partial_nested_merge(tmp_path):
    from simcoach import config
    p = str(tmp_path / "config.json")
    assert config.load_config(p)["voice"]["enabled"] is False
    assert config.load_config(p)["voice"]["mic_device_id"] is None
    config.save_config({"voice": {"enabled": True}}, p)
    v = config.load_config(p)["voice"]
    assert v["enabled"] is True and v["ptt_key"] == "F9" and v["vad_silence_ms"] == 1000
    config.save_config({"voice": {"tts_rate": 1.2}}, p)
    v = config.load_config(p)["voice"]
    assert v["enabled"] is True and v["tts_rate"] == 1.2      # both partial saves kept
    config.save_config({"voice": {"mic_device_id": "abc123"}}, p)
    v = config.load_config(p)["voice"]
    assert v["mic_device_id"] == "abc123" and v["enabled"] is True  # round-trips, keeps prior partials


def test_ptt_focus_gate_defaults_true_and_round_trips(tmp_path):
    """#112 escape hatch: default ON, and a partial save doesn't disturb it."""
    from simcoach import config
    p = str(tmp_path / "config.json")
    assert config.load_config(p)["voice"]["ptt_focus_gate"] is True
    config.save_config({"voice": {"ptt_focus_gate": False}}, p)
    assert config.load_config(p)["voice"]["ptt_focus_gate"] is False
    config.save_config({"voice": {"tts_rate": 1.2}}, p)
    v = config.load_config(p)["voice"]
    assert v["ptt_focus_gate"] is False and v["tts_rate"] == 1.2  # kept across an unrelated partial save


def test_crewchief_wait_defaults_true(tmp_path):
    """#163: the coach waits for CrewChief by default (a no-op when it isn't running)."""
    from simcoach import config
    assert config.load_config(str(tmp_path / "config.json"))["voice"]["crewchief_wait"] is True


def test_driver_merge_still_works_after_generalising(tmp_path):
    from simcoach import config
    p = str(tmp_path / "config.json")
    config.save_config({"driver": {"braking_style": "late"}}, p)
    config.save_config({"driver": {"risk_tolerance": "high"}}, p)
    d = config.load_config(p)["driver"]
    assert d["braking_style"] == "late" and d["risk_tolerance"] == "high" and d["balance_preference"] is None


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


def test_voice_defaults_include_engine_and_kokoro_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    cfg = config.load_config()
    assert cfg["voice"]["tts_engine"] == "windows" and cfg["voice"]["kokoro_voice"] == "af_heart"


def test_bind_names_default_none_and_round_trip(tmp_path):
    """#104: the device name captured with a wheel/gamepad bind, so Settings can
    show it even when the device is asleep at render time."""
    p = str(tmp_path / "config.json")
    v = config.load_config(p)["voice"]
    assert v["ptt_wheel_name"] is None and v["ptt_gamepad_name"] is None
    config.save_config({"voice": {"ptt_wheel_name": "Fanatec DD1", "ptt_gamepad_name": "Xbox controller 2"}}, p)
    v = config.load_config(p)["voice"]
    assert v["ptt_wheel_name"] == "Fanatec DD1" and v["ptt_gamepad_name"] == "Xbox controller 2"


def test_save_config_writes_atomically(tmp_path, monkeypatch):
    # #11: a bare write_text leaves invalid JSON on a crash mid-write, which
    # load_config's malformed-JSON fallback silently swallows back to defaults.
    # save_config must write to a tmp file and os.replace it into place, like
    # _write_track_json does for track maps.
    p = tmp_path / "config.json"
    calls = []
    real_replace = os.replace

    def recording_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    config.save_config({"units": "imperial"}, str(p))
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == str(p)
    assert src != str(p) and src.startswith(str(p.parent))  # a distinct tmp path
    assert config.load_config(str(p))["units"] == "imperial"
