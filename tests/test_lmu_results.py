import json
from pathlib import Path

from simcoach.lmu import results

_FIXTURE = Path(__file__).parent / "fixtures" / "lmu_results" / "sample_race.xml"


_SAMPLE_XML = """<?xml version="1.0"?>
<rFactorXML>
  <RaceResults>
    <PlayerFile>Settings</PlayerFile>
  </RaceResults>
</rFactorXML>
"""


def _write_profile(player_dir, data):
    player_dir.mkdir(parents=True, exist_ok=True)
    (player_dir / "Settings.JSON").write_text(json.dumps(data))


def test_player_name_resolves_from_player_file(tmp_path):
    root = results.ET.fromstring(_SAMPLE_XML)
    player_dir = tmp_path / "player"
    _write_profile(player_dir, {"Player Name": "Jayson G"})

    assert results.player_name_for(root, player_dir) == "Jayson G"


def test_config_override_wins(tmp_path, monkeypatch):
    root = results.ET.fromstring(_SAMPLE_XML)
    player_dir = tmp_path / "player"
    _write_profile(player_dir, {"Player Name": "Jayson G"})
    monkeypatch.setattr(
        results._config, "load_config", lambda: {"lmu_player_name": "Override Name"}
    )

    assert results.player_name_for(root, player_dir) == "Override Name"


def test_none_when_profile_missing(tmp_path):
    root = results.ET.fromstring(_SAMPLE_XML)
    player_dir = tmp_path / "player"  # never created

    assert results.player_name_for(root, player_dir) is None


def test_none_when_player_name_field_missing(tmp_path):
    root = results.ET.fromstring(_SAMPLE_XML)
    player_dir = tmp_path / "player"
    _write_profile(player_dir, {"Something Else": "x"})

    assert results.player_name_for(root, player_dir) is None


def test_none_when_player_file_element_missing(tmp_path):
    root = results.ET.fromstring("<rFactorXML><RaceResults/></rFactorXML>")
    player_dir = tmp_path / "player"
    _write_profile(player_dir, {"Player Name": "Jayson G"})

    assert results.player_name_for(root, player_dir) is None


def test_parse_result_extracts_player_session():
    session = results.parse_result(_FIXTURE, "Jayson G")

    assert session is not None
    assert session.sim == "lmu"
    assert session.track == "Sebring International Raceway"
    assert session.car == "Team WRT #41:LM"
    assert session.car_class == "LMP2"
    assert session.session_type == "race"
    assert session.position == 17
    assert session.class_position == 8
    # The GRID slot the race started from -- deliberately different from the finish
    # (9 -> 17, 4 -> 8) so reading the wrong field can't pass. Live capture never
    # samples it; without it the coach reports a race backwards (issue #67).
    assert session.grid_position == 9
    assert session.class_grid_position == 4
    assert session.best_lap == 120.9103
    assert session.finish_status == "Finished Normally"

    assert [lap.lap_no for lap in session.laps] == [1, 2, 3]
    assert session.laps[0].lap_time == 122.2503
    assert session.laps[0].sectors == [49.0212, 31.4488, 41.7803]
    assert session.laps[0].valid is True
    # lap 3 has no valid time ("--.----" sentinel) -> not parsed as a real lap
    assert session.laps[2].lap_time is None
    assert session.laps[2].valid is False

    assert any("Jayson G" in text for text in session.incidents)
    assert any("Warning issued" in text for text in session.incidents)
    assert len(session.penalties) == 1
    assert "Drive Thru" in session.penalties[0]["text"]


def test_parse_result_returns_none_when_player_absent():
    assert results.parse_result(_FIXTURE, "Nobody Here") is None


def test_parse_result_returns_none_on_malformed_xml(tmp_path):
    bad = tmp_path / "broken.xml"
    bad.write_text("<rFactorXML><RaceResults>")  # unclosed tags

    assert results.parse_result(bad, "Jayson G") is None
