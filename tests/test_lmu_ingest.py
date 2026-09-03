import json

from simcoach.lmu.ingest import ingest_all
from simcoach.store import LapStore

_XML = """<?xml version="1.0"?>
<rFactorXML>
<RaceResults>
<PlayerFile>Settings</PlayerFile>
<DateTime>1750472422</DateTime>
<TrackCourse>Test Track</TrackCourse>
<Race>
<DateTime>1750473029</DateTime>
<Driver>
<Name>{name}</Name>
<VehName>Car</VehName>
<CarClass>GT3</CarClass>
<Position>1</Position>
<ClassPosition>1</ClassPosition>
<Lap num="1" s1="30.0" s2="30.0" s3="30.0">90.0</Lap>
<BestLapTime>90.0</BestLapTime>
<FinishStatus>Finished Normally</FinishStatus>
</Driver>
</Race>
</RaceResults>
</rFactorXML>
"""


def _make_userdata(tmp_path, matched_name="Jayson G", other_name="Someone Else"):
    userdata = tmp_path / "UserData"
    results_dir = userdata / "Log" / "Results"
    results_dir.mkdir(parents=True)
    player_dir = userdata / "player"
    player_dir.mkdir(parents=True)
    (player_dir / "Settings.JSON").write_text(json.dumps({"Player Name": matched_name}))
    (results_dir / "race1.xml").write_text(_XML.format(name=matched_name))
    (results_dir / "race2.xml").write_text(_XML.format(name=other_name))
    return userdata


def _store(tmp_path):
    return LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))


def test_ingest_all_imports_matched_and_counts_unmatched(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)

    summary = ingest_all(store, userdata_dir=userdata)

    assert summary == {"scanned": 2, "imported": 1, "skipped": 0, "unmatched": 1, "errors": 0}
    sessions = store.recent_race_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "race1"
    assert sessions[0]["track"] == "Test Track"


def test_ingest_all_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    userdata = _make_userdata(tmp_path)
    store = _store(tmp_path)
    ingest_all(store, userdata_dir=userdata)

    summary2 = ingest_all(store, userdata_dir=userdata)

    assert summary2["imported"] == 0
    assert summary2["skipped"] == 1
    assert len(store.recent_race_sessions()) == 1


def test_ingest_all_explicit_player_names_skips_profile_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    userdata = _make_userdata(tmp_path, matched_name="Old Alias")
    store = _store(tmp_path)

    summary = ingest_all(store, userdata_dir=userdata, player_names="old alias")

    assert summary["imported"] == 1


def test_ingest_all_no_results_dir_returns_zero_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    store = _store(tmp_path)

    summary = ingest_all(store, userdata_dir=tmp_path / "nowhere")

    assert summary == {"scanned": 0, "imported": 0, "skipped": 0, "unmatched": 0, "errors": 0}
