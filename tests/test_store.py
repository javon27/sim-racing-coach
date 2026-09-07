import sqlite3
import threading

import numpy as np
import pytest

from simcoach.model_types import LapRecord


def test_save_load_and_best(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lid = s.save(synthetic_lap)
    back = s.load(lid)
    assert back.lap_time == synthetic_lap.lap_time
    assert np.allclose(back.channels["speed"], synthetic_lap.channels["speed"])
    slow = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 99.0})
    s.save(slow)
    assert s.best_lap("test_track", "test_car").lap_time == 92.5


def test_sector_bounds_from_latest_capturing_lap(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    assert s.sector_bounds("test_track", "test_car") is None      # none captured yet
    s.save(LapRecord(**{**synthetic_lap.__dict__, "sector_bounds": None}))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "sector_bounds": [975.1, 2585.4]}))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "sector_bounds": None}))  # newer, no bounds
    # Returns the most-recent lap that actually captured bounds, skipping None rows.
    assert s.sector_bounds("test_track", "test_car") == [975.1, 2585.4]


def test_save_is_atomic_on_failure(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))

    class BadMetric:
        pass  # missing corner_no etc. -> AttributeError while building metrics rows

    with pytest.raises(AttributeError):
        s.save(synthetic_lap, metrics=[BadMetric()])

    # no phantom laps row left behind
    count = s.conn.execute("SELECT COUNT(*) FROM laps").fetchone()[0]
    assert count == 0
    # no orphaned .npz left behind
    assert list((tmp_path/"data").iterdir()) == []

    # store still works for a subsequent good save
    lid = s.save(synthetic_lap)
    assert s.load(lid).lap_time == synthetic_lap.lap_time


def test_user_version_not_clobbered(tmp_path):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db_path = str(tmp_path/"c.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    conn.close()

    LapStore(db_path, str(tmp_path/"data"))

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == SCHEMA_VERSION


def test_load_missing_lap_raises_keyerror(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    with pytest.raises(KeyError):
        s.load(99999)


def test_best_lap_excludes_given_lap_id(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    fastest_id = s.save(synthetic_lap)  # lap_time 92.5, the fastest
    slower = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 99.0})
    s.save(slower)

    # Excluding the fastest lap should return the next-fastest instead.
    ref = s.best_lap("test_track", "test_car", exclude_lap_id=fastest_id)
    assert ref is not None
    assert ref.lap_time == 99.0

    # Excluding the only lap present leaves no reference.
    only = LapStore(str(tmp_path/"only.sqlite"), str(tmp_path/"only_data"))
    only_id = only.save(synthetic_lap)
    assert only.best_lap("test_track", "test_car", exclude_lap_id=only_id) is None


def test_save_load_roundtrips_phase_and_session_id(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lap = LapRecord(**{**synthetic_lap.__dict__, "phase": "qualifying", "session_id": "s1"})
    lid = s.save(lap)
    back = s.load(lid)
    assert back.phase == "qualifying"
    assert back.session_id == "s1"


def test_save_without_phase_defaults_to_none(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lid = s.save(synthetic_lap)
    back = s.load(lid)
    assert back.phase is None
    assert back.session_id is None


def test_session_lap_ids(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lap_s1a = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1"})
    lap_s1b = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1"})
    lap_s2 = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s2"})
    id_a = s.save(lap_s1a)
    id_b = s.save(lap_s1b)
    s.save(lap_s2)
    assert s.session_lap_ids("s1") == [id_a, id_b]


def test_facets_group_by_class_and_class_filter(tmp_path, synthetic_lap, monkeypatch):
    from simcoach import store as store_mod
    from simcoach.store import LapStore
    # team entry name -> (model, class), stand-in for the result-log scrape
    table = {"WRT #32": ("BMW M4 LMGT3", "GT3"),
             "Ferrari 499 #50": ("Ferrari 499P", "Hyper")}
    monkeypatch.setattr(store_mod, "resolve_car", lambda c: table.get(c, (c, None))[0])
    monkeypatch.setattr(store_mod, "resolve_car_class", lambda c: table.get(c, (c, None))[1])
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "car": "WRT #32", "session_id": "gt3s"}))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "car": "Ferrari 499 #50", "session_id": "hys"}))

    groups = s.session_facets()["car_groups"]
    # class-ordered (Hyper before GT3), models nested under each class
    assert [g["car_class"] for g in groups] == ["Hyper", "GT3"]
    assert groups[0]["cars"] == ["Ferrari 499P"] and groups[1]["cars"] == ["BMW M4 LMGT3"]

    # selecting a class filters to only that class's sessions
    page = s.race_sessions_page(car_class="Hyper")
    assert [r["session_id"] for r in page["rows"]] == ["hys"]
    assert page["rows"][0]["car_model"] == "Ferrari 499P"
    # and a model filter still narrows to that one model
    assert [r["session_id"] for r in s.race_sessions_page(car="BMW M4 LMGT3")["rows"]] == ["gt3s"]


