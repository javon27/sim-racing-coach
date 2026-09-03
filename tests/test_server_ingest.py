import json

from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel

_XML = """<?xml version="1.0"?>
<rFactorXML>
<RaceResults>
<PlayerFile>Settings</PlayerFile>
<DateTime>1750472422</DateTime>
<TrackCourse>Test Track</TrackCourse>
<Race>
<DateTime>1750473029</DateTime>
<Driver>
<Name>Jayson G</Name>
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


def _client(tmp_path, monkeypatch, mem_keyring):
    lmu_dir = tmp_path / "UserData"
    results_dir = lmu_dir / "Log" / "Results"
    results_dir.mkdir(parents=True)
    player_dir = lmu_dir / "player"
    player_dir.mkdir(parents=True)
    (player_dir / "Settings.JSON").write_text(json.dumps({"Player Name": "Jayson G"}))
    (results_dir / "race1.xml").write_text(_XML)

    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("SIMCOACH_LMU_DIR", str(lmu_dir))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(), "simcoach/knowledge"))
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_post_ingest_returns_summary(tmp_path, monkeypatch, mem_keyring):
    c, store = _client(tmp_path, monkeypatch, mem_keyring)

    r = c.post("/ingest")

    assert r.status_code == 200
    assert r.json() == {"scanned": 1, "imported": 1, "skipped": 0, "unmatched": 0, "errors": 0}
    assert len(store.recent_race_sessions()) == 1
