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
    from simcoach.store import LapStore
    db_path = str(tmp_path/"c.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    LapStore(db_path, str(tmp_path/"data"))

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == 2


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


def test_v1_schema_migrates_in_place_to_v2(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
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
    assert version == 2

    # old row still queryable via the new query helpers
    old_row = s.conn.execute("SELECT id FROM laps WHERE track = 't' AND car = 'c'").fetchone()
    assert old_row is not None

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