def test_best_lap_phase_filter(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    practice = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 90.0, "phase": "practice"})
    race = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 95.0, "phase": "race"})
    s.save(practice)
    s.save(race)
    best_race = s.best_lap("test_track", "test_car", phase="race")
    assert best_race.lap_time == 95.0
    assert s.best_lap("test_track", "test_car").lap_time == 90.0


def test_best_lap_session_filter(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    # all-time best is a qualy lap; the race session's own best is slower
    s.save(LapRecord(**{**synthetic_lap.__dict__, "lap_time": 90.0,
                        "session_id": "qualy", "phase": "qualifying"}))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "lap_time": 95.0,
                        "session_id": "race", "phase": "race"}))
    assert s.best_lap("test_track", "test_car").lap_time == 90.0            # all-time
    assert s.best_lap("test_track", "test_car", session_id="race").lap_time == 95.0
    assert s.recent_laps("test_track", "test_car", session_id="race")[0]["phase"] == "race"


def test_v1_schema_migrates_in_place_to_v2(tmp_path, synthetic_lap):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db_path = str(tmp_path/"c.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE laps (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sim TEXT, "
        "track TEXT, car TEXT, lap_time REAL, valid INTEGER, telemetry_path TEXT)"
    )
    conn.execute(
        "INSERT INTO laps (ts,sim,track,car,lap_time,valid,telemetry_path) "
        "VALUES (0,'lmu','t','c',90.0,1,'x.npz')"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    s = LapStore(db_path, str(tmp_path/"data"))

    version = s.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION  # v1 steps all the way to current

    # old row still queryable via the new query helpers
    old_row = s.conn.execute("SELECT id FROM laps WHERE track = 't' AND car = 'c'").fetchone()
    assert old_row is not None

    # v3 coach_chat table reached by the same cumulative migration
    s.save_chat(s.create_chat(), "s1", "t", "c", None, "q", "a")
    assert s.session_chat("s1")[0]["answer"] == "a"

    # a new save() with phase works against the migrated schema
    lap = LapRecord(**{**synthetic_lap.__dict__, "phase": "race", "session_id": "s1"})
    lid = s.save(lap)
    assert s.load(lid).phase == "race"
    assert s.session_lap_ids("s1") == [lid]


def test_concurrent_save_and_read_is_safe(tmp_path, synthetic_lap):
    """A background writer looping save() and a foreground reader looping
    best_lap()/load() on the same LapStore must never crash. Without the
    lock, the reader can observe a laps row mid-save (telemetry_path still
    NULL), and np.load(None) raises -- this exercises that window."""
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    track, car = synthetic_lap.track, synthetic_lap.car
    errors: list[Exception] = []

    def writer():
        try:
            for i in range(100):
                lap = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 100.0 - i * 0.05})
                s.save(lap)
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=writer)
    t.start()
    try:
        while t.is_alive():
            best = s.best_lap(track, car)
            if best is not None:
                assert best.channels["speed"].size > 0
                ids = s.list_lap_ids(track, car)
                if ids:
                    s.load(ids[-1])
    except Exception as e:
        errors.append(e)
    t.join()
    assert errors == []


