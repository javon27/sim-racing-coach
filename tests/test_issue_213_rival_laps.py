"""#213 (rig 2026-09-27, Long Beach): "What is his fastest time?" about the car ahead got
"no tool reads a rival's fastest lap". The live scoring frame carries every car's best
and last lap (lmu.live), and the result log every driver's <BestLapTime>."""
import dataclasses
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext, race_result_text
from simcoach.lmu.live import read_field
from simcoach.lmu.results import parse_result
from simcoach.lmu.rf2structs import VehicleScoringInfoV01, rF2Scoring
from simcoach.model.fake import FakeModel
from simcoach.model_types import RaceSample
from simcoach.race import LapFacts, build_snapshot, class_field_line, rival_laps_line
from simcoach.server.app import create_app
from simcoach.store import SCHEMA_VERSION, LapStore

XML = Path(__file__).parent / "fixtures" / "lmu_results" / "sample_race.xml"
HEAD = "Lap times now (live, this session): "


def _frame(cars, total_laps=3):
    """A race frame, overall order as listed: (driver, class, best, last). "You" is the
    player."""
    s = rF2Scoring()
    s.mScoringInfo.mSession = 12
    s.mScoringInfo.mMaxLaps = 24
    s.mScoringInfo.mNumVehicles = len(cars)
    for i, (name, cls, best, last) in enumerate(cars):
        v = VehicleScoringInfoV01()
        v.mID, v.mPlace, v.mTotalLaps, v.mIsPlayer = i + 1, i + 1, total_laps, name == "You"
        v.mDriverName, v.mVehicleName = name.encode(), f"{name} Entry".encode()
        v.mVehicleClass, v.mTimeBehindNext = cls.encode(), 1.5
        v.mBestLapTime, v.mLastLapTime = best, last
        s.mVehicles[i] = v
    return s


# Multiclass: the car one place up overall is a Hypercar, the class car ahead is A.
FIELD = [("A", "GT3", 81.618, 82.004), ("H", "Hypercar", 70.0, 71.0),
         ("You", "GT3", 80.874, 81.2), ("C", "GT3", 82.3, 83.1)]


def test_live_best_and_last_of_the_class_cars_ahead_and_behind():
    assert rival_laps_line(read_field(_frame(FIELD))) == (
        HEAD + "car ahead in class A (A Entry): best 1:21.618, last 1:22.004; car behind "
        "in class C (C Entry): best 1:22.300, last 1:23.100; yours: best 1:20.874, last "
        "1:21.200.")


def test_the_neighbour_is_the_class_one_not_the_overall_one():
    line = rival_laps_line(read_field(_frame(FIELD)))
    assert "H (H Entry)" not in line and "1:10.000" not in line and "1:11.000" not in line


def test_no_time_yet_is_left_out_never_zero():
    # LMU's "not set": 0 or negative. C has neither; A only a last lap.
    cars = [("A", "GT3", -1.0, 82.004), ("H", "Hypercar", 70.0, 71.0),
            ("You", "GT3", 0.0, 0.0), ("C", "GT3", 0.0, -1.0)]
    assert rival_laps_line(read_field(_frame(cars))) == (
        HEAD + "car ahead in class A (A Entry): last 1:22.004.")
    none_set = [(n, c, 0.0, -1.0) for n, c, _, _ in cars]
    assert rival_laps_line(read_field(_frame(none_set))) is None


def test_get_race_state_states_them(tmp_path):
    rs = RaceSample(class_position=2, class_ahead="A (A Entry)", class_gap_ahead=3.0,
                    class_behind="C (C Entry)", class_gap_behind=1.5, max_laps=24)
    snap = build_snapshot([LapFacts(lap_no=n, lap_time=81.0, clean=True, position=3,
                                    race=rs) for n in (2, 3)], "s")
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    frame = _frame(FIELD)
    out = CoachContext(store, on_track=True, race=snap,
                       live_field=lambda: read_field(frame)).dispatch("get_race_state", {})
    assert "Car ahead in class (P1) at the end of lap 3: A (A Entry), 3.0 s ahead" in out
    # #254/#255 lines follow it; the frame has no track length, so no on-track line
    assert out.splitlines()[-2:] == [rival_laps_line(read_field(frame)),
                                     class_field_line(read_field(frame))]
    # Without a live frame there is no measured time to state.
    plain = CoachContext(store, on_track=True, race=snap).dispatch("get_race_state", {})
    assert "Lap times" not in plain


