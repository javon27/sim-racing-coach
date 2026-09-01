"""Config file + imperial/metric units + the richer per-corner inputs."""
import json

from simcoach.analysis import analyze_lap
from simcoach.coach import Coach
from simcoach.config import DEFAULTS, load_config
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel


def _analysis(lap):
    return analyze_lap(lap, CornerMap.load("simcoach/tracks/test_track.json"))


def test_config_defaults_and_starter_file(tmp_path):
    p = tmp_path / "cfg.json"
    cfg = load_config(str(p))
    assert cfg["units"] == "metric"
    assert p.exists()                                   # starter file written
    assert json.loads(p.read_text())["units"] == "metric"


def test_config_reads_file_and_survives_garbage(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"units": "imperial"}')
    assert load_config(str(p))["units"] == "imperial"
    p.write_text("not json {{{")
    assert load_config(str(p)) == DEFAULTS               # malformed -> defaults


def test_metric_prompt_uses_kmh(synthetic_lap):
    c = Coach(FakeModel(), "simcoach/knowledge")
    _, user = c.build_prompt(_analysis(synthetic_lap), "test_car", "q", units="metric")
    assert "km/h" in user and "mph" not in user
    assert "°C" in user


def test_imperial_prompt_converts_speed_and_temp(synthetic_lap):
    c = Coach(FakeModel(), "simcoach/knowledge")
    a = _analysis(synthetic_lap)
    _, user = c.build_prompt(a, "test_car", "q", units="imperial")
    assert "mph" in user and "km/h" not in user
    assert "°F" in user and "°C" not in user
    # 30 km/h min speed -> ~18-19 mph, not the raw 30
    assert "min 18" in user or "min 19" in user
    # system prompt tells the model the units
    system, _ = c.build_prompt(a, "test_car", "q", units="imperial")
    assert "mph" in system and "°F" in system


def test_corner_line_has_richer_inputs(synthetic_lap):
    c = Coach(FakeModel(), "simcoach/knowledge")
    _, user = c.build_prompt(_analysis(synthetic_lap), "test_car", "q")
    t1 = [l for l in user.splitlines() if l.startswith("T1 ")][0]
    assert "peak" in t1 and "g" in t1        # peak lateral g surfaced
    assert "trail" in t1
    assert "→min" in t1                       # entry→min→exit speeds surfaced