def test_migration_wraps_legacy_rows(tmp_path):
    from simcoach.store import LapStore
    db = str(tmp_path / "c.sqlite")
    # simulate a v4 DB with a legacy chat row lacking chat_id
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=4")
    con.execute("""CREATE TABLE coach_chat(id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, session_id TEXT, track TEXT, car TEXT, lap_id INTEGER,
        question TEXT, answer TEXT, tool_calls TEXT)""")
    con.execute("INSERT INTO coach_chat(ts,session_id,question,answer) VALUES(1.0,'s1','q','a')")
    con.commit(); con.close()

    store = LapStore(db, str(tmp_path / "data"))  # opening runs the migration
    chats = store.list_chats()
    assert len(chats) == 1
    assert chats[0]["chat_id"] == "legacy"
    assert chats[0]["message_count"] == 1


def test_new_chat_round_trip(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    cid = store.create_chat()
    store.save_chat(cid, "s1", "Le Mans", "Corvette", None, "q1", "a1")
    store.save_chat(cid, "s2", "Le Mans", "Corvette", 7, "q2", "a2", tool_calls=[{"t": "get_corner"}])
    msgs = store.chat_messages(cid)
    assert [m["question"] for m in msgs] == ["q1", "q2"]
    assert msgs[1]["lap_id"] == 7
    assert msgs[1]["tool_calls"] == [{"t": "get_corner"}]


def test_chats_for_session_only_matching_newest_first(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    # chatA touches s1; chatB touches s2 only; chatC touches s1 later.
    a = store.create_chat()
    store.save_chat(a, "s1", "Le Mans", "Corvette", None, "qa", "aa", ts=10.0)
    b = store.create_chat()
    store.save_chat(b, "s2", "Spa", "Ferrari", None, "qb", "ab", ts=20.0)
    c = store.create_chat()
    store.save_chat(c, "s1", "Imola", "Corvette", None, "qc", "ac", ts=30.0)

    rows = store.chats_for_session("s1")
    assert [r["chat_id"] for r in rows] == [c, a]  # newest last-activity first
    assert all(r["chat_id"] != b for r in rows)
    assert store.chats_for_session("s2") == store.chats_for_session("s2")
    assert [r["chat_id"] for r in store.chats_for_session("s2")] == [b]
    assert store.chats_for_session("nope") == []


def test_migration_tolerates_duplicate_column(tmp_path):
    """Simulates a raced/partial v4->v5 upgrade: coach_chat already has
    chat_id but user_version is still 4. Opening the store (twice, to also
    cover the case of a second process racing the first) must not crash."""
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=4")
    con.execute("""CREATE TABLE coach_chat(id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, session_id TEXT, track TEXT, car TEXT, lap_id INTEGER,
        question TEXT, answer TEXT, tool_calls TEXT, chat_id TEXT)""")
    con.execute("INSERT INTO coach_chat(ts,session_id,chat_id,question,answer) "
                "VALUES(1.0,'s1','legacy','q','a')")
    con.commit(); con.close()

    LapStore(db, str(tmp_path / "data"))
    store = LapStore(db, str(tmp_path / "data"))  # open again: no crash

    version = store.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    rows = store.conn.execute("SELECT chat_id, question, answer FROM coach_chat").fetchall()
    assert rows == [("legacy", "q", "a")]


def test_chat_messages_survives_bad_tool_calls(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    cid = store.create_chat()
    store.save_chat(cid, "s1", "Le Mans", "Corvette", None, "q1", "a1",
                     tool_calls=[{"t": "get_corner"}])
    store.save_chat(cid, "s2", "Le Mans", "Corvette", 7, "q2", "a2")
    store.conn.execute(
        "UPDATE coach_chat SET tool_calls = ? WHERE question = ?",
        ("not json {{{", "q2"),
    )
    store.conn.commit()

    msgs = store.chat_messages(cid)
    assert [m["question"] for m in msgs] == ["q1", "q2"]
    assert msgs[0]["tool_calls"] == [{"t": "get_corner"}]
    assert msgs[1]["tool_calls"] == "not json {{{"


def test_save_chat_rejects_null_chat_id(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    with pytest.raises(ValueError):
        store.save_chat(None, "s1", "Le Mans", "Corvette", None, "q", "a")


def test_v5_schema_migrates_source_column_to_v6(tmp_path, synthetic_lap):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db_path = str(tmp_path / "c.sqlite")
    # simulate a v5 db: laps table with no `source` column, one legacy row.
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE laps (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sim TEXT, "
        "track TEXT, car TEXT, lap_time REAL, valid INTEGER, telemetry_path TEXT, "
        "phase TEXT, session_id TEXT)"
    )
    con.execute(
        "INSERT INTO laps (ts,sim,track,car,lap_time,valid,telemetry_path) "
        "VALUES (0,'lmu','t','c',90.0,1,'x.npz')"
    )
    con.execute("PRAGMA user_version = 5")
    con.commit()
    con.close()

    s = LapStore(db_path, str(tmp_path / "data"))
    version = s.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION

    # legacy row reads back with the default source
    row = s.conn.execute("SELECT source FROM laps WHERE track='t'").fetchone()
    assert row[0] == "live"

    # a new save() still works and defaults to 'live'
    lid = s.save(synthetic_lap)
    row = s.conn.execute("SELECT source FROM laps WHERE id=?", (lid,)).fetchone()
    assert row[0] == "live"

    # dup-safe on 2nd open of an already-migrated db
    LapStore(db_path, str(tmp_path / "data"))


def _fake_result_session(**overrides):
    from simcoach.lmu.results import ResultLap, ResultSession
    laps = overrides.pop("laps", [
        ResultLap(lap_no=1, lap_time=91.234, sectors=[30.1, 30.2, 30.9], valid=True),
        ResultLap(lap_no=2, lap_time=90.876, sectors=[29.9, 30.1, 30.9], valid=True),
    ])
    defaults = dict(
        sim="lmu", track="Sebring", car="GT3 Vantage", car_class="GT3",
        date_ts=1234567890, session_type="race", position=3, class_position=2,
        best_lap=90.876, finish_status="Finished", laps=laps, incidents=[], penalties=[],
    )
    defaults.update(overrides)
    return ResultSession(**defaults)


def test_save_result_session_inserts_rows_queryable(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session()

    s.save_result_session(rs, session_id="result-abc")

    laps = s.session_laps("result-abc")
    assert len(laps) == 2
    assert sorted(l["lap_time"] for l in laps) == [90.876, 91.234]
    rows = s.conn.execute(
        "SELECT source, telemetry_path, track, car, phase FROM laps WHERE session_id='result-abc'"
    ).fetchall()
    assert all(r[0] == "result" for r in rows)
    assert all(r[1] is None for r in rows)
    assert all(r[2] == "Sebring" and r[3] == "GT3 Vantage" and r[4] == "race" for r in rows)

    sessions = s.recent_race_sessions()
    assert any(sess["session_id"] == "result-abc" and sess["laps"] == 2 for sess in sessions)


def test_save_result_session_persists_sectors(tmp_path):
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session(laps=[
        ResultLap(lap_no=1, lap_time=91.2, sectors=[30.1, 30.2, 30.9], valid=True),
        ResultLap(lap_no=2, lap_time=None, sectors=[], valid=False),  # DNF: no sectors -> NULLs
    ])
    s.save_result_session(rs, session_id="result-sec")

    laps = {l["lap_time"]: l for l in s.session_laps("result-sec")}
    assert (laps[91.2]["s1"], laps[91.2]["s2"], laps[91.2]["s3"]) == (30.1, 30.2, 30.9)
    empty = next(l for l in s.session_laps("result-sec") if l["lap_time"] is None)
    assert empty["s1"] is None and empty["s2"] is None and empty["s3"] is None


def test_save_result_session_marks_invalid_laps(tmp_path):
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session(laps=[ResultLap(lap_no=1, lap_time=None, sectors=[], valid=False)])

    s.save_result_session(rs, session_id="result-dnf")

    row = s.conn.execute(
        "SELECT lap_time, valid FROM laps WHERE session_id='result-dnf'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == 0


def test_best_lap_skips_result_laps_without_telemetry(tmp_path, synthetic_lap):
    from simcoach.lmu.results import ResultLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))

    # Only lap for this track/car is a result-import (no telemetry) -- must not crash.
    rs = _fake_result_session(track="test_track", car="test_car")
    s.save_result_session(rs, session_id="result-abc")
    assert s.best_lap("test_track", "test_car") is None

    # A live lap (with telemetry) beats a faster result-import lap.
    s.save(synthetic_lap)  # lap_time 92.5
    faster_result = _fake_result_session(
        track="test_track", car="test_car",
        laps=[ResultLap(lap_no=1, lap_time=80.0, sectors=[], valid=True)],
    )
    s.save_result_session(faster_result, session_id="result-fast")
    best = s.best_lap("test_track", "test_car")
    assert best is not None
    assert best.lap_time == 92.5


def test_save_result_session_persists_race_row(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session()
    s.save_result_session(rs, session_id="result-abc")

    race = s.race_session("result-abc")
    assert race == {"position": 3, "class_position": 2, "finish_status": "Finished",
                    "incidents": [], "penalties": []}


def test_race_session_round_trips_incidents_and_penalties(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session(
        incidents=["Contact with wall at T3", "Off track T7"],
        penalties=[{"text": "Drive-through: track limits", "et": "123.4"}],
    )
    s.save_result_session(rs, session_id="result-inc")

    race = s.race_session("result-inc")
    assert race["incidents"] == ["Contact with wall at T3", "Off track T7"]
    assert race["penalties"] == [{"text": "Drive-through: track limits", "et": "123.4"}]


def test_race_session_none_when_absent(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    assert s.race_session("nope") is None


def test_load_result_lap_raises_filenotfounderror(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = _fake_result_session()
    s.save_result_session(rs, session_id="result-abc")
    lap_id = s.session_lap_ids("result-abc")[0]

    with pytest.raises(FileNotFoundError):
        s.load(lap_id)


def test_is_ingested_and_mark_ingested_round_trip(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))

    assert s.is_ingested("some/file.xml", 100.0) is False

    s.mark_ingested("some/file.xml", 100.0)
    assert s.is_ingested("some/file.xml", 100.0) is True

    # a changed mtime (file re-saved) is treated as not-yet-ingested
    assert s.is_ingested("some/file.xml", 200.0) is False

    # marking again (re-ingest after mtime change) updates cleanly, no crash
    s.mark_ingested("some/file.xml", 200.0)
    assert s.is_ingested("some/file.xml", 200.0) is True


def test_save_persists_live_sectors(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap = LapRecord(**{**synthetic_lap.__dict__,
                       "session_id": "s-sec", "sectors": [30.1, 30.2, 30.9]})
    s.save(lap)
    row = s.session_laps("s-sec")[0]
    assert (row["s1"], row["s2"], row["s3"]) == (30.1, 30.2, 30.9)


def test_save_without_sectors_writes_null(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s-none", "sectors": None})
    s.save(lap)
    row = s.session_laps("s-none")[0]
    assert row["s1"] is None and row["s2"] is None and row["s3"] is None


def test_save_persists_position_and_arc(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    # A race that ran P5 -> P8 -> P10 -> P3; distinct ts so ORDER BY ts is stable.
    for i, place in enumerate((5, 8, 10, 3)):
        s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "race",
                            "timestamp": float(i), "position": place}))
    assert s.session_position_arc("race") == {
        "start": 5, "best": 3, "worst": 10, "last": 3, "laps": 4,
    }


def test_session_position_arc_none_without_positions(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    s.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "prac", "position": None}))
    assert s.session_position_arc("prac") is None
