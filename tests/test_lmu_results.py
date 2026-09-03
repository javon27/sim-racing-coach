import json

from simcoach.lmu import results


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
