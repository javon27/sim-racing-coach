import json
from pathlib import Path

import pytest

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
    # Two drivers, both flagged isPlayer -> another human was on track, which is what
    # live capture's `--online` suffix means. Needed to match a result to a live session
    # without handing an offline stint an online race's outcome.
    assert session.online is True
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


def test_parse_result_reads_offline_from_a_lone_isplayer(tmp_path):
    """An OFFLINE result flags only the human; the AI field does not get isPlayer. Built
    from the real shape of 2026_09_10_00_39_07-16R1 (18 drivers, 1 isPlayer, Setting
    'Race Weekend'). `Setting` is NOT the signal -- 5 real logs say 'Multiplayer' for one
    human alone on a server, which live capture reads as offline, as this does."""
    xml = tmp_path / "offline.xml"
    xml.write_text(
        "<rFactorXML><RaceResults><DateTime>1789017807</DateTime>"
        "<TrackCourse>Silverstone National Circuit</TrackCourse><Setting>Race Weekend</Setting>"
        "<Race><Stream/>"
        "<Driver><Name>Jayson G</Name><VehName>Team WRT 2026 #32:WEC</VehName>"
        "<isPlayer>1</isPlayer><GridPos>9</GridPos><Position>18</Position>"
        "<Lap num=\"1\" s1=\"26.1\">70.4068</Lap></Driver>"
        "<Driver><Name>AI Opponent</Name><VehName>Other #7:WEC</VehName>"
        "<Position>1</Position></Driver>"
        "</Race></RaceResults></rFactorXML>")

    session = results.parse_result(xml, "Jayson G")
    assert session is not None
    assert session.online is False
    assert session.grid_position == 9 and session.position == 18


# The tags real logs use -- "Practice1" (719), "Qualify" (340), "Race" (347) -- and the
# numbered/unnumbered other forms the tag grammar also accepts.
@pytest.mark.parametrize("tag, m_session", [
    ("Practice1", 1), ("Qualify", 5), ("Qualify1", 5), ("Warmup", 9), ("Race", 10),
    ("Race1", 10)])
def test_result_log_phase_matches_live_capture_phase(tmp_path, tag, m_session):
    """Live capture and result-log import must name a session phase identically. The
    live/result dedupe matches phase exactly, and "qualify" vs live's "qualifying" inserted
    every qualifying result lap as a duplicate of its live twin (#88) -- 3 on one Fuji
    session, 11 already in the driver's store. Pinned against smm.session_phase itself, so
    a rename on EITHER side fails here."""
    from simcoach.lmu.smm import session_phase
    xml = tmp_path / "s.xml"
    xml.write_text(
        "<rFactorXML><RaceResults><DateTime>1789017807</DateTime>"
        "<TrackCourse>Fuji Speedway</TrackCourse>"
        f"<{tag}><Stream/>"
        "<Driver><Name>Jayson G</Name><VehName>Car #1</VehName><isPlayer>1</isPlayer>"
        "<Lap num=\"1\">104.5</Lap></Driver>"
        f"</{tag}></RaceResults></rFactorXML>")
    session = results.parse_result(xml, "Jayson G")
    assert session is not None
    assert session.session_type == session_phase(m_session)


@pytest.mark.parametrize("tag", ["Qualify", "Qualify1"])
def test_a_qualifying_log_parses_exactly_like_the_same_race_log(tmp_path, tag):
    """Every other test parses a <Race> or <Practice*> log, so a change that broke ONLY
    qualifying -- the tag grammar, the element lookup, anything reading that session --
    passed the whole suite. The same log under a <Qualify> element must yield the same
    session field for field: laps, sectors, positions, incidents, penalties, mode.
    Only the phase differs."""
    import dataclasses
    text = _FIXTURE.read_text(encoding="utf-8")
    assert text.count("<Race>") == text.count("</Race>") == 1   # the premise
    xml = tmp_path / "q.xml"
    xml.write_text(text.replace("<Race>", f"<{tag}>").replace("</Race>", f"</{tag}>"),
                   encoding="utf-8")

    race = results.parse_result(_FIXTURE, "Jayson G")
    qualifying = results.parse_result(xml, "Jayson G")

    assert race is not None and race.laps and race.incidents   # a non-trivial session
    assert qualifying == dataclasses.replace(race, session_type="qualifying")


def test_parse_result_returns_none_when_player_absent():
    assert results.parse_result(_FIXTURE, "Nobody Here") is None


def test_parse_result_returns_none_on_malformed_xml(tmp_path):
    bad = tmp_path / "broken.xml"
    bad.write_text("<rFactorXML><RaceResults>")  # unclosed tags

    assert results.parse_result(bad, "Jayson G") is None