def test_result_log_parses_every_class_rivals_best():
    rs = parse_result(XML, "Jayson G")
    assert rs.best_lap == 120.9103
    assert rs.class_bests == [
        {"p": 1, "name": "Riley Fast", "best": 114.9868, "me": False,
         "st": "Finished Normally"},
        {"p": 8, "name": "Jayson G", "best": 120.9103, "me": True, "st": "Finished Normally"}]


def test_result_log_keeps_only_the_players_class(tmp_path):
    xml = tmp_path / "multi.xml"
    xml.write_text(XML.read_text().replace(
        "<CarClass>LMP2</CarClass>\n<CarNumber>3</CarNumber>",
        "<CarClass>Hypercar</CarClass>\n<CarNumber>3</CarNumber>", 1))
    assert [d["name"] for d in parse_result(xml, "Jayson G").class_bests] == ["Jayson G"]


# A third LMP2, LAST in the file and in class: a 3-lap DNF with the fastest best of all
# (the real logs' "1:55.030 Kai Chase (P16)" shape). The file order is then not the
# fastest-first order, and his bracket is no finish.
DNF = """<Driver>
<Name>Kai Chase</Name>
<VehName>Some Team #9:LM</VehName>
<CarClass>LMP2</CarClass>
<Position>18</Position>
<ClassPosition>9</ClassPosition>
<Lap num="1" p="18" et="130.0">110.5000</Lap>
<BestLapTime>110.5000</BestLapTime>
<Laps>3</Laps>
<FinishStatus>DNF</FinishStatus>
</Driver>
</Race>"""


def _store_with_dnf(tmp_path, store=None, sid="log"):
    xml = tmp_path / "dnf.xml"
    xml.write_text(XML.read_text().replace("</Race>", DNF, 1))
    store = store or LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save_result_session(parse_result(xml, "Jayson G"), session_id=sid)
    return store


def test_get_race_result_ranks_the_class_bests_and_tags_a_dnf(tmp_path):
    assert ("Best laps in your class (result log), fastest first, class position in "
            "brackets: 1:50.500 Kai Chase (P9, DNF); 1:54.987 Riley Fast (P1); 2:00.910 "
            "you (P8).") in race_result_text(_store_with_dnf(tmp_path), "log")


def test_a_logged_status_of_none_is_no_status(tmp_path):
    # The real logs write <FinishStatus>None</FinishStatus> for a car with no result
    # (Long Beach 2026-09-27: Bastien Boucher, 9 laps): no "(P9, None)".
    xml = tmp_path / "none.xml"
    xml.write_text(XML.read_text().replace("</Race>", DNF, 1).replace(
        "<FinishStatus>DNF</FinishStatus>", "<FinishStatus>None</FinishStatus>"))
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save_result_session(parse_result(xml, "Jayson G"), session_id="log")
    assert "1:50.500 Kai Chase (P9); 1:54.987" in race_result_text(store, "log")


def test_the_profile_digest_leaves_the_rival_list_out(tmp_path, monkeypatch, mem_keyring,
                                                      synthetic_lap):
    """/profile/distill's digest reads race_result_text too: a class of rival names must
    not reach the memory proposals."""
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel(json.dumps({"memories": [], "summary": ""}))
    c = TestClient(create_app(store, Coach(fake)), base_url="http://127.0.0.1")
    store.save(dataclasses.replace(synthetic_lap, session_id="sess-1", phase="race",
                                   timestamp=2.0, position=17))
    _store_with_dnf(tmp_path, store, "sess-1")
    assert "Kai Chase" in race_result_text(store, "sess-1")      # the coach tool keeps it
    assert c.post("/profile/distill", json={"session_id": "sess-1"}).status_code == 200
    prompt = fake.last_system + " ".join(str(m) for m in fake.last_messages)
    assert "Finished P8 in class" in prompt                       # the digest's race is there
    assert "Best laps" not in prompt and "Kai Chase" not in prompt
    assert "Riley Fast" not in prompt


def test_v27_db_gains_the_column_and_re_reads_the_logs(tmp_path):
    db = tmp_path / "db.sqlite"
    LapStore(str(db), str(tmp_path / "telem")).conn.close()
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE race_sessions DROP COLUMN class_bests")
    conn.execute("INSERT INTO ingested_files VALUES ('x.xml', 1.0)")
    conn.execute("PRAGMA user_version = 27")
    conn.commit()
    conn.close()
    store = LapStore(str(db), str(tmp_path / "telem"))
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert "class_bests" in [r[1] for r in store.conn.execute(
        "PRAGMA table_info(race_sessions)")]
    assert store.ingested_files() == set()     # every log is re-read to backfill it
    store.save_result_session(parse_result(XML, "Jayson G"), session_id="log")
    assert store.race_session("log")["class_bests"][0]["name"] == "Riley Fast"
