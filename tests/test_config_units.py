"""Config file + imperial/metric units + the richer per-corner inputs."""
import json

from simcoach.analysis import analyze_lap
from simcoach.config import DEFAULTS, load_config
from simcoach.corners import CornerMap
from simcoach.format import format_lap_facts


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
    user = format_lap_facts(_analysis(synthetic_lap), units="metric")
    assert "km/h" in user and "mph" not in user
    assert "°C" in user


def test_imperial_prompt_converts_speed_and_temp(synthetic_lap):
    a = _analysis(synthetic_lap)
    user = format_lap_facts(a, units="imperial")
    assert "mph" in user and "km/h" not in user
    assert "°F" in user and "°C" not in user
    # Both corners' minima must be CONVERTED, not passed through: the hairpin's
    # 50 km/h reads 31 mph and the sweeper's 94 km/h reads 58 mph. Asserting the raw
    # km/h numbers are absent is the half that catches a missing conversion -- and
    # checking both corners is what catches a conversion applied to only one of them
    # (the two used to share a single min speed, so one number covered both).
    assert "min 31" in user and "min 58" in user
    assert "min 50" not in user and "min 94" not in user
    # The agentic system prompt telling the model the units is covered by
    # test_coach_tools.py's ask_agentic units test.


def test_corner_line_has_richer_inputs(synthetic_lap):
    user = format_lap_facts(_analysis(synthetic_lap))
    t1 = [l for l in user.splitlines() if l.startswith("T1 ")][0]
    assert "peak" in t1 and "g" in t1        # peak lateral g surfaced
    assert "trail" in t1
    assert "→min" in t1                       # entry→min→exit speeds surfaced
