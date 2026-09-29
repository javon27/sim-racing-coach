"""#213 (rig 2026-09-27, Long Beach): "What is his fastest time?" about the car ahead got
"no tool reads a rival's fastest lap". The live scoring frame carries every car's best
and last lap (lmu.live), and the result log every driver's <BestLapTime>."""
import sqlite3
from pathlib import Path

from simcoach.coach_tools import CoachContext, race_result_text
from simcoach.lmu.live import read_field
from simcoach.lmu.results import parse_result
from simcoach.lmu.rf2structs import VehicleScoringInfoV01, rF2Scoring
from simcoach.model_types import RaceSample
from simcoach.race import LapFacts, build_snapshot, rival_laps_line
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
    assert "Car ahead in class (P1): A (A Entry), 3.0 s ahead" in out
    assert out.splitlines()[-1] == rival_laps_line(read_field(frame))
    # Without a live frame there is no measured time to state.
    plain = CoachContext(store, on_track=True, race=snap).dispatch("get_race_state", {})
    assert "Lap times" not in plain


def test_result_log_parses_every_class_rivals_best():
    rs = parse_result(XML, "Jayson G")
    assert rs.best_lap == 120.9103
    assert rs.class_bests == [
        {"p": 1, "name": "Ryan Friend", "best": 114.9868, "me": False},
        {"p": 8, "name": "Jayson G", "best": 120.9103, "me": True}]


def test_result_log_keeps_only_the_players_class(tmp_path):
    xml = tmp_path / "multi.xml"
    xml.write_text(XML.read_text().replace(
        "<CarClass>LMP2</CarClass>\n<CarNumber>3</CarNumber>",
        "<CarClass>Hypercar</CarClass>\n<CarNumber>3</CarNumber>", 1))
    assert [d["name"] for d in parse_result(xml, "Jayson G").class_bests] == ["Jayson G"]


def test_get_race_result_ranks_the_class_bests(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save_result_session(parse_result(XML, "Jayson G"), session_id="log")
    assert ("Best laps in your class (result log), fastest first, class finish in "
            "brackets: 1:54.987 Ryan Friend (P1); 2:00.910 you (P8).") in race_result_text(
                store, "log")


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
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 28
    assert store.ingested_files() == set()
    store.save_result_session(parse_result(XML, "Jayson G"), session_id="log")
    assert store.race_session("log")["class_bests"][0]["name"] == "Ryan Friend"
